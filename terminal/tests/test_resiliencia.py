"""Resilience of the offline terminal: quarantine, license renewal, lockouts and the worker."""

import json
import sqlite3
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from shared.licensing import generate_keypair, payload_digest, sign_lease
from shared.security import hash_pin
from terminal.app import create_app
from terminal.store import Store, TerminalError
from terminal.sync import Worker, _fit_batch, heartbeat_once, refresh_bootstrap, sync_once


class ResilienceCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "terminal.sqlite3"
        self.app = create_app({"TESTING": True, "DATA_DIR": self.temp.name,
                               "DB_PATH": self.path, "START_WORKER": False})
        self.store = self.app.extensions["terminal_store"]
        self.client = self.app.test_client()
        self.tenant_id, self.shop_id, self.terminal_id = (str(uuid.uuid4()) for _ in range(3))
        self.cashier_id, self.manager_id = (str(uuid.uuid4()) for _ in range(2))
        self.product_id = str(uuid.uuid4())
        self.private, self.public = generate_keypair()
        self.operators = [
            {"id": self.cashier_id, "name": "Ana", "role": "cashier", "pin_hash": hash_pin("123987")},
            {"id": self.manager_id, "name": "Bruno", "role": "manager", "pin_hash": hash_pin("654321")},
        ]
        self.products = [{"id": self.product_id, "sku": "ARROZ", "barcode": "7890000000002", "name": "Arroz",
                          "unit": "UN", "category": "Mercearia", "price_cents": 10000,
                          "quantity_milli": 50000, "active": True}]
        self.lease = self.signed()
        self.store.set_pairing({"token": "t" * 48, "tenant_id": self.tenant_id,
                                "shop_id": self.shop_id, "terminal_id": self.terminal_id,
                                "lease": self.lease, "public_key": self.public}, "http://127.0.0.1:8000")
        self.bootstrap = {"products": self.products, "operators": self.operators,
                          "lease": self.lease, "public_key": self.public}
        self.store.apply_bootstrap(self.bootstrap)
        self.cashier = {"id": self.cashier_id, "name": "Ana", "role": "cashier"}
        self.manager = {"id": self.manager_id, "name": "Bruno", "role": "manager"}

    def signed(self, operators=None, issued_offset=-5, lifetime=3600):
        now = int(time.time())
        claims = {"schema": 1, "tenant_id": self.tenant_id, "shop_id": self.shop_id,
                  "terminal_id": self.terminal_id, "device_id": self.store.identity()["device_id"],
                  "issued_at": now + issued_offset, "expires_at": now + issued_offset + lifetime,
                  "features": ["pdv"], "status": "active",
                  "operators_hash": payload_digest(self.operators if operators is None else operators)}
        return sign_lease(claims, self.private)

    def sell(self, request_id="venda-1", quantity=1):
        return self.store.sale(self.cashier, {
            "request_id": request_id, "items": [{"product_id": self.product_id, "quantity_milli": quantity * 1000}],
            "payments": [{"method": "cash", "amount_cents": 10000 * quantity}]})

    def audit_actions(self):
        with self.store.connection() as conn:
            return [row["action"] for row in conn.execute("SELECT action FROM audit ORDER BY created_at,rowid")]

    def pending_types(self):
        return [event["type"] for event in self.store.pending_events()]


