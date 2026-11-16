"""Fase 3 de ponta a ponta: o caixa principal vende, o caixa adicional replica pela rede, o principal
morre e o adicional assume com promotion.promote, usando o Store de verdade.

Depende do núcleo da fase 3 (Store.apply_journal_events e Store.finish_promotion); enquanto ele não
estiver na árvore, este arquivo inteiro é pulado. A parte com internet depende também da nuvem da
fase 3 (capacidade "stations").
"""

import json
import shutil
import sqlite3
import unittest
import uuid
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from terminal.store import Store, TerminalError

if __name__.startswith("terminal."):
    from terminal.tests.test_e2e_replica_recuperacao import CLOUD_URL, PIN, RealPhaseTwoCase
    from terminal.tests.test_replica_net import Lan
else:
    from test_e2e_replica_recuperacao import CLOUD_URL, PIN, RealPhaseTwoCase
    from test_replica_net import Lan

from core import views_terminal
from core.models import Customer, Sale, StockMovement, SyncEvent, TerminalFormerToken
from terminal import promotion, replica_net, sync

HAS_CORE = hasattr(Store, "apply_journal_events") and hasattr(Store, "finish_promotion")
HAS_CLOUD = "stations" in getattr(views_terminal, "CLOUD_CAPABILITIES", [])
STATION_ID = "0f" * 16

TABLES = {
    "sales": "SELECT id,status,gross_cents,discount_cents,total_cents,session_id,customer_id FROM sales ORDER BY id",
    "stock": "SELECT id,stock_milli FROM products ORDER BY id",
    "fiado": "SELECT id,open_credit_cents,store_credit_cents FROM customers ORDER BY id",
    "cash": "SELECT id,operator_id,opening_cents,opened_at,closed_at,counted_cents,expected_cents "
            "FROM cash_sessions ORDER BY id",
    "movements": "SELECT id,session_id,kind,amount_cents FROM cash_movements ORDER BY id",
    "returns": "SELECT id,sale_id,refund_method,total_cents,items FROM sale_returns ORDER BY id",
    "receipts": "SELECT id,customer_id,method,amount_cents FROM credit_receipts ORDER BY id",
    "queue": "SELECT seq,id,event_type,payload,acked_at IS NULL FROM outbox ORDER BY seq",
}


def picture(path):
    """O que a loja tem em um banco: vendas, estoque, fiado, caixas e a fila, lidos direto do arquivo."""
    with closing(sqlite3.connect(path)) as conn:
        return {name: conn.execute(query).fetchall() for name, query in TABLES.items()}


@unittest.skipUnless(HAS_CORE, "depende do núcleo da fase 3 (Store.apply_journal_events / finish_promotion)")
class PromotionCase(RealPhaseTwoCase):
    def setUp(self):
        super().setUp()
        self.addCleanup(replica_net.stop_all)
        self.customer = Customer.objects.create(tenant=self.tenant, name="Seu Zé", credit_limit_cents=50000)

    def sell_on_credit(self, computer, quantity=1):
        sale = computer.store.sale(computer.operator, {
            "request_id": "fiado-" + uuid.uuid4().hex, "customer_id": str(self.customer.id),
            "items": [{"product_id": str(self.product.id), "quantity_milli": quantity * 1000}],
            "payments": [{"method": "credit_account", "amount_cents": quantity * 1500}]})
        self.sold.append(sale["id"])
        return sale

    def shop_running(self):
        """Caixa principal vendendo, caixa adicional com a cópia, e mais movimento só no diário."""
        old = self.first_computer("principal")
        old.store.open_cash(old.operator["id"], 10000)
        first = self.sell(old, 2)
        self.sell_on_credit(old, 2)

        station_dir = self.folder / "caixa-2"
        station_dir.mkdir()
        lan = Lan(old.store, STATION_ID)
        puller = replica_net.Puller(station_dir, lan.client)
        for _ in range(400):
            if puller.cycle() and replica_net.puller_status(station_dir)["state"] == "ok":
                break
        self.assertEqual(replica_net.puller_status(station_dir)["state"], "ok")

        # Daqui em diante nenhuma cópia nova do banco: o que acontecer chega só pelo diário.
        with patch.object(replica_net, "_maybe_build", lambda *args, **kwargs: None):
            self.sell(old, 3)
            old.store.return_sale(old.operator, first["id"], {
                "return_id": str(uuid.uuid4()), "refund_method": "cash", "reason": "Produto vencido",
                "items": [{"product_id": str(self.product.id), "quantity_milli": 1000}]})
            old.store.receive_credit(old.operator, {
                "receipt_id": str(uuid.uuid4()), "customer_id": str(self.customer.id),
                "amount_cents": 1000, "method": "cash"})
            old.store.close_cash(old.operator["id"], 10000, PIN)
            old.store.open_cash(old.operator["id"], 5000)
            self.sell(old, 1)
            self.assertTrue(puller.cycle())
        status = replica_net.puller_status(station_dir)
        self.assertEqual(status["journal_seq"], status["server_seq"])
        self.assertGreater(status["journal_seq"], puller.state["last_snapshot"]["outbox_seq"])
        return old, station_dir

    def kill(self, old):
        """O computador principal morre: fica só o retrato do que ele tinha."""
        before = picture(old.store.path)
        meta = old.store.meta()
        open_cash = old.store.cash_state()["id"]
        shutil.rmtree(old.directory)
        return before, meta, open_cash

    def promote(self, station_dir):
        self.assertTrue(promotion.status(station_dir)["available"])
        result = promotion.promote(station_dir, PIN, station_id=STATION_ID, station_name="Caixa 2")
        self.assertTrue(promotion.pending(station_dir))
        return result


