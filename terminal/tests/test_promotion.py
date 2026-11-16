"""Promoção assistida: PIN conferido na cópia, máquina de estados, troca do banco e limpeza em falha.

O banco do caixa principal é de verdade e chega ao caixa adicional pelo puxador; só o Store da
promoção é falso (as operações de verdade, apply_journal_events e finish_promotion, são do núcleo).
"""

import json
import os
import sqlite3
import threading
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from shared.security import hash_pin
from terminal import mirror, promotion, replica_net
from terminal.store import TerminalError

if __name__.startswith("terminal."):
    from terminal.tests.test_replica_net import ReplicaCase, TERMINAL, TOKEN
else:
    from test_replica_net import ReplicaCase, TERMINAL, TOKEN

MANAGER_PIN = "246810"
CASHIER_PIN = "135790"
MINUTE = 60 * 1000


class PromotionCase(ReplicaCase):
    @classmethod
    def setUpClass(cls):
        cls.manager_hash = hash_pin(MANAGER_PIN)
        cls.cashier_hash = hash_pin(CASHIER_PIN)

    def setUp(self):
        super().setUp()
        with self.store.transaction() as conn:
            conn.executemany("INSERT INTO operators(id,name,role,pin_hash,active) VALUES (?,?,?,?,?)", [
                ("op-gerente", "Bruno", "manager", self.manager_hash, 1),
                ("op-caixa", "Carla", "cashier", self.cashier_hash, 1),
                ("op-antigo", "Dono antigo", "owner", self.cashier_hash, 0)])
        self.log = []

    def replicate(self, before=2, after=3):
        """Cópia do banco com ``before`` eventos e mais ``after`` só no diário."""
        self.events(before)
        self.sync()
        later = self.events(after)
        self.assertTrue(self.puller.cycle())
        return later

    def saved(self):
        return json.loads((self.puller.base / "promotion.json").read_text(encoding="utf-8"))

    def factory(self, fail_at=None, cash_open=True):
        case = self

        class FakeStore:
            def __init__(self, path):
                self.path = Path(path)
                case.log.append(("open", self.path.name, case.saved()["step"]))
                if fail_at == "open":
                    raise RuntimeError("segredo-interno")

            def apply_journal_events(self, events):
                case.log.append(("apply", [item["event_id"] for item in events], case.saved()["step"]))
                if fail_at == "apply":
                    raise TerminalError("O diário não pôde ser aplicado.", 409)
                with closing(sqlite3.connect(self.path)) as conn:  # o Store de verdade grava em WAL
                    conn.execute("PRAGMA journal_mode=WAL")
                    conn.execute("INSERT INTO meta VALUES ('aplicado',?)", (str(len(events)),))
                    conn.commit()
                return {"applied": len(events), "skipped": 0, "invalid": 0, "foreign": 0, "reseq": 0,
                        "warnings": 0, "last_seq": events[-1]["seq"] if events else 0}

            def finish_promotion(self, station_id, station_name, mirror_key, operator_id=None):
                case.log.append(("finish", station_id, station_name, mirror_key, operator_id, case.saved()["step"]))
                if fail_at == "finish":
                    raise RuntimeError("segredo-interno")
                return {"promoted": True, "previous_server_station": "anterior-1a2b3c4d",
                        "previous_server_cash_open": cash_open, "generation": 2}

        return FakeStore

    def promote(self, pin=MANAGER_PIN, **kwargs):
        return promotion.promote(self.station_dir, pin, station_id="caixa-2", station_name="Caixa 2",
                                 store_factory=kwargs.pop("store_factory", None) or self.factory(**kwargs))