class QuarantineTests(ResilienceCase):
    def test_quarantined_events_are_acknowledged_recorded_and_counted(self):
        self.store.open_cash(self.cashier_id, 0)
        self.sell()
        events = self.store.pending_events()
        ids = [event["event_id"] for event in events]
        response = {"accepted_event_ids": ids, "conflicts": [],
                    "quarantined": [{"event_id": ids[1], "reason": "rejected_event",
                                     "message": "Desconto acima do limite configurado pela administração"},
                                    {"event_id": "f" * 32, "reason": "rejected_event", "message": "de outro lote"},
                                    "lixo"]}
        with patch("terminal.sync._request", return_value=response):
            result = sync_once(self.store)
        self.assertEqual((result["sent"], result["remaining"]), (2, 0))
        self.assertEqual([row["event_id"] for row in result["quarantined"]], [ids[1]])
        self.assertEqual(self.store.pending_events(), [])
        status = self.store.sync_status()
        self.assertEqual(status["quarantined"], 1)
        self.assertFalse(status["last_sync_error"])
        row = self.store.quarantined_events()[0]
        self.assertEqual((row["event_id"], row["event_type"], row["aggregate_id"], row["reason"]),
                         (ids[1], "sale.completed", events[1]["aggregate_id"], "rejected_event"))
        self.assertIn("Desconto acima do limite", row["message"])
        self.assertEqual(self.audit_actions().count("sync.event_quarantined"), 1)
        conflict = self.store.sync_conflicts()[0]
        self.assertEqual((conflict["event_id"], conflict["reason"]), (ids[1], "rejected_event"))
        # The same report arriving again (lost answer, replay) is not counted twice.
        self.assertEqual(len(self.store.record_quarantined(response["quarantined"], events)), 1)
        self.assertEqual(self.store.sync_status()["quarantined"], 1)
        self.assertEqual(self.audit_actions().count("sync.event_quarantined"), 1)
        self.assertEqual(self.client.get("/api/session").json["sync"]["quarantined"], 1)
        exported = self.store.export_data()["quarantined_events"]
        self.assertEqual([(row["event"]["event_id"], row["status"]) for row in exported], [(ids[1], "open")])

    def test_cloud_without_quarantine_key_keeps_working(self):
        self.store.open_cash(self.cashier_id, 0)
        ids = [event["event_id"] for event in self.store.pending_events()]
        with patch("terminal.sync._request", return_value={"accepted_event_ids": ids}):
            self.assertEqual(sync_once(self.store), {"sent": 1, "remaining": 0})
        self.assertEqual(self.store.sync_status()["quarantined"], 0)
        self.assertEqual(self.store.quarantined_events(), [])

    def test_cloud_conflict_row_is_kept_and_resolving_it_clears_the_review_count(self):
        self.store.open_cash(self.cashier_id, 0)
        event = self.store.pending_events()[0]
        cloud_conflict = {"id": str(uuid.uuid4()), "event_id": event["event_id"], "event_type": event["type"],
                          "aggregate_id": event["aggregate_id"], "reason": "rejected_event", "status": "open",
                          "detail": {"error": "Valor esperado inválido"}}
        response = {"accepted_event_ids": [event["event_id"]], "conflicts": [cloud_conflict],
                    "quarantined": [{"event_id": event["event_id"], "reason": "rejected_event",
                                     "message": "Valor esperado inválido"}]}
        with patch("terminal.sync._request", return_value=response):
            sync_once(self.store)
        conflicts = self.store.sync_conflicts()
        self.assertEqual([row["id"] for row in conflicts], [cloud_conflict["id"]])
        self.assertEqual(self.store.sync_status()["quarantined"], 1)
        resolved = self.store.resolve_sync_conflict(cloud_conflict["id"], "resolved", "Lançado manualmente no painel")
        self.assertEqual((resolved[0]["status"], resolved[0]["resolution"]), ("resolved", "Lançado manualmente no painel"))
        status = self.store.sync_status()
        self.assertEqual((status["quarantined"], status["conflicts"]), (0, 0))
        self.assertEqual(self.store.quarantined_events("resolved")[0]["event_id"], event["event_id"])

    def test_database_created_before_resolution_column_is_migrated(self):
        old_path = Path(self.temp.name) / "antigo.sqlite3"
        with sqlite3.connect(old_path) as conn:
            conn.execute("""CREATE TABLE sync_conflicts(
                id TEXT PRIMARY KEY,event_id TEXT NOT NULL UNIQUE,event_type TEXT NOT NULL,
                aggregate_id TEXT NOT NULL,reason TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'open',
                detail TEXT NOT NULL,created_at INTEGER NOT NULL,resolved_at INTEGER)""")
            conn.execute("INSERT INTO sync_conflicts VALUES ('c1','e1','sale.completed','s1','negative_stock','open','{}',1,NULL)")
        old = Store(old_path)
        self.assertEqual(old.resolve_sync_conflict("c1", "ignored", "Conferido")[0]["resolution"], "Conferido")