class OfflinePromotionTests(PromotionCase):
    def test_caixa_adicional_assume_sem_internet_com_tudo_que_o_principal_tinha(self):
        old, station_dir = self.shop_running()
        before, old_meta, old_cash = self.kill(old)

        with self.offline():
            result = self.promote(station_dir)
            after = picture(station_dir / "terminal.sqlite3")
            # Mesmas vendas, estoque, fiado, caixas, devoluções, recebimentos e a mesma fila:
            # nenhum evento novo, nenhum perdido, mesma ordem.
            for name in TABLES:
                self.assertEqual(after[name], before[name], name)
            self.assertEqual(result["journal_last_seq"], before["queue"][-1][0])
            self.assertGreater(result["applied"], 0)
            self.assertEqual((result["previous_server_cash_open"], result["warnings"]), (True, []))

            new = self.computer("caixa-2")
            self.assertEqual(new.store.path, station_dir / "terminal.sqlite3")
            meta = new.store.meta()
            self.assertEqual(new.store.identity()["terminal_id"], str(self.terminal.id))
            self.assertNotIn("token", meta)
            self.assertEqual(json.loads(meta["mirror_key"]), json.loads(old_meta["mirror_key"]))
            state = new.store.promotion_status()
            self.assertEqual((state["promoted"], state["state"], state["previous_server_cash_open"]),
                             (True, "awaiting_cloud", True))

            # Vende na hora, com a licença que veio na réplica.
            new.operator = new.store.login(str(self.member.id), PIN)
            self.assertTrue(new.store.lease_status()["can_sell"])
            self.assertIsNone(new.store.cash_state())  # o caixa aberto era o do computador anterior
            new.store.open_cash(new.operator["id"], 2000)
            resumed = self.sell(new)
            self.assertEqual(new.store.sale_detail(resumed["id"])["status"], "completed")

            # O caixa do computador anterior aparece para o gerente fechar.
            opened = {item["id"]: item for item in new.store.open_cash_sessions()}
            self.assertIn(old_cash, opened)
            previous = opened[old_cash]["station_id"]
            self.assertTrue(previous.startswith("anterior-"), previous)
            new.store.close_cash(new.operator["id"], 6500, PIN, station_id=previous)
            self.assertNotIn(old_cash, {item["id"] for item in new.store.open_cash_sessions()})

        # Repetir a promoção não faz nada.
        with self.assertRaises(TerminalError) as caught:
            promotion.promote(station_dir, PIN, station_id=STATION_ID, station_name="Caixa 2")
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(len(list(station_dir.glob("terminal-antes-da-promocao-*"))), 0)

    def test_aplicar_o_diario_duas_vezes_e_inocuo(self):
        old, station_dir = self.shop_running()
        before, _, _ = self.kill(old)
        self.promote(station_dir)
        promoted = Store(station_dir / "terminal.sqlite3")
        events = [{"seq": seq, "event_id": event_id, "created_at": None, "event": json.loads(payload)}
                  for seq, event_id, _, payload, _ in before["queue"]]
        again = promoted.apply_journal_events(events)
        self.assertEqual((again["applied"], again["skipped"]), (0, len(events)))
        after = picture(promoted.path)
        for name in ("sales", "stock", "fiado", "cash", "queue"):
            self.assertEqual(after[name], before[name], name)