class StatusTests(PromotionCase):
    def test_sem_copia_nao_oferece_e_com_copia_mostra_de_quando_ela_e(self):
        empty = promotion.status(self.station_dir)
        self.assertEqual(set(empty), {"available", "reason", "state", "step", "error", "shop_name", "terminal_name",
                                      "last_snapshot_at", "journal_seq", "last_contact_at", "locked_until"})
        self.assertEqual((empty["available"], empty["state"], empty["last_snapshot_at"]), (False, "idle", None))
        self.assertIn("ainda não tem uma cópia completa", empty["reason"])
        self.assertFalse(promotion.pending(self.station_dir))

        self.replicate()
        status = promotion.status(self.station_dir)
        snapshot = self.server_status()["snapshot"]
        self.assertEqual((status["available"], status["reason"], status["state"], status["step"], status["error"]),
                         (True, "", "idle", "", ""))
        self.assertEqual((status["shop_name"], status["terminal_name"], status["journal_seq"]),
                         ("Matriz", "Caixa 01", 5))
        self.assertEqual(status["last_snapshot_at"], snapshot["created_at"])
        self.assertIsNotNone(status["last_contact_at"])
        self.assertIsNone(status["locked_until"])
        self.assertNotIn(self.key["key"], json.dumps(status))

    def test_pasta_inexistente_nao_levanta(self):
        status = promotion.status(self.root / "nao-existe")
        self.assertEqual((status["available"], status["state"]), (False, "idle"))
        self.assertFalse(promotion.pending(self.root / "nao-existe"))