class SyncErrorTests(ResilienceCase):
    def test_successful_heartbeat_clears_stale_error_when_queue_is_empty(self):
        self.store.set_sync_result("Sem conexão com o servidor; dados locais preservados")
        with patch("terminal.sync._request", return_value={"ok": True, "server_time": int(time.time())}):
            heartbeat_once(self.store)
        self.assertFalse(self.store.sync_status()["last_sync_error"])
        self.assertFalse(self.client.get("/api/session").json["sync"]["last_sync_error"])

    def test_successful_bootstrap_clears_stale_error(self):
        self.store.set_sync_result("Sem conexão com o servidor; dados locais preservados")
        with patch("terminal.sync._request", side_effect=[dict(self.bootstrap, next_cursor=None),
                                                           {"release": None, "public_key": self.public}]):
            refresh_bootstrap(self.store)
        self.assertFalse(self.store.sync_status()["last_sync_error"])

    def test_failed_contact_keeps_error_and_pending_events_need_their_own_delivery(self):
        self.store.set_sync_result("Sem conexão com o servidor; dados locais preservados")
        with patch("terminal.sync._request", side_effect=TerminalError("Sem conexão", 503)):
            with self.assertRaises(TerminalError):
                heartbeat_once(self.store)
        self.assertTrue(self.store.sync_status()["last_sync_error"])
        self.store.open_cash(self.cashier_id, 0)
        with patch("terminal.sync._request", return_value={"ok": True}):
            heartbeat_once(self.store)
        self.assertTrue(self.store.sync_status()["last_sync_error"])  # the queue has not been delivered yet
        ids = [event["event_id"] for event in self.store.pending_events()]
        with patch("terminal.sync._request", return_value={"accepted_event_ids": ids}):
            sync_once(self.store)
        self.assertFalse(self.store.sync_status()["last_sync_error"])

    def test_worker_pass_with_working_cloud_leaves_no_error_behind(self):
        self.store.set_sync_result("Heartbeat: Sem conexão com o servidor; dados locais preservados")

        def cloud(method, url, **kwargs):
            if url.endswith("/heartbeat"):
                return {"ok": True}
            if "/bootstrap" in url:
                return dict(self.bootstrap, next_cursor=None)
            return {"release": None, "public_key": self.public}

        with self.store.transaction() as conn:
            self.store._set_meta(conn, "bootstrap_at", "0")
        with patch("terminal.sync._request", side_effect=cloud):
            self.assertEqual(Worker(self.store).run_once(), [])
        self.assertFalse(self.store.sync_status()["last_sync_error"])