@unittest.skipUnless(HAS_CLOUD, "depende da nuvem da fase 3 (capacidade \"stations\")")
class OnlinePromotionTests(PromotionCase):
    def test_regularizar_com_o_codigo_de_recuperacao_sobe_a_fila_sem_duplicar(self):
        old, station_dir = self.shop_running()
        with self.cloud():  # parte da fila já tinha subido antes de o principal morrer
            sync.sync_once(old.store)
        self.sell(old, 1)   # e esta venda ficou só no principal e na réplica
        puller = replica_net.Puller(station_dir, Lan(old.store, STATION_ID).client)
        with patch.object(replica_net, "_maybe_build", lambda *args, **kwargs: None):
            self.assertTrue(puller.cycle())
        before, old_meta, _ = self.kill(old)
        self.promote(station_dir)
        new = self.computer("caixa-2")
        self.assertEqual(new.store.promotion_status()["state"], "awaiting_cloud")

        transfer = self.panel_call("POST", f"/api/v1/terminals/{self.terminal.id}/transfer")
        with self.cloud():
            answer = sync.regularize_promotion(new.store, CLOUD_URL, transfer["license_key"],
                                               transfer["recovery_code"].replace("-", "").lower())
            self.assertTrue(answer["regularized"])
            self.assertEqual(new.store.promotion_status()["state"], "done")
            self.assertFalse(new.store.restore_status()["active"])
            for _ in range(20):
                if sync.sync_once(new.store)["remaining"] == 0:
                    break
            self.assertEqual(new.store.pending_events(50), [])

        # A nuvem tem cada venda uma vez só, e cada evento uma vez só.
        self.assertEqual(self.cloud_sales(), sorted(self.sold))
        for _, event_id, _, _, _ in before["queue"]:
            self.assertEqual(SyncEvent.objects.filter(event_id=event_id).count(), 1, event_id)
        sold_milli = sum(item["quantity_milli"] for sale in Sale.objects.filter(terminal=self.terminal)
                         for item in sale.items.values("quantity_milli"))
        balance = sum(StockMovement.objects.filter(shop=self.shop, product=self.product)
                      .values_list("delta_milli", flat=True))
        self.assertEqual(balance, 90000 - sold_milli + 1000)  # uma unidade devolvida

        # O token do computador antigo virou só-envio.
        self.terminal.refresh_from_db()
        self.assertEqual(self.terminal.device_id, new.store.meta()["device_id"])
        self.assertNotEqual(new.store.meta()["device_id"], old_meta["device_id"])
        self.assertTrue(TerminalFormerToken.objects.filter(terminal=self.terminal,
                                                           device_id=old_meta["device_id"]).exists())
        with self.cloud():
            with self.assertRaises(TerminalError) as caught:
                sync._request("POST", CLOUD_URL + "/api/v1/terminal/heartbeat", headers=sync._auth_headers(old_meta),
                              json={"version": sync.VERSION, "pending_events": 0})
            self.assertEqual(getattr(caught.exception, "remote_code", None), "terminal_transferred")
            # ...e o envio dele ainda é aceito.
            again = sync._request("POST", CLOUD_URL + "/api/v1/terminal/sync", headers=sync._auth_headers(old_meta),
                                  json={"events": [json.loads(before["queue"][-1][3])]})
            self.assertTrue(again.get("send_only"), again)

    @unittest.skipUnless(hasattr(Store, "enroll_station"), "depende do núcleo da fase 3 (Store.enroll_station)")
    def test_outro_caixa_religado_com_o_mesmo_station_id_conserva_o_caixa_aberto(self):
        old = self.first_computer("principal")
        old.store.open_cash(old.operator["id"], 10000)
        code = old.store.create_station_code(old.operator["id"], "Caixa 3")
        third = old.store.enroll_station(code["code"], name="Caixa 3", hostname="PC-CAIXA3")
        with old.store.station(third["station_id"]):
            old.store.open_cash(old.operator["id"], 3000)
            third_cash = old.store.cash_state()["id"]
            self.sell(old, 1)

        station_dir = self.folder / "caixa-2"
        station_dir.mkdir()
        puller = replica_net.Puller(station_dir, Lan(old.store, STATION_ID).client)
        for _ in range(400):
            if puller.cycle() and replica_net.puller_status(station_dir)["state"] == "ok":
                break
        self.kill(old)
        self.promote(station_dir)
        new = self.computer("caixa-2")
        new.operator = new.store.login(str(self.member.id), PIN)

        # O token antigo do outro caixa não vale no novo principal: ele se vincula de novo.
        with self.assertRaises(TerminalError):
            new.store.authenticate_station(third["token"])
        code = new.store.create_station_code(new.operator["id"])
        again = new.store.enroll_station(code["code"], station_id=third["station_id"])
        self.assertEqual((again["station_id"], again["station_name"]), (third["station_id"], "Caixa 3"))
        self.assertNotEqual(again["token"], third["token"])
        with new.store.station(third["station_id"]):
            self.assertEqual(new.store.cash_state()["id"], third_cash)
            self.sell(new, 1)  # continua vendendo no mesmo caixa aberto


if __name__ == "__main__":
    unittest.main()