class PinDoGerenteTests(PromotionCase):
    def test_pin_de_gerente_passa_e_de_operador_de_caixa_nao(self):
        self.replicate()
        self.assertEqual(promotion.verify_manager(self.station_dir, MANAGER_PIN),
                         {"operator_id": "op-gerente", "name": "Bruno", "role": "manager"})
        # O PIN do caixa é também o de um dono desativado: nenhum dos dois autoriza.
        for wrong in (CASHIER_PIN, "000000", "", None, 246810):
            with self.assertRaises(TerminalError) as caught:
                promotion.verify_manager(self.station_dir, wrong)
            self.assertEqual((caught.exception.status, str(caught.exception)), (403, "PIN de gerente inválido"))
            promotion.verify_manager(self.station_dir, MANAGER_PIN)  # o acerto zera a contagem
        # A cópia decifrada para conferir o PIN não fica no disco.
        self.assertEqual([path.name for path in self.puller.base.iterdir() if path.name.startswith(".pin-")], [])
        for path in self.station_dir.rglob("*"):
            if path.is_file():
                self.assertNotIn(b"SQLite format 3", path.read_bytes()[:16], path.name)
        self.assertNotIn(MANAGER_PIN, (self.puller.base / "promotion.json").read_text(encoding="utf-8"))

    def test_cinco_erros_travam_por_cinco_minutos(self):
        self.replicate()
        for _ in range(4):
            with self.assertRaises(TerminalError) as caught:
                promotion.verify_manager(self.station_dir, "999999")
            self.assertEqual(caught.exception.status, 403)
        with self.assertRaises(TerminalError) as caught:
            promotion.verify_manager(self.station_dir, "999999")
        self.assertEqual((caught.exception.status, str(caught.exception)),
                         (429, "Muitas tentativas; aguarde cinco minutos"))
        with self.assertRaises(TerminalError) as caught:  # nem o PIN certo passa durante a espera
            promotion.verify_manager(self.station_dir, MANAGER_PIN)
        self.assertEqual(caught.exception.status, 429)
        locked = promotion.status(self.station_dir)["locked_until"]
        self.assertTrue(4 * MINUTE < locked - promotion._now_ms() <= 5 * MINUTE)
        with self.assertRaises(TerminalError) as caught:
            self.promote()
        self.assertEqual(caught.exception.status, 429)
        self.assertEqual(self.log, [])
        with patch.object(promotion, "_now_ms", return_value=locked + 1):
            self.assertIsNone(promotion.status(self.station_dir)["locked_until"])
            self.assertEqual(promotion.verify_manager(self.station_dir, MANAGER_PIN)["name"], "Bruno")

    def test_sem_copia_completa_da_409(self):
        with self.assertRaises(TerminalError) as caught:
            promotion.verify_manager(self.station_dir, MANAGER_PIN)
        self.assertEqual((caught.exception.status, str(caught.exception)),
                         (409, "Este caixa ainda não tem uma cópia completa do caixa principal. "
                               "Não é possível assumir."))
        # Só o diário, sem a cópia do banco: também não dá.
        self.replicate()
        for path in self.replica().glob("snapshot-*"):
            path.unlink()
        self.assertFalse(promotion.status(self.station_dir)["available"])
        with self.assertRaises(TerminalError) as caught:
            promotion.verify_manager(self.station_dir, MANAGER_PIN)
        self.assertEqual(caught.exception.status, 409)
        with self.assertRaises(TerminalError) as caught:
            self.promote()
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(self.log, [])

    def test_copia_danificada_da_409_sem_contar_como_erro_de_pin(self):
        self.replicate()
        for path in self.replica().glob("snapshot-*"):
            path.write_bytes(path.read_bytes()[:300])
        with self.assertRaises(TerminalError) as caught:
            promotion.verify_manager(self.station_dir, MANAGER_PIN)
        self.assertEqual(caught.exception.status, 409)
        self.assertFalse((self.puller.base / "promotion.json").exists())

    def test_replica_de_versao_mais_nova_da_409(self):
        with patch.dict(os.environ, {"PDV_VERSION": "9.4.0"}):
            self.replicate()
        manifest = json.loads((self.replica() / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["app_version"], "9.4.0")
        with patch.dict(os.environ, {"PDV_VERSION": "2.1.0"}):
            status = promotion.status(self.station_dir)
            self.assertEqual((status["available"], status["reason"]),
                             (False, "Atualize este caixa antes de assumir: o caixa principal usava uma versão "
                                     "mais nova."))
            with self.assertRaises(TerminalError) as caught:
                promotion.verify_manager(self.station_dir, MANAGER_PIN)
            self.assertEqual((caught.exception.status, str(caught.exception)), (409, status["reason"]))
            with self.assertRaises(TerminalError):
                self.promote()
        self.assertEqual(self.log, [])
        with patch.dict(os.environ, {"PDV_VERSION": "9.4.0"}):  # mesma versão: pode
            self.assertTrue(promotion.status(self.station_dir)["available"])
            promotion.verify_manager(self.station_dir, MANAGER_PIN)
        with patch.dict(os.environ, {"PDV_VERSION": "10.0.0"}):  # caixa adicional mais novo: pode
            promotion.verify_manager(self.station_dir, MANAGER_PIN)


    def test_versao_de_correcao_do_principal_nao_bloqueia_so_formato_de_banco_mais_novo(self):
        # O principal se atualiza sozinho de madrugada; o caixa adicional não.
        with patch.dict(os.environ, {"PDV_VERSION": "2.1.7"}):
            self.replicate()
        with patch.dict(os.environ, {"PDV_VERSION": "2.1.0"}):
            status = promotion.status(self.station_dir)
            self.assertEqual((status["available"], status["reason"]), (True, ""))
            self.assertEqual(promotion.verify_manager(self.station_dir, MANAGER_PIN)["operator_id"], "op-gerente")
            self.assertTrue(self.promote()["promoted"])
        for local, blocked in (("2.0.9", True), ("2.0", True), ("1.9.9", True), ("2.1", False), ("2.2.0", False)):
            with patch.dict(os.environ, {"PDV_VERSION": local}):
                self.assertEqual(promotion._newer(promotion._copy(self.station_dir)), blocked, local)


class PromoverTests(PromotionCase):
    def test_relogio_atrasado_em_relacao_ao_principal_recusa_antes_de_trocar_qualquer_coisa(self):
        # A marca contra volta de relógio viaja na cópia, no relógio do caixa principal.
        last_seen = int(promotion.time.time())
        with self.store.transaction() as conn:
            self.store._set_meta(conn, "last_seen", str(last_seen))
        self.replicate(before=2, after=1)
        old = self.station_dir / "terminal.sqlite3"
        old.write_bytes(b"banco que estava aqui antes")
        with patch.object(promotion.time, "time", return_value=last_seen - 7 * 60):
            with self.assertRaises(TerminalError) as caught:
                self.promote()
        self.assertEqual((caught.exception.status, caught.exception.code), (409, "clock_behind"))
        self.assertIn("relógio deste computador está atrasado", str(caught.exception))
        self.assertEqual([entry[0] for entry in self.log], [], "o banco nem chegou a ser aberto")
        self.assertEqual(old.read_bytes(), b"banco que estava aqui antes")
        self.assertFalse((self.station_dir / "terminal.sqlite3.promoting").exists())
        self.assertFalse(promotion.pending(self.station_dir))
        self.assertEqual(self.saved()["state"], "failed")
        # Dentro da tolerância da licença (5 min) e com o relógio certo, assume normalmente.
        with patch.object(promotion.time, "time", return_value=last_seen - 4 * 60):
            promotion._check_clock(self.store.path)
        self.assertTrue(self.promote()["promoted"])

    def test_passos_na_ordem_estado_e_troca_do_banco(self):
        later = self.replicate(before=2, after=3)
        snapshot = self.server_status()["snapshot"]
        old = self.station_dir / "terminal.sqlite3"
        old.write_bytes(b"banco que estava aqui antes")
        (self.station_dir / "active-profile.json").write_text('{"profile":"outra"}', encoding="utf-8")
        self.assertFalse(promotion.pending(self.station_dir))

        result = self.promote()
        self.assertEqual(result, {"promoted": True, "applied": 3, "journal_last_seq": 5,
                                  "snapshot_at": snapshot["created_at"], "previous_server_cash_open": True,
                                  "warnings": []})
        self.assertEqual(self.log, [
            ("open", "terminal.sqlite3.promoting", "replaying"),
            ("apply", later, "replaying"),
            ("finish", "caixa-2", "Caixa 2", self.key, "op-gerente", "finalizing")])
        saved = self.saved()
        self.assertEqual((saved["state"], saved["step"], saved["error"]), ("done", "", ""))
        self.assertTrue(saved["started_at"] <= saved["finished_at"])
        self.assertNotIn(self.key["key"], json.dumps(saved))
        self.assertTrue(promotion.pending(self.station_dir))
        status = promotion.status(self.station_dir)
        self.assertEqual((status["state"], status["available"], status["reason"]),
                         ("done", False, "Este computador já assumiu como caixa principal."))

        # O banco promovido está no lugar, em um arquivo só, íntegro e sem os segredos do antigo.
        promoted = self.station_dir / "terminal.sqlite3"
        with closing(sqlite3.connect(promoted)) as conn:
            self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            meta = dict(conn.execute("SELECT key,value FROM meta"))
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0], 2)
        self.assertEqual((meta["terminal_id"], meta["aplicado"]), (TERMINAL, "3"))
        self.assertNotIn("token", meta)
        self.assertNotIn("mirror_key", meta)
        self.assertNotIn(TOKEN.encode(), promoted.read_bytes())
        names = sorted(path.name for path in self.station_dir.iterdir())
        self.assertEqual([name for name in names if "promoting" in name or name.endswith(("-wal", "-shm"))], [])
        self.assertNotIn("active-profile.json", names)
        # O banco que existia aqui foi renomeado, nunca apagado.
        kept = [name for name in names if name.startswith("terminal-antes-da-promocao-")]
        self.assertEqual(len(kept), 1)
        self.assertEqual((self.station_dir / kept[0]).read_bytes(), b"banco que estava aqui antes")
        self.assertEqual(saved["result"]["previous_database"], kept[0])
        # A cópia guardada continua lá para o suporte.
        self.assertTrue(any(self.replica().glob("snapshot-*.pdv2snap")))

    def test_promocao_repetida_e_recusada_e_nao_mexe_no_banco(self):
        self.replicate()
        self.promote()
        promoted = (self.station_dir / "terminal.sqlite3").read_bytes()
        self.log.clear()
        with self.assertRaises(TerminalError) as caught:
            self.promote()
        self.assertEqual((caught.exception.status, str(caught.exception)),
                         (409, "Este computador já assumiu como caixa principal."))
        self.assertEqual(self.log, [])
        self.assertEqual((self.station_dir / "terminal.sqlite3").read_bytes(), promoted)
        self.assertEqual(list(self.station_dir.glob("terminal-antes-da-promocao-*")), [])
        self.assertEqual(self.saved()["state"], "done")
        self.assertTrue(promotion.pending(self.station_dir))

    def test_falha_em_cada_passo_apaga_o_promoting_e_deixa_o_caixa_adicional_intacto(self):
        self.replicate()
        old = self.station_dir / "terminal.sqlite3"
        old.write_bytes(b"intacto")
        before = sorted(path.name for path in self.replica().iterdir())
        for fail_at, status, text in (("open", 500, "Não foi possível assumir como caixa principal (RuntimeError)."),
                                      ("apply", 409, "O diário não pôde ser aplicado."),
                                      ("finish", 500, "Não foi possível assumir como caixa principal (RuntimeError).")):
            with self.assertRaises(TerminalError) as caught:
                self.promote(fail_at=fail_at)
            self.assertEqual((caught.exception.status, str(caught.exception)), (status, text), fail_at)
            self.assertNotIn("segredo-interno", str(caught.exception))
            self.assertEqual(list(self.station_dir.glob("*promoting*")), [], fail_at)
            self.assertEqual(old.read_bytes(), b"intacto")
            self.assertEqual(list(self.station_dir.glob("terminal-antes-da-promocao-*")), [])
            saved = self.saved()
            self.assertEqual((saved["state"], saved["error"]), ("failed", text))
            self.assertFalse(promotion.pending(self.station_dir))
            shown = promotion.status(self.station_dir)
            self.assertEqual((shown["state"], shown["error"], shown["available"]), ("failed", text, True))
            self.assertEqual(sorted(path.name for path in self.replica().iterdir()), before)
        # Depois das falhas, tentar de novo funciona.
        self.assertTrue(self.promote()["promoted"])
        self.assertTrue(promotion.pending(self.station_dir))

    def test_pin_errado_na_promocao_nao_abre_o_banco(self):
        self.replicate()
        with self.assertRaises(TerminalError) as caught:
            self.promote(pin=CASHIER_PIN)
        self.assertEqual(caught.exception.status, 403)
        self.assertEqual(self.log, [])
        self.assertEqual((self.saved()["state"], self.saved()["failures"]), ("failed", 1))
        self.assertFalse((self.station_dir / "terminal.sqlite3").exists())
        self.assertFalse(promotion.pending(self.station_dir))

    def test_uma_promocao_por_vez_e_trava_solta_no_fim(self):
        self.replicate()
        entered, release, results = threading.Event(), threading.Event(), []
        base = self.factory()

        class Slow(base):
            def apply_journal_events(self, events):
                entered.set()
                release.wait(10)
                return super().apply_journal_events(events)

        first = threading.Thread(target=lambda: results.append(self.promote(store_factory=Slow)))
        first.start()
        self.assertTrue(entered.wait(10))
        running = promotion.status(self.station_dir)
        self.assertEqual((running["state"], running["step"]), ("running", "replaying"))
        self.assertFalse(promotion.pending(self.station_dir))
        with self.assertRaises(TerminalError) as caught:
            self.promote()
        self.assertEqual((caught.exception.status, str(caught.exception)),
                         (409, "A promoção já está em andamento neste computador."))
        release.set()
        first.join(10)
        self.assertEqual([item["promoted"] for item in results], [True])
        self.assertTrue(promotion.pending(self.station_dir))

    def test_processo_que_caiu_no_meio_aparece_como_falha_e_a_sobra_e_limpa(self):
        self.replicate()
        saved = promotion._load(self.station_dir)
        saved.update(state="running", step="replaying", started_at=1)
        promotion._save(self.station_dir, saved)
        leftover = self.station_dir / "terminal.sqlite3.promoting"
        leftover.write_bytes(b"sobra")
        (self.station_dir / "terminal.sqlite3.promoting-wal").write_bytes(b"sobra")
        shown = promotion.status(self.station_dir)
        self.assertEqual((shown["state"], shown["step"]), ("failed", ""))
        self.assertIn("interrompida", shown["error"])
        self.assertFalse(promotion.pending(self.station_dir))
        self.assertTrue(self.promote()["promoted"])
        self.assertEqual(list(self.station_dir.glob("*promoting*")), [])

    def test_estado_done_sem_o_banco_no_lugar_nao_e_pendente(self):
        self.replicate()
        self.promote()
        os.unlink(self.station_dir / "terminal.sqlite3")
        self.assertFalse(promotion.pending(self.station_dir))
        self.assertEqual(promotion.status(self.station_dir)["state"], "idle")
        self.assertTrue(self.promote()["promoted"])

    def write_mode(self, mode, changed_at):
        (self.station_dir / "mode.json").write_text(
            json.dumps({"v": 1, "mode": mode, "changed_at": changed_at}), encoding="utf-8")

    def test_pendente_so_ate_o_modo_ser_gravado_depois_da_promocao(self):
        self.replicate()
        self.write_mode("station", self.saved_time() - 3600)  # de quando virou caixa adicional
        self.promote()
        finished = self.saved()["finished_at"]
        self.assertTrue(promotion.pending(self.station_dir))  # caiu antes de trocar o modo
        self.write_mode("server", finished // 1000)  # mesmo segundo: na dúvida continua pendente
        self.assertTrue(promotion.pending(self.station_dir))

        self.write_mode("server", finished // 1000 + 1)  # station.py concluiu a troca
        self.assertFalse(promotion.pending(self.station_dir))
        # Meses depois: outro computador virou o principal e este volta a ser caixa adicional.
        (self.station_dir / "active-profile.json").write_text('{"profile":"outro"}', encoding="utf-8")
        self.write_mode("station", finished // 1000 + 5000)
        self.assertFalse(promotion.pending(self.station_dir))
        status = promotion.status(self.station_dir)
        self.assertEqual((status["state"], status["available"]), ("idle", False))
        # A cópia com que ele já assumiu não pode passar por cima do banco promovido.
        self.assertIn("ainda não tem uma cópia completa", status["reason"])
        promoted = (self.station_dir / "terminal.sqlite3").read_bytes()
        for _ in range(2):  # a recusa não pode apagar a guarda (estado "failed" no meio)
            with self.assertRaises(TerminalError) as caught:
                self.promote()
            self.assertEqual(caught.exception.status, 409)
            self.assertIn("ainda não tem uma cópia completa", str(caught.exception))
        self.assertEqual((self.station_dir / "terminal.sqlite3").read_bytes(), promoted)
        self.assertFalse(promotion.pending(self.station_dir))

        # Voltou a falar com o principal: pode assumir de novo, e o banco anterior fica guardado.
        self.write_mode("station", self.saved_time())
        time.sleep(0.01)
        self.assertTrue(self.puller.cycle())
        status = promotion.status(self.station_dir)
        self.assertEqual((status["state"], status["available"], status["reason"]), ("failed", True, ""))
        self.assertTrue(self.promote()["promoted"])
        self.assertEqual(len(list(self.station_dir.glob("terminal-antes-da-promocao-*"))), 1)
        self.assertTrue(promotion.pending(self.station_dir))  # nova janela, até o modo ser gravado de novo

    def saved_time(self):
        return replica_net._now_ms() // 1000

    def test_copia_mais_recente_danificada_promove_com_a_anterior_e_avisa(self):
        self.events(2)
        self.sync()
        first = self.server_status()["snapshot"]
        middle = self.events(2)
        mirror.on_cash_closed(self.store)  # fechamento de caixa: o principal faz outra cópia
        self.server_status()
        second = self.wait_snapshot(newer_than=first["name"])
        self.assertTrue(self.puller.cycle())
        last = self.events(1)
        self.assertTrue(self.puller.cycle())
        self.assertEqual(len(list(self.replica().glob("snapshot-*"))), 2)
        newest = self.replica() / second["name"]
        newest.write_bytes(newest.read_bytes()[:-50])

        result = self.promote()
        self.assertEqual(result["snapshot_at"], first["created_at"])
        self.assertEqual(result["warnings"], [mirror.MSG_DAMAGED_SNAPSHOT])
        self.assertEqual(self.log[1][1], middle + last)  # tudo que veio depois da cópia que abriu
        self.assertEqual(result["applied"], 3)

    def test_copia_de_outro_banco_plantada_na_pasta_nao_e_promovida(self):
        self.replicate()
        impostor = self.make_store(self.root / "impostor" / "terminal.sqlite3", self.key, terminal="outro-caixa")
        with impostor.transaction() as conn:  # mesmo com um gerente de PIN conhecido lá dentro
            conn.execute("INSERT INTO operators(id,name,role,pin_hash,active) VALUES ('x','Intruso','owner',?,1)",
                         (self.manager_hash,))
        entry = mirror.make_snapshot(impostor, self.root / "saida")
        target = next(self.replica().glob("snapshot-*"))
        target.write_bytes((self.root / "saida" / entry["name"]).read_bytes())
        with self.assertRaises(TerminalError) as caught:
            self.promote()
        self.assertEqual(caught.exception.status, 409)
        self.assertFalse((self.station_dir / "terminal.sqlite3").exists())
        self.assertEqual(list(self.station_dir.glob("*promoting*")), [])


if __name__ == "__main__":
    unittest.main()