class ManagerLockoutTests(ResilienceCase):
    def test_bootstrap_does_not_give_back_manager_pin_attempts(self):
        for _ in range(5):
            with self.assertRaisesRegex(TerminalError, "PIN de gerente inválido"):
                self.store.manager_override("000000")
        with self.assertRaisesRegex(TerminalError, "Muitas tentativas"):
            self.store.manager_override("654321")
        self.store.apply_bootstrap(self.bootstrap)
        with patch("terminal.sync._request", side_effect=[dict(self.bootstrap, next_cursor=None),
                                                           {"release": None, "public_key": self.public}]):
            refresh_bootstrap(self.store)
        with self.assertRaises(TerminalError) as blocked:
            self.store.manager_override("654321")
        self.assertEqual(blocked.exception.status, 429)

    def test_bootstrap_keeps_operator_lock_until_the_pin_changes(self):
        for _ in range(5):
            with self.assertRaises(TerminalError):
                self.store.login(self.cashier_id, "000000")
        self.store.apply_bootstrap(self.bootstrap)
        with self.assertRaisesRegex(TerminalError, "Muitas tentativas"):
            self.store.login(self.cashier_id, "123987")
        changed = [dict(self.operators[0], pin_hash=hash_pin("112233")), self.operators[1]]
        self.store.apply_bootstrap(dict(self.bootstrap, operators=changed, lease=self.signed(changed)))
        self.assertEqual(self.store.login(self.cashier_id, "112233")["id"], self.cashier_id)

    def test_pin_is_verified_without_holding_the_database_write_lock(self):
        from shared.security import verify_pin

        def verify_while_writing(pin, encoded):
            # Another request (a sale, the worker) must be able to write meanwhile.
            with sqlite3.connect(self.path, timeout=0.2) as other:
                other.execute("BEGIN IMMEDIATE")
                other.rollback()
            return verify_pin(pin, encoded)

        with patch("shared.security.verify_pin", side_effect=verify_while_writing):
            self.assertEqual(self.store.login(self.cashier_id, "123987")["id"], self.cashier_id)
            self.assertEqual(self.store.manager_override("654321"), self.manager_id)
            with self.assertRaisesRegex(TerminalError, "PIN de gerente inválido"):
                self.store.manager_override("000000")


class LicenseRenewalTests(ResilienceCase):
    def test_product_name_up_to_cloud_limit_is_accepted(self):
        long_name = "Camiseta " + "x" * 171  # 180 characters, the cloud limit
        products = [self.products[0], dict(self.products[0], id=str(uuid.uuid4()), barcode="", sku="GRADE", name=long_name)]
        result = self.store.apply_bootstrap(dict(self.bootstrap, products=products))
        self.assertNotIn("skipped", result)
        self.assertEqual(self.store.products("GRADE")[0]["name"], long_name)
        self.assertEqual(self.store.sync_status()["catalog_skipped"], 0)

    def test_invalid_catalog_row_is_skipped_and_license_is_still_renewed(self):
        renewed = self.signed(issued_offset=0, lifetime=72 * 3600)
        bad_id = str(uuid.uuid4())
        products = [dict(self.products[0], price_cents=12345),
                    dict(self.products[0], id=bad_id, barcode="", sku="RUIM", unit="CX")]
        response = dict(self.bootstrap, products=products, lease=renewed,
                        customers=[{"id": str(uuid.uuid4()), "name": "Dona Maria", "credit_limit_cents": 1000},
                                   {"id": str(uuid.uuid4()), "name": "", "credit_limit_cents": 0}],
                        promotions=[{"id": str(uuid.uuid4()), "name": "Nova regra", "kind": "combo"}],
                        payment_methods=[{"id": "cash", "kind": "cash", "name": "Dinheiro", "active": True},
                                         {"id": "x", "kind": "cripto", "name": "Moeda", "active": True}])
        result = self.store.apply_bootstrap(response)
        self.assertEqual(result["skipped"], 4)
        self.assertEqual(self.store.lease_status()["expires_at"], renewed["payload"]["expires_at"])
        self.assertEqual(self.store.products("ARROZ")[0]["price_cents"], 12345)
        self.assertEqual(self.store.products("RUIM"), [])
        self.assertEqual([row["name"] for row in self.store.customers()], ["Dona Maria"])
        self.assertEqual([row["kind"] for row in self.store.payment_methods()], ["cash"])
        skipped = self.store.catalog_skipped()
        self.assertEqual({row["kind"] for row in skipped}, {"product", "customer", "promotion", "payment_method"})
        self.assertIn({"kind": "product", "id": bad_id, "error": "Unidade de produto inválida"}, skipped)
        self.assertEqual(self.store.sync_status()["catalog_skipped"], 4)
        self.assertEqual(self.audit_actions().count("terminal.catalog_skipped"), 4)
        # The same problem reported every 30 seconds is audited once.
        self.store.apply_bootstrap(response)
        self.assertEqual(self.audit_actions().count("terminal.catalog_skipped"), 4)
        with patch("terminal.sync._request", return_value={"ok": True}) as send:
            heartbeat_once(self.store)
        self.assertIn("4 item(ns) ignorado(s)", send.call_args.kwargs["json"]["last_error"])
        self.store.apply_bootstrap(self.bootstrap)
        self.assertEqual(self.store.sync_status()["catalog_skipped"], 0)
        # The sale still works with the rows that were valid.
        self.store.open_cash(self.cashier_id, 0)
        self.assertEqual(self.sell()["total_cents"], 10000)

    def test_malformed_catalog_still_renews_license_and_operators(self):
        renewed = self.signed(issued_offset=0, lifetime=72 * 3600)
        with self.assertRaisesRegex(TerminalError, "Catálogo"):
            self.store.apply_bootstrap(dict(self.bootstrap, products=None, lease=renewed))
        self.assertEqual(self.store.lease_status()["expires_at"], renewed["payload"]["expires_at"])
        self.assertEqual(len(self.store.products()), 1)

    def test_tampered_operators_never_renew_the_license(self):
        renewed = self.signed(issued_offset=0, lifetime=72 * 3600)
        tampered = [dict(self.operators[0], pin_hash=hash_pin("111111")), self.operators[1]]
        with self.assertRaisesRegex(TerminalError, "operadores"):
            self.store.apply_bootstrap(dict(self.bootstrap, operators=tampered, lease=renewed))
        self.assertEqual(self.store.lease_status()["expires_at"], self.lease["payload"]["expires_at"])

    def test_barcode_repeated_in_one_load_keeps_both_products(self):
        twin = dict(self.products[0], id=str(uuid.uuid4()), sku="GEMEO", name="Arroz integral")
        result = self.store.apply_bootstrap(dict(self.bootstrap, products=[self.products[0], twin]))
        self.assertEqual(result["skipped"], 1)
        self.assertEqual(self.store.products(self.products[0]["barcode"])[0]["id"], self.product_id)
        self.assertEqual(self.store.products("GEMEO")[0]["barcode"], None)

    def test_repeated_bootstrap_is_audited_only_when_something_changes(self):
        for _ in range(3):
            self.store.apply_bootstrap(self.bootstrap)
        self.assertEqual(self.audit_actions().count("terminal.bootstrap"), 1)
        second = dict(self.products[0], id=str(uuid.uuid4()), barcode="", sku="NOVO")
        self.store.apply_bootstrap(dict(self.bootstrap, products=[self.products[0], second]))
        self.assertEqual(self.audit_actions().count("terminal.bootstrap"), 2)

    def test_fresh_lease_releases_cashier_after_clock_was_ahead_and_got_fixed(self):
        with self.store.transaction() as conn:
            self.store._set_meta(conn, "last_seen", str(int(time.time()) + 24 * 3600))
        self.assertEqual(self.store.lease_status()["state"], "clock_rollback")
        # Replaying the lease already stored proves nothing about the current time.
        self.store.apply_bootstrap(self.bootstrap)
        self.assertEqual(self.store.lease_status()["state"], "clock_rollback")
        self.store.apply_bootstrap(dict(self.bootstrap, lease=self.signed(issued_offset=0)))
        self.assertEqual(self.store.lease_status()["state"], "active")
        self.assertIn("terminal.clock_corrected", self.audit_actions())


class CashAndQueueTests(ResilienceCase):
    def test_void_after_withdrawal_closes_cash_with_signed_negative_expected(self):
        self.store.open_cash(self.cashier_id, 0)
        sale = self.sell()
        self.store.cash_movement(self.cashier_id, "withdrawn", 10000, "Sangria do meio-dia")
        voided = self.store.void_sale(self.manager, sale["id"], "Cliente desistiu")
        self.assertEqual(voided["status"], "voided")
        self.assertEqual(self.store.cash_summary()["expected_cents"], -10000)
        self.assertEqual(self.client.get("/api/session").status_code, 200)
        with self.assertRaisesRegex(TerminalError, "Sangria excede"):
            self.store.cash_movement(self.cashier_id, "withdrawn", 1, "Sem dinheiro")
        other = self.sell("venda-2")
        with self.assertRaisesRegex(TerminalError, "insuficiente"):
            self.store.return_sale(self.manager, other["id"], {
                "return_id": str(uuid.uuid4()), "refund_method": "cash", "reason": "Avaria",
                "items": [{"product_id": self.product_id, "quantity_milli": 1000}]})
        self.store.void_sale(self.manager, other["id"], "Cancelada")
        closed = self.store.close_cash(self.cashier_id, 0, "654321")
        self.assertEqual((closed["expected_cents"], closed["difference_cents"]), (-10000, 10000))
        event = self.store.pending_events()[-1]
        self.assertEqual(event["type"], "cash.closed")
        self.assertEqual((event["data"]["expected_cents"], event["data"]["counted_cents"],
                          event["data"]["difference_cents"]), (-10000, 0, 10000))
        self.assertEqual(self.store.export_data()["cash_sessions"][0]["expected_cents"], -10000)
        self.assertIsNone(self.store.cash_state())
        self.assertEqual(self.store.open_cash(self.cashier_id, 5000)["opening_cents"], 5000)

    def test_outbox_follows_insertion_order_when_the_clock_goes_back(self):
        ahead = time.time_ns() // 1_000_000 + 180_000
        with patch("terminal.store._now_ms", return_value=ahead):
            self.store.open_cash(self.cashier_id, 0)
            sale = self.sell()
        # Windows corrected the clock three minutes back before the cancellation.
        with patch("terminal.store._now_ms", return_value=ahead - 180_000):
            self.store.void_sale(self.manager, sale["id"], "Cliente desistiu")
        self.assertEqual(self.pending_types(), ["cash.opened", "sale.completed", "sale.voided"])
        self.assertEqual([event["type"] for event in self.store.export_data()["pending_events"]],
                         ["cash.opened", "sale.completed", "sale.voided"])
        # The stock restored by the void is preserved over the cloud balance.
        self.store.apply_bootstrap(self.bootstrap)
        self.assertEqual(self.store.products("ARROZ")[0]["stock_milli"], 50000)

    def test_sale_respects_cloud_limits_for_items_and_payments(self):
        self.store.open_cash(self.cashier_id, 0)
        items = [{"product_id": str(uuid.uuid4()), "quantity_milli": 1000} for _ in range(201)]
        with self.assertRaisesRegex(TerminalError, "até 200 produtos"):
            self.store.sale(self.cashier, {"request_id": "grande", "items": items,
                                           "payments": [{"method": "cash", "amount_cents": 100}]})
        with self.assertRaisesRegex(TerminalError, "até 20 pagamentos"):
            self.store.sale(self.cashier, {
                "request_id": "parcelada", "items": [{"product_id": self.product_id, "quantity_milli": 1000}],
                "payments": [{"method": "cash", "amount_cents": 500}] * 21})
        self.assertEqual(self.store.sales(), [])
        twenty = [{"method": "cash", "amount_cents": 500}] * 20
        self.assertEqual(self.store.sale(self.cashier, {
            "request_id": "limite", "items": [{"product_id": self.product_id, "quantity_milli": 1000}],
            "payments": twenty})["total_cents"], 10000)

    def test_batch_is_halved_when_the_cloud_answers_too_large(self):
        self.store.open_cash(self.cashier_id, 0)
        for index in range(3):
            self.store.cash_movement(self.cashier_id, "supplied", 100, f"Troco {index}")
        ids = [event["event_id"] for event in self.store.pending_events()]
        too_large = TerminalError("Servidor recusou pedido (413): Corpo muito grande", 502)
        too_large.remote_status = 413
        with patch("terminal.sync._request", side_effect=[too_large, {"accepted_event_ids": ids[:2]}]) as send:
            self.assertEqual(sync_once(self.store), {"sent": 2, "remaining": 2})
        self.assertEqual([event["event_id"] for event in send.call_args.kwargs["json"]["events"]], ids[:2])
        self.assertEqual([event["event_id"] for event in self.store.pending_events()], ids[2:])
        events = self.store.pending_events()
        self.assertEqual(_fit_batch(events, limit=1), events[:1])
        self.assertEqual(_fit_batch(events, limit=len(json.dumps(events[0])) + 2), events[:1])
        self.assertEqual(_fit_batch(events), events)
        self.assertEqual(_fit_batch([]), [])


class WorkerTests(ResilienceCase):
    def test_step_failure_of_any_kind_does_not_stop_the_other_steps(self):
        self.store.open_cash(self.cashier_id, 0)
        ids = [event["event_id"] for event in self.store.pending_events()]
        with patch("terminal.sync.heartbeat_once", side_effect=sqlite3.OperationalError("database is locked")), \
             patch("terminal.sync.refresh_bootstrap", side_effect=KeyError("lease")), \
             patch("terminal.sync._request", return_value={"accepted_event_ids": ids}):
            errors = Worker(self.store).run_once()
        self.assertEqual(len(errors), 1)  # bootstrap ran less than 30 seconds ago
        self.assertIn("database is locked", errors[0])
        self.assertEqual(self.store.pending_events(), [])

    def test_loop_survives_unexpected_errors_and_dead_thread_is_revived(self):
        worker = Worker(self.store, interval=0.01)
        with patch.object(Worker, "run_once", side_effect=RuntimeError("inesperado")) as step:
            worker.start()
            deadline = time.time() + 5
            while step.call_count < 2 and time.time() < deadline:
                time.sleep(0.01)
            self.assertGreaterEqual(step.call_count, 2)
            self.assertTrue(worker.thread.is_alive())
            self.assertFalse(worker.ensure_running())
            worker.stop()
        self.assertFalse(worker.ensure_running())  # stopped on purpose

        revived = Worker(self.store, interval=0.01)
        self.assertFalse(revived.ensure_running())  # never started
        revived.thread = threading.Thread(target=lambda: None)
        revived.thread.start()
        revived.thread.join()
        with patch.object(Worker, "run_once", return_value=[]):
            self.assertTrue(revived.ensure_running())
            self.assertTrue(revived.thread.is_alive())
            revived.stop()
        self.assertFalse(revived.thread.is_alive())

    def test_automatic_backup_waits_for_first_local_record_so_wrong_pairing_can_be_undone(self):
        fresh = Store(Path(self.temp.name) / "novo" / "terminal.sqlite3")
        claims = dict(self.lease["payload"], device_id=fresh.identity()["device_id"])
        fresh.set_pairing({"token": "n" * 48, "tenant_id": self.tenant_id, "shop_id": self.shop_id,
                           "terminal_id": self.terminal_id, "lease": sign_lease(claims, self.private),
                           "public_key": self.public}, "http://127.0.0.1:8000")
        with patch("terminal.sync._request", side_effect=TerminalError("Sem conexão", 503)):
            Worker(fresh).run_once()
        self.assertEqual(fresh.backup_status()["copies"], [])
        self.assertEqual(fresh.unpair(), {"paired": False})
        # With business records the daily copy is made as before.
        self.store.open_cash(self.cashier_id, 0)
        with patch("terminal.sync._request", side_effect=TerminalError("Sem conexão", 503)):
            Worker(self.store).run_once()
        self.assertEqual(len(self.store.backup_status()["copies"]), 1)


if __name__ == "__main__":
    unittest.main()
