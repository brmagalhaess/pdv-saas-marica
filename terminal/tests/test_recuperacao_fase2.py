"""Fase 2 no terminal: "Recuperar minha loja", caixa transferido, evento terminal.config
e os ganchos da cópia em segundo disco. A nuvem aqui é falsa e segue o contrato da fase 2."""

import copy
import json
import shutil
import subprocess
import sys
import tempfile
import time
import types
import unittest
import uuid
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

import terminal
from shared.licensing import generate_keypair, payload_digest, sign_lease
from shared.security import hash_pin
from terminal import sync
from terminal.app import create_app
from terminal.store import RESTORE_BLOCKED, Store, TerminalError

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "terminal" / "static"
PIN_HASHES = {pin: hash_pin(pin) for pin in ("123987", "654321")}
MIRROR_KEY = {"key_id": "a1b2c3d4e5f60718", "key": "k" * 43 + "="}
CAPABILITIES = ["restore", "send-only", "terminal-config", "mirror-key"]
_MISSING = object()


@contextmanager
def mirror_module(fake):
    """Make `terminal.mirror` be `fake` (None: this build has no second-disk copy)."""
    previous = getattr(terminal, "mirror", _MISSING)
    with patch.dict(sys.modules, {"terminal.mirror": fake}), patch.object(sync, "mirror", fake):
        if fake is None:
            if previous is not _MISSING:
                delattr(terminal, "mirror")
        else:
            terminal.mirror = fake
        try:
            yield fake
        finally:
            if previous is _MISSING:
                if hasattr(terminal, "mirror"):
                    delattr(terminal, "mirror")
            else:
                terminal.mirror = previous


def remote_error(status, body):
    """What sync._json_response raises for an HTTP error of the cloud."""
    error = TerminalError(f"Servidor recusou pedido ({status}): {body.get('error')}", 502)
    error.remote_status, error.remote_code, error.remote_body = status, body.get("code"), body
    return error


class FakeMirror(types.SimpleNamespace):
    """Records the hooks the terminal calls, with the interface of the contract."""

    def __init__(self, enabled=True, pending=None):
        super().__init__(calls=[], enabled=enabled, pending=pending or [], explode=False)

    def _note(self, name, *args):
        self.calls.append((name, *args))
        if self.explode:
            raise RuntimeError("disco arrancado")

    def on_event(self, store):
        self._note("on_event", len(store.pending_events(500)))

    def on_cash_closed(self, store):
        self._note("on_cash_closed")

    def is_enabled(self, store):
        return self.enabled

    def start(self, store):
        self._note("start", str(store.path))
        return types.SimpleNamespace(stop=lambda timeout=5.0: self.calls.append(("stop",)))

    def status(self, store):
        return {"supported": True, "enabled": self.enabled, "directory": "/mnt/pendrive", "state": "ok",
                "message": "Cópia de segurança em dia", "last_snapshot_at": 1, "last_journal_at": 1,
                "journal_seq": 3, "pending_events": 0, "last_error": "", "key_id": MIRROR_KEY["key_id"]}

    def drives(self, store):
        return [{"directory": "/mnt/pendrive", "label": "pendrive · 12 GB livres", "free_bytes": 12 * 10**9,
                 "removable": True, "same_volume": False}]

    def configure(self, store, directory, allow_same_volume=False):
        self._note("configure", directory, allow_same_volume)
        self.enabled = directory is not None
        return self.status(store)

    def snapshot_now(self, store):
        self._note("snapshot_now")
        return {"name": "snapshot-1.pdv2snap", "created_at": 1, "size_bytes": 10, "outbox_seq": 3}

    def candidates(self, identity, directory=None):
        self._note("candidates", directory)
        return [{"directory": directory or "/mnt/pendrive", "replica_id": "antigo", "terminal_id": identity["terminal_id"],
                 "terminal_name": "Caixa 01", "shop_name": "Loja", "updated_at": 1, "journal_seq": 3,
                 "last_snapshot_at": 1, "matches": True, "current_device": False}]

    def read_pending(self, directory, key, identity, replica_id=None):
        self._note("read_pending", directory, key, replica_id)
        return {"terminal_id": identity["terminal_id"], "replica_id": replica_id or "antigo", "snapshot": None,
                "journal_last_seq": len(self.pending), "events": self.pending, "warnings": []}


class FakeCloud:
    """The cloud side of the phase 2 contract, small enough to read: it keeps what the
    terminal sent and gives it back through the paged restore."""

    def __init__(self, case, page_size=2):
        self.case, self.page_size = case, page_size
        self.event_ids, self.events, self.calls = set(), [], []
        self.config, self.sessions, self.sales, self.receipts = None, {}, {}, {}
        self.fail_restore_calls, self.restore_calls = set(), 0
        self.totals_patch, self.refuse_cursors, self.transferred, self.send_only = None, 0, None, None

    # ----- what the real cloud derives from the events -----
    def ingest(self, events):
        for event in events:
            if event["event_id"] in self.event_ids:
                continue
            self.event_ids.add(event["event_id"])
            self.events.append(event)
            kind, key, data = event["type"], event["aggregate_id"], event["data"]
            if kind == "cash.opened":
                self.sessions[key] = {"kind": "cash_session", "id": key, "operator_id": data["operator_id"],
                                      "opening_cents": data["opening_cents"], "opened_at": data["created_at"],
                                      "closed_at": None, "counted_cents": None, "expected_cents": None,
                                      "incomplete": False, "movements": []}
            elif kind in ("cash.withdrawn", "cash.supplied"):
                self.sessions[data["cash_session_id"]]["movements"].append(
                    {"id": key, "kind": kind.split(".")[1], "amount_cents": data["amount_cents"],
                     "reason": data["reason"], "operator_id": data["operator_id"], "created_at": data["created_at"]})
            elif kind == "cash.closed":
                self.sessions[key].update(closed_at=data["created_at"], counted_cents=data["counted_cents"],
                                          expected_cents=data["expected_cents"])
            elif kind == "sale.completed":
                self.sales[key] = {"kind": "sale", "id": key, "event_id": event["event_id"], "status": "completed",
                                   "voided_at": None, "void_reason": "", "data": data, "returns": []}
            elif kind == "sale.voided":
                self.sales[key].update(status="voided", voided_at=data["created_at"], void_reason=data["reason"])
            elif kind == "sale.returned":
                self.sales[data["sale_id"]]["returns"].append(
                    {"id": key, "refund_method": data["refund_method"], "total_cents": data["total_cents"],
                     "reason": data["reason"], "operator_id": data["operator_id"],
                     "cash_session_id": data.get("cash_session_id", ""), "created_at": data["created_at"],
                     "origin": "terminal", "items": data["items"]})
            elif kind == "credit.received":
                self.receipts[key] = {"kind": "credit_receipt", "id": key, "customer_id": data["customer_id"],
                                      "cash_session_id": data["cash_session_id"], "method": data["method"],
                                      "amount_cents": data["amount_cents"], "operator_id": data["operator_id"],
                                      "created_at": data["created_at"]}
            elif kind == "terminal.config":
                if data["updated_at"] >= ((self.config or {}).get("updated_at") or 0):
                    self.config = data

    def records(self):
        rows = [{"kind": "config", "data": self.config}] if self.config else []
        return copy.deepcopy(rows + list(self.sessions.values()) + list(self.sales.values())
                             + list(self.receipts.values()))

    @staticmethod
    def totals(records):
        sales = [row for row in records if row["kind"] == "sale"]
        returns = [ret for row in sales for ret in row["returns"]]
        receipts = [row for row in records if row["kind"] == "credit_receipt"]
        sessions = [row for row in records if row["kind"] == "cash_session"]
        movements = [move for row in sessions for move in row["movements"]]
        return {"sales": {"count": len(sales), "total_cents": sum(row["data"]["total_cents"] for row in sales),
                          "voided_count": sum(row["status"] == "voided" for row in sales)},
                "returns": {"count": len(returns), "total_cents": sum(row["total_cents"] for row in returns)},
                "credit_receipts": {"count": len(receipts),
                                    "total_cents": sum(row["amount_cents"] for row in receipts)},
                "cash_sessions": {"count": len(sessions), "stale_open_omitted": 0,
                                  "open_count": sum(row["closed_at"] is None for row in sessions)},
                "cash_movements": {"count": len(movements),
                                   "total_cents": sum(row["amount_cents"] for row in movements)},
                "config": {"count": sum(row["kind"] == "config" for row in records)}}

    def restore_page(self, cursor):
        records = self.records()
        offset = int(cursor[1:]) if cursor else 0
        chunk = records[offset:offset + self.page_size]
        end = offset + len(chunk)
        done = end >= len(records)
        totals = self.totals(records) if done else None
        if done and self.totals_patch:
            self.totals_patch(totals)
        return {"records": chunk, "next_cursor": None if done else f"c{end}", "done": done,
                "progress": {"position": end, "total": len(records)}, "cutoff": 0, "history_days": 90,
                "server_time": int(time.time()), "totals": totals}

    # ----- the HTTP surface, used in place of sync._request -----
    def request(self, method, url, **kwargs):
        parsed = urlparse(url)
        route = parsed.path.rsplit("/", 1)[-1]
        self.calls.append(route)
        if self.transferred is not None and route in ("lease", "bootstrap", "heartbeat", "restore"):
            raise remote_error(403, self.transferred)
        if route == "restore":
            self.restore_calls += 1
            if self.restore_calls in self.fail_restore_calls:
                raise TerminalError("Sem conexão com o servidor; dados locais preservados", 503)
            cursor = parse_qs(parsed.query).get("cursor", [""])[0]
            if cursor and self.refuse_cursors:
                self.refuse_cursors -= 1
                raise remote_error(400, {"error": "Cursor inválido", "code": "invalid_cursor"})
            self.case.restore_cursors.append(cursor)
            return self.restore_page(cursor)
        if route == "sync":
            if self.transferred is not None and self.send_only is None:
                raise remote_error(403, self.transferred)
            events = kwargs["json"]["events"]
            self.ingest(events)
            answer = {"accepted_event_ids": [event["event_id"] for event in events], "conflicts": [],
                      "send_only": self.send_only is not None}
            if self.send_only is not None:
                answer["send_only_until"] = self.send_only
            return answer
        if route == "bootstrap":
            return self.case.bootstrap_answer(self.case.store)
        if route == "heartbeat":
            return {"ok": True}
        if route == "lease":
            return self.case.lease_answer(self.case.store)
        if route == "current":  # release metadata
            return {"release": None}
        raise AssertionError("rota inesperada: " + url)


class RecoveryCase(unittest.TestCase):
    """An old computer that sold, a cloud that received it, and a new computer recovering the shop."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        hidden = mirror_module(None)  # each test says when a second-disk copy exists
        hidden.__enter__()
        self.addCleanup(hidden.__exit__, None, None, None)
        self.tenant_id, self.shop_id, self.terminal_id = (str(uuid.uuid4()) for _ in range(3))
        self.cashier_id, self.manager_id, self.customer_id = (str(uuid.uuid4()) for _ in range(3))
        self.product_id = str(uuid.uuid4())
        self.private, self.public = generate_keypair()
        self.operators = [
            {"id": self.cashier_id, "name": "Ana", "role": "cashier", "pin_hash": PIN_HASHES["123987"]},
            {"id": self.manager_id, "name": "Bruno", "role": "manager", "pin_hash": PIN_HASHES["654321"]}]
        self.cashier = {"id": self.cashier_id, "name": "Ana", "role": "cashier"}
        self.manager = {"id": self.manager_id, "name": "Bruno", "role": "manager"}
        self.products = [{"id": self.product_id, "sku": "ARROZ", "barcode": "7890000000002", "name": "Arroz",
                          "unit": "UN", "category": "Mercearia", "price_cents": 1000, "quantity_milli": 50000,
                          "active": True}]
        self.customers = [{"id": self.customer_id, "name": "Dona Rosa", "credit_limit_cents": 50000,
                           "open_credit_cents": 0, "store_credit_cents": 0}]
        self.restore_cursors = []
        self.cloud = FakeCloud(self)
        self.old = self.paired_store("antigo")
        self.store = self.old

    # ----- helpers -----
    def lease(self, store):
        issued = int(time.time()) - 5
        return sign_lease({"schema": 1, "tenant_id": self.tenant_id, "shop_id": self.shop_id,
                           "terminal_id": self.terminal_id, "device_id": store.identity()["device_id"],
                           "issued_at": issued, "expires_at": issued + 3600, "features": ["pdv"],
                           "status": "active", "operators_hash": payload_digest(self.operators)}, self.private)

    def pairing_answer(self, store, recovery=False, **extra):
        return {"token": "t" * 48, "tenant_id": self.tenant_id, "shop_id": self.shop_id,
                "terminal_id": self.terminal_id, "lease": self.lease(store), "public_key": self.public,
                "terminal": {"name": "Caixa 01"}, "shop": {"name": "Loja"}, "tenant": {"name": "Mercearia"},
                "recovery": recovery, "history_days": 90, "capabilities": CAPABILITIES,
                "mirror_key": MIRROR_KEY, **extra}

    def bootstrap_answer(self, store):
        return {"products": self.products, "operators": self.operators, "customers": self.customers,
                "lease": self.lease(store), "public_key": self.public, "capabilities": CAPABILITIES}

    def lease_answer(self, store):
        lease = self.lease(store)
        return {"lease": lease, "public_key": self.public, "operators": self.operators,
                "server_time": lease["payload"]["issued_at"], "capabilities": CAPABILITIES,
                "mirror_key": MIRROR_KEY}

    def paired_store(self, name, recovery=False):
        store = Store(Path(self.temp.name) / name / "terminal.sqlite3")
        store.set_pairing(self.pairing_answer(store, recovery), "http://127.0.0.1:8000")
        store.apply_bootstrap(self.bootstrap_answer(store))
        return store

    def sell(self, store, request_id, quantity=1, payments=None, customer_id=None):
        request = {"request_id": request_id, "items": [{"product_id": self.product_id,
                                                         "quantity_milli": quantity * 1000}],
                   "payments": payments or [{"method": "cash", "amount_cents": quantity * 1000}]}
        if customer_id:
            request["customer_id"] = customer_id
        return store.sale(self.cashier, request)

    def old_day(self):
        """A closed cashier, then an open one with everything a day at the counter produces."""
        old = self.old
        old.save_printer("receipt", "file", str(Path(self.temp.name) / "cupons"), "escpos80", self.manager_id)
        old.open_cash(self.cashier_id, 10000)
        self.sell(old, "ontem-1")
        old.close_cash(self.cashier_id, 11000, "654321")
        old.open_cash(self.cashier_id, 5000)
        self.sale_ids = [self.sell(old, "hoje-1", 2)["id"], self.sell(old, "hoje-2")["id"]]
        fiado = self.sell(old, "hoje-fiado", 3, [{"method": "credit_account", "amount_cents": 3000}],
                          self.customer_id)["id"]
        old.void_sale(self.cashier, self.sale_ids[1], "Cliente desistiu", "654321")
        old.cash_movement(self.cashier_id, "withdrawn", 2000, "Sangria")
        old.return_sale(self.manager, self.sale_ids[0], {"return_id": str(uuid.uuid4()), "refund_method": "cash",
                        "reason": "Defeito", "items": [{"product_id": self.product_id, "quantity_milli": 1000}]})
        old.receive_credit(self.cashier, {"receipt_id": str(uuid.uuid4()), "customer_id": self.customer_id,
                                          "method": "cash", "amount_cents": 1000})
        self.sale_ids.append(fiado)

    def deliver(self, store):
        """Everything waiting in a store's queue reaches the cloud, as sync_once would do."""
        events = store.pending_events(500)
        self.cloud.ingest(events)
        store.ack_events([event["event_id"] for event in events])
        return events

    def new_computer(self):
        # The cloud already shows the balances its ledger holds after the old computer's day.
        self.customers[0]["open_credit_cents"] = 2000
        self.products[0]["quantity_milli"] = 45000
        self.store = self.paired_store("novo", recovery=True)
        return self.store

    def online(self):
        return patch.object(sync, "_request", side_effect=self.cloud.request)

    def rows(self, table, store=None):
        with (store or self.store).connection() as conn:
            return [dict(row) for row in conn.execute(f"SELECT * FROM {table}")]

    def finish_first_pass(self):
        with self.online():
            status = sync.restore_step(self.store)
        self.assertEqual(status["state"], "awaiting_backup", status)
        return status


class RestoreFlowTests(RecoveryCase):
    def test_pairing_in_recovery_mode_blocks_the_cashier_until_the_data_is_back(self):
        self.old_day()
        self.deliver(self.old)
        new = self.new_computer()
        status = new.restore_status()
        self.assertEqual((status["state"], status["active"], status["pass"], status["percent"]),
                         ("pending", True, 1, 0))
        self.assertIsNotNone(status["started_at"])
        for blocked in (new.ensure_sale_allowed, lambda: new.open_cash(self.cashier_id, 0),
                        lambda: new.close_cash(self.cashier_id, 0, "654321"),
                        lambda: self.sell(new, "cedo-demais")):
            with self.assertRaises(TerminalError) as caught:
                blocked()
            self.assertEqual((str(caught.exception), caught.exception.status), (RESTORE_BLOCKED, 409))
        # A terminal that never sold pairs as a common one.
        self.assertEqual(self.old.restore_status()["state"], "done")
        self.assertFalse(self.old.restore_status()["active"])

    def test_full_restore_brings_history_back_without_events_stock_or_balance_changes(self):
        self.old_day()
        self.deliver(self.old)
        new = self.new_computer()
        before = {"products": self.rows("products"), "customers": self.rows("customers")}
        status = self.finish_first_pass()
        self.assertEqual((status["percent"], status["applied"], status["total"]), (100, 8, 8))
        self.assertEqual(self.restore_cursors, ["", "c2", "c4", "c6"])  # paged and resumed by cursor
        with self.assertRaisesRegex(TerminalError, "trazendo os dados"):  # still waiting for the backup answer
            new.ensure_sale_allowed()
        self.assertEqual(new.restore_skip_backup()["state"], "done")

        # Same records the old computer had.
        self.assertEqual(sorted(row["id"] for row in self.rows("sales")), sorted(row["id"] for row in self.rows("sales", self.old)))
        for table in ("cash_sessions", "cash_movements", "credit_receipts", "sale_returns"):
            key = lambda row: row["id"]
            self.assertEqual(sorted(self.rows(table), key=key), sorted(self.rows(table, self.old), key=key), table)
        voided = new.sale_detail(self.sale_ids[1])
        self.assertEqual((voided["status"], voided["void_reason"]), ("voided", "Cliente desistiu"))
        original = self.old.sale_detail(self.sale_ids[0])
        restored = new.sale_detail(self.sale_ids[0])
        for key in ("items", "payments", "total_cents", "change_cents", "request_id", "cash_session_id",
                    "created_at", "operator_id", "customer_name"):
            self.assertEqual(restored[key], original[key], key)
        self.assertEqual(new.printer_settings(), self.old.printer_settings())

        # Nothing was generated: no event, no print job, no stock or balance movement.
        self.assertEqual(self.rows("outbox"), [])
        self.assertEqual(self.rows("print_jobs"), [])
        self.assertEqual({"products": self.rows("products"), "customers": self.rows("customers")}, before)
        audit = [row["action"] for row in self.rows("audit")]
        self.assertEqual(audit.count("terminal.restored"), 1)
        self.assertFalse([action for action in audit if action.startswith(("sale.", "cash."))])
        status = new.restore_status()
        self.assertEqual((status["state"], status["active"], status["counts"]["sales"],
                          status["counts"]["sales_cents"], status["counts"]["cash_sessions"],
                          status["counts"]["returns"], status["counts"]["credit_receipts"]),
                         ("done", False, 4, 7000, 2, 1, 1))
        self.assertIsNotNone(status["finished_at"])

        # The open cashier came back open: the shop sells again, and the drawer adds up.
        self.assertEqual(new.cash_state()["id"], self.old.cash_state()["id"])
        self.assertEqual(new.cash_summary()["expected_cents"], self.old.cash_summary()["expected_cents"])
        self.assertEqual(self.sell(new, "depois-1")["total_cents"], 1000)
        self.assertEqual([event["type"] for event in new.pending_events()], ["sale.completed"])
        self.assertEqual(new.returnable(self.sale_ids[0])["items"][0]["available_milli"], 1000)

    def test_network_drop_in_the_middle_resumes_from_the_saved_cursor(self):
        self.old_day()
        self.deliver(self.old)
        new = self.new_computer()
        self.cloud.fail_restore_calls = {3}
        with self.online():
            status = sync.restore_step(new)
        self.assertEqual((status["state"], status["applied"]), ("restoring", 4))
        self.assertIn("Sem conexão", status["error"])
        self.assertEqual(new.meta()["restore_cursor"], "c4")
        with self.assertRaisesRegex(TerminalError, "trazendo os dados"):
            new.ensure_sale_allowed()
        # The process may even restart: the cursor lives in the database.
        self.store = new = Store(new.path)
        status = self.finish_first_pass()
        self.assertEqual(self.restore_cursors, ["", "c2", "c4", "c6"])  # never from the start again
        self.assertEqual((status["error"], status["applied"]), ("", 8))
        self.assertEqual(len(self.rows("sales")), 4)

    def test_same_page_twice_and_wrong_cursor_change_nothing(self):
        self.old_day()
        self.deliver(self.old)
        new = self.new_computer()
        first, second = self.cloud.restore_page(""), self.cloud.restore_page("c2")
        self.assertEqual(new.apply_restore(first, "")["applied"], 2)
        snapshot = {table: self.rows(table) for table in ("sales", "cash_sessions", "cash_movements", "meta")}
        self.assertEqual(new.apply_restore(first, ""), {"skipped": True})        # repeated page
        self.assertEqual(new.apply_restore(second, "c9"), {"skipped": True})     # another conductor's position
        self.assertEqual({table: self.rows(table) for table in snapshot}, snapshot)
        self.assertEqual(new.apply_restore(second, "c2")["state"], "restoring")
        self.assertEqual(new.restore_status()["applied"], 4)
        with self.assertRaises(TerminalError) as caught:
            self.old.apply_restore(first, "")
        self.assertEqual(caught.exception.status, 409)  # a common terminal never takes a restore page

    def test_totals_that_do_not_match_restart_and_pause_on_the_third_attempt(self):
        self.old_day()
        self.deliver(self.old)
        new = self.new_computer()
        self.cloud.totals_patch = lambda totals: totals["sales"].update(total_cents=1)
        with self.online():
            status = sync.restore_step(new, max_pages=4)
        self.assertEqual((status["state"], status["paused"], status["applied"]), ("restoring", False, 0))
        self.assertEqual(new.meta()["restore_attempts"], "1")
        with self.online():
            status = sync.restore_step(new, max_pages=50)
        self.assertEqual((status["state"], status["paused"]), ("restoring", True))
        self.assertIn("não bateram", status["error"])
        calls = self.cloud.restore_calls
        with self.online():
            sync.restore_step(new)  # paused: waits for the person
        self.assertEqual(self.cloud.restore_calls, calls)
        with self.assertRaisesRegex(TerminalError, "trazendo os dados"):
            new.ensure_sale_allowed()
        self.cloud.totals_patch = None
        status = new.restore_retry()
        self.assertEqual((status["paused"], status["error"], status["applied"]), (False, "", 0))
        self.finish_first_pass()
        self.assertEqual(len(self.rows("sales")), 4)  # the retries never duplicated a row

    def test_cursor_refused_by_the_cloud_starts_over(self):
        self.old_day()
        self.deliver(self.old)
        new = self.new_computer()
        self.cloud.refuse_cursors = 1
        self.finish_first_pass()
        self.assertEqual(self.restore_cursors, ["", "", "c2", "c4", "c6"])
        self.assertEqual(new.restore_status()["applied"], 8)

    def test_odd_records_never_break_the_restore(self):
        new = self.new_computer()
        session = {"kind": "cash_session", "id": str(uuid.uuid4()), "operator_id": None, "opening_cents": 0,
                   "opened_at": 10, "closed_at": None, "counted_cents": None, "expected_cents": None,
                   "incomplete": True, "movements": []}
        second_open = dict(session, id=str(uuid.uuid4()), opened_at=20)
        sale = {"kind": "sale", "id": str(uuid.uuid4()), "event_id": None, "status": "completed", "voided_at": None,
                "void_reason": "", "returns": [{"id": str(uuid.uuid4()), "refund_method": "external",
                                                "total_cents": 500, "reason": "Painel", "operator_id": None,
                                                "cash_session_id": "", "created_at": 30, "origin": "panel",
                                                "items": [{"product_id": self.product_id, "quantity_milli": 500,
                                                           "amount_cents": 500}]}],
                "data": {"request_id": "r-1", "cash_session_id": "sessao-que-nao-veio", "created_at": 25,
                         "gross_cents": 1000, "promo_discount_cents": 0, "discount_cents": 0, "total_cents": 1000,
                         "change_cents": 0, "customer_id": None, "customer_name": "", "customer_document": "",
                         "operator_id": None, "seller_id": None,
                         "items": [{"product_id": self.product_id, "name": "Arroz", "unit": "UN",
                                    "quantity_milli": 1000, "line_total_cents": 1000}],
                         "payments": [{"method": "cash", "amount_cents": 1000}]}}
        twin = dict(sale, id=str(uuid.uuid4()))  # another sale with the same request_id
        lone = dict(sale, id=str(uuid.uuid4()), returns=[], data=dict(sale["data"], request_id="r-2"))
        records = [session, second_open, sale, twin, lone, {"kind": "novidade-futura"}]
        totals = FakeCloud.totals([session, second_open, sale, twin, lone])
        result = new.apply_restore({"records": records, "next_cursor": None, "done": True, "totals": totals,
                                    "progress": {"position": 6, "total": 6}}, "")
        self.assertEqual((result["state"], result["done"], result["warnings"]), ("awaiting_backup", True, 2))
        sessions = {row["id"]: row for row in self.rows("cash_sessions")}
        self.assertEqual((sessions[session["id"]]["closed_at"], sessions[session["id"]]["operator_id"]), (None, ""))
        self.assertEqual(sessions[second_open["id"]]["closed_at"], 20)  # only one cashier may be open
        self.assertEqual(sorted(row["id"] for row in self.rows("sales")), sorted([sale["id"], lone["id"]]))
        self.assertEqual(self.rows("sale_returns")[0]["refund_method"], "external")
        new.restore_skip_backup()
        # A sale whose cashier this computer does not have counts as closed: no void at the counter.
        with self.assertRaisesRegex(TerminalError, "já foi fechado"):
            new.void_sale(self.manager, lone["id"], "Engano")

    def test_secrets_of_phase_two_are_stored_cleared_and_never_exported(self):
        new = self.new_computer()
        meta = new.meta()
        self.assertEqual(json.loads(meta["mirror_key"]), MIRROR_KEY)
        self.assertEqual(json.loads(meta["cloud_capabilities"]), CAPABILITIES)
        self.assertNotIn(MIRROR_KEY["key"], json.dumps(new.export_data()))
        self.assertNotIn(MIRROR_KEY["key"], json.dumps(self.rows("audit")))
        # A malformed key is ignored and never replaces the good one.
        new.apply_lease(dict(self.lease_answer(new), mirror_key={"key_id": "x", "key": "curta"}))
        self.assertEqual(json.loads(new.meta()["mirror_key"]), MIRROR_KEY)
        with new.transaction() as conn:
            Store._clear_connection_meta(conn)
        self.assertFalse({"mirror_key", "cloud_capabilities", "restore_state", "restore_pass",
                          "restore_started_at"} & set(new.meta()))


class MirrorImportTests(RecoveryCase):
    def stranded_day(self):
        """The old computer synced part of the day and died with the rest only in its queue."""
        self.old_day()
        events = self.old.pending_events(500)
        delivered, stranded = events[:6], events[6:]
        self.cloud.ingest(delivered)
        self.old.ack_events([event["event_id"] for event in delivered])
        with self.old.connection() as conn:
            rows = conn.execute("SELECT id,seq,created_at,payload FROM outbox WHERE acked_at IS NULL ORDER BY seq")
            return [{"seq": row["seq"], "event_id": row["id"], "created_at": row["created_at"],
                     "event": json.loads(row["payload"])} for row in rows], stranded

    def test_queue_of_the_backup_goes_up_untouched_and_comes_back_in_a_second_pass(self):
        items, stranded = self.stranded_day()
        self.assertGreater(len(items), 3)
        new = self.new_computer()
        self.finish_first_pass()
        self.assertLess(len(self.rows("sales")), 4)  # the cloud did not have the whole day
        foreign = copy.deepcopy(items[0])
        foreign["event"]["identity"]["shop_id"] = str(uuid.uuid4())
        foreign["event_id"] = foreign["event"]["event_id"] = uuid.uuid4().hex
        broken = [{"seq": 99, "event_id": "x", "created_at": 1, "event": {"event_id": "x"}}, "lixo",
                  dict(items[1], event_id=uuid.uuid4().hex)]
        result = new.import_outbox_from_mirror(items + [foreign] + broken)
        self.assertEqual((result["imported"], result["already_present"], result["foreign"], result["invalid"]),
                         (len(items), 0, 1, 3))
        self.assertEqual((result["first_seq"], result["last_seq"]), (1, len(items)))
        self.assertEqual(new.restore_status()["state"], "importing")
        self.assertEqual(new.restore_status()["import"]["imported"], len(items))
        # Same events, same ids, same order, byte for byte, with their original dates.
        self.assertEqual(new.pending_events(500), stranded)
        with new.connection() as conn, self.old.connection() as origin:
            query = "SELECT id,event_type,aggregate_id,payload,created_at FROM outbox WHERE acked_at IS NULL ORDER BY seq"
            self.assertEqual([tuple(row) for row in conn.execute(query)], [tuple(row) for row in origin.execute(query)])
        # Idempotent, and it applied nothing locally.
        again = new.import_outbox_from_mirror(items)
        self.assertEqual((again["imported"], again["already_present"]), (0, len(items)))
        self.assertEqual(len(new.pending_events(500)), len(items))
        self.assertLess(len(self.rows("sales")), 4)
        self.assertEqual([row["action"] for row in self.rows("audit")].count("restore.mirror_imported"), 1)
        with self.assertRaisesRegex(TerminalError, "trazendo os dados"):
            new.ensure_sale_allowed()

        with self.online():
            status = sync.restore_step(new)
        self.assertEqual((status["state"], status["pass"], status["active"]), ("done", 2, False))
        self.assertEqual(new.pending_events(), [])
        self.assertEqual(len(self.cloud.events), len(self.cloud.event_ids))  # the cloud deduplicated by event_id
        for table in ("sales", "cash_sessions", "cash_movements", "credit_receipts", "sale_returns"):
            key = lambda row: row["id"]
            mine, theirs = sorted(self.rows(table), key=key), sorted(self.rows(table, self.old), key=key)
            if table == "sales":
                mine, theirs = [[row["id"], row["status"]] for row in mine], [[row["id"], row["status"]] for row in theirs]
            self.assertEqual(mine, theirs, table)
        self.assertEqual([row["action"] for row in self.rows("audit")].count("terminal.restored"), 1)
        self.assertEqual(self.sell(new, "depois-da-copia")["total_cents"], 1000)

    def test_import_needs_the_question_to_be_open_and_empty_copy_keeps_waiting(self):
        items, _ = self.stranded_day()
        new = self.new_computer()
        with self.assertRaises(TerminalError) as caught:
            new.import_outbox_from_mirror(items)  # data not back yet
        self.assertEqual(caught.exception.status, 409)
        self.finish_first_pass()
        self.assertEqual(new.import_outbox_from_mirror([])["imported"], 0)
        self.assertEqual(new.restore_status()["state"], "awaiting_backup")
        with self.assertRaises(TerminalError):
            self.old.restore_skip_backup()


class TransferredTests(RecoveryCase):
    BODY = {"error": "Este caixa foi transferido para outro computador.", "code": "terminal_transferred",
            "send_only_until": "2026-11-23T12:00:00+00:00", "transferred_at": "2026-11-16T12:00:00+00:00"}

    def test_old_computer_keeps_sending_its_queue_and_stops_selling(self):
        old = self.old
        old.open_cash(self.cashier_id, 0)
        self.sell(old, "antes-da-troca")
        self.cloud.transferred, self.cloud.send_only = self.BODY, self.BODY["send_only_until"]
        with self.online():
            result = sync.sync_once(old)
        self.assertEqual((result["sent"], result["remaining"]), (2, 0))
        self.assertEqual(len(self.cloud.events), 2)  # what was sold here is safe in the cloud
        status = old.lease_status()
        self.assertEqual((status["state"], status["can_sell"], status["can_open_cash"], status["send_only_until"]),
                         ("transferred", False, False, self.BODY["send_only_until"]))
        self.assertIn("transferido para outro computador", status["reason"])
        with self.assertRaises(TerminalError) as caught:
            self.sell(old, "depois-da-troca")
        self.assertEqual(caught.exception.status, 403)
        self.assertIn("transferido", str(caught.exception))
        # The open cashier can still be closed, and the closing goes up too.
        old.close_cash(self.cashier_id, 1000, "654321")
        with self.assertRaisesRegex(TerminalError, "transferido"):
            old.open_cash(self.cashier_id, 0)
        self.cloud.calls.clear()
        with self.online():
            errors = sync.Worker(old).run_once()
        self.assertEqual((errors, self.cloud.calls), ([], ["sync"]))  # no heartbeat, license, catalog or restore
        self.assertEqual(self.cloud.events[-1]["type"], "cash.closed")
        self.cloud.calls.clear()
        with self.online():
            self.assertEqual(sync.Worker(old).run_once(), [])
        self.assertEqual(self.cloud.calls, [])  # empty queue: nothing left to do here
        self.assertEqual([row["action"] for row in self.rows("audit")].count("terminal.transferred"), 1)
        self.assertIsNone(old.ensure_config_event())

    def test_refusal_on_any_route_marks_the_transfer_and_the_queue_stays_safe(self):
        self.cloud.transferred = dict(self.BODY, send_only_until=None)  # the send-only days are over
        for call in (sync.heartbeat_once, lambda store: sync.renew_lease(store, force=True),
                     sync.refresh_bootstrap):
            old = self.store = self.paired_store("antigo-" + uuid.uuid4().hex[:6])
            with self.online(), self.assertRaises(TerminalError):
                call(old)
            self.assertEqual(old.lease_status()["state"], "transferred")
        old.open_cash  # noqa: B018 - nothing sold on this one
        seller = self.store = self.paired_store("antigo-fila")
        seller.open_cash(self.cashier_id, 0)
        self.sell(seller, "ficou-na-fila")
        with self.online(), self.assertRaises(TerminalError):
            sync.sync_once(seller)
        self.assertEqual(seller.lease_status()["state"], "transferred")
        self.assertIsNone(seller.lease_status()["send_only_until"])
        self.assertEqual(len(seller.pending_events()), 2)  # refused, never dropped
        self.assertEqual(len(seller.export_data()["pending_events"]), 2)

    def test_cloud_error_code_travels_with_the_exception(self):
        answer = types.SimpleNamespace(ok=False, status_code=403, json=lambda: self.BODY)
        with self.assertRaises(TerminalError) as caught:
            sync._json_response(answer)
        self.assertEqual((caught.exception.remote_status, caught.exception.remote_code,
                          caught.exception.remote_body), (403, "terminal_transferred", self.BODY))
        plain = types.SimpleNamespace(ok=False, status_code=400, json=lambda: {"error": "Paginação inválida"})
        with self.assertRaises(TerminalError) as caught:
            sync._json_response(plain)
        self.assertIsNone(caught.exception.remote_code)


class ConfigEventTests(RecoveryCase):
    def config_events(self, store=None):
        return [event for event in (store or self.store).pending_events(500) if event["type"] == "terminal.config"]

    def test_printer_change_queues_one_config_event_with_the_contract_shape(self):
        target = str(Path(self.temp.name) / "cupons")
        with mirror_module(FakeMirror(enabled=True)):
            self.old.save_printer("receipt", "file", target, "escpos80", self.manager_id)
            self.assertIsNone(self.old.ensure_config_event())  # nothing changed since
            self.old.save_printer("receipt", "file", target, "escpos80", self.manager_id)
        events = self.config_events()
        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event["aggregate_id"], self.terminal_id)
        self.assertEqual(set(event["data"]), {"version", "print_settings", "mirror", "app_version", "updated_at"})
        self.assertEqual((event["data"]["version"], event["data"]["mirror"], event["data"]["app_version"]),
                         (1, {"enabled": True}, sync.VERSION))
        self.assertEqual(event["data"]["print_settings"],
                         [{"kind": "receipt", "driver": "file", "target": target, "format": "escpos80"}])
        self.assertIsInstance(event["data"]["updated_at"], int)
        # A change (the printer, or the copy being turned off) queues a new one.
        with mirror_module(FakeMirror(enabled=False)):
            self.assertIsNotNone(self.old.ensure_config_event())
            self.old.save_printer("receipt", "file", target, "escpos58", self.manager_id)
        self.assertEqual([event["data"]["mirror"]["enabled"] for event in self.config_events()], [True, False, False])
        self.assertEqual(self.config_events()[-1]["data"]["print_settings"][0]["format"], "escpos58")

    def test_no_mirror_module_omits_the_key_and_old_cloud_gets_no_event(self):
        target = str(Path(self.temp.name) / "cupons")
        self.old.save_printer("receipt", "file", target, "escpos80", self.manager_id)
        self.assertNotIn("mirror", self.config_events()[0]["data"])
        legacy = self.store = Store(Path(self.temp.name) / "nuvem-antiga" / "terminal.sqlite3")
        answer = self.pairing_answer(legacy)
        del answer["capabilities"]
        legacy.set_pairing(answer, "http://127.0.0.1:8000")
        legacy.save_printer("receipt", "file", target, "escpos80", self.manager_id)
        self.assertEqual(legacy.pending_events(), [])

    def test_restored_config_fills_missing_printers_and_is_not_sent_back(self):
        target = str(Path(self.temp.name) / "cupons")
        self.old.save_printer("receipt", "file", target, "escpos80", self.manager_id)
        self.old.open_cash(self.cashier_id, 0)
        self.deliver(self.old)
        new = self.new_computer()
        self.assertIsNone(new.ensure_config_event())  # never while the data is coming
        self.finish_first_pass()
        new.restore_skip_backup()
        self.assertEqual(new.printer_settings(),
                         [{"kind": "receipt", "driver": "file", "target": target, "format": "escpos80"}])
        self.assertIsNone(new.ensure_config_event())
        self.assertEqual(self.config_events(new), [])
        with self.online():
            self.assertEqual(sync.Worker(new).run_once(), [])  # the worker asks on every pass
        self.assertEqual(self.config_events(new), [])

    def test_printer_chosen_on_the_new_computer_is_kept(self):
        self.old.save_printer("receipt", "file", "/antigo", "escpos80", self.manager_id)
        self.old.open_cash(self.cashier_id, 0)
        self.deliver(self.old)
        new = self.new_computer()
        new.save_printer("receipt", "tcp", "192.168.0.9:9100", "escpos58", self.manager_id)
        self.finish_first_pass()
        self.assertEqual(new.printer_settings()[0]["target"], "192.168.0.9:9100")
        new.restore_skip_backup()
        self.assertEqual(len(self.config_events(new)), 0)
        self.assertIsNotNone(new.ensure_config_event())  # it differs from what the cloud holds


class MirrorHookTests(RecoveryCase):
    def test_hooks_fire_after_commit_and_never_on_rollback(self):
        fake = FakeMirror()
        with mirror_module(fake):
            old = self.old
            old.open_cash(self.cashier_id, 0)
            self.assertEqual(fake.calls, [("on_event", 1)])  # the event was already readable: after the commit
            self.sell(old, "com-copia")
            with self.assertRaises(TerminalError):
                self.sell(old, "sem-estoque", 999)
            old.record_audit(None, "teste.sem_evento")  # a transaction without events wakes nobody
            self.assertEqual(fake.calls, [("on_event", 1), ("on_event", 2)])
            old.close_cash(self.cashier_id, 1000, "654321")
            self.assertEqual(fake.calls[2:], [("on_event", 3), ("on_cash_closed",)])

    def test_import_from_the_backup_also_wakes_the_copy(self):
        self.old.open_cash(self.cashier_id, 0)
        with self.old.connection() as conn:
            row = conn.execute("SELECT id,seq,created_at,payload FROM outbox").fetchone()
        item = {"seq": row["seq"], "event_id": row["id"], "created_at": row["created_at"],
                "event": json.loads(row["payload"])}
        self.cloud.ingest([])
        self.new_computer()
        self.store.apply_restore({"records": [], "next_cursor": None, "done": True,
                                  "totals": FakeCloud.totals([])}, "")
        fake = FakeMirror()
        with mirror_module(fake):
            self.store.import_outbox_from_mirror([item])
            self.store.import_outbox_from_mirror([item])  # nothing new: nobody is woken
        self.assertEqual(fake.calls, [("on_event", 1)])

    def test_broken_or_missing_copy_never_breaks_a_sale(self):
        fake = FakeMirror()
        fake.explode = True
        with mirror_module(fake):
            self.old.open_cash(self.cashier_id, 0)
            self.assertEqual(self.sell(self.old, "disco-arrancado")["total_cents"], 1000)
            self.old.close_cash(self.cashier_id, 1000, "654321")
        self.assertGreaterEqual(len(fake.calls), 4)
        with mirror_module(None):  # build without terminal/mirror.py
            self.old.open_cash(self.cashier_id, 0)
            self.assertEqual(self.sell(self.old, "sem-modulo")["total_cents"], 1000)
            self.old.close_cash(self.cashier_id, 1000, "654321")
        self.assertEqual(len(self.old.pending_events(500)), 6)

    def test_worker_start_and_profile_change_start_and_stop_the_copy(self):
        fake = FakeMirror()
        data_dir = Path(self.temp.name) / "app"
        with mirror_module(fake), patch.object(sync.Worker, "start"), patch.object(sync.Worker, "stop"):
            app = create_app({"TESTING": True, "DATA_DIR": str(data_dir), "START_WORKER": True})
            self.assertEqual(fake.calls, [("start", str(data_dir / "terminal.sqlite3"))])
            self.assertIn("terminal_mirror", app.extensions)
            client = app.test_client()
            csrf = client.get("/api/session").json["csrf"]
            answer = client.post("/api/new-profile", json={"confirm": True}, headers={"X-CSRF-Token": csrf})
            self.assertEqual(answer.status_code, 200, answer.json)
            self.assertEqual([call[0] for call in fake.calls], ["start", "stop", "start"])
            self.assertIn(answer.json["profile"], fake.calls[2][1])
        with mirror_module(None):
            app = create_app({"TESTING": True, "DATA_DIR": str(Path(self.temp.name) / "app2"), "START_WORKER": False})
            self.assertNotIn("terminal_mirror", app.extensions)


class RouteCase(RecoveryCase):
    def setUp(self):
        super().setUp()
        self.data_dir = Path(self.temp.name) / "tela"
        self.app = create_app({"TESTING": True, "DATA_DIR": str(self.data_dir), "START_WORKER": False})
        self.client = self.app.test_client()
        self.store = self.app.extensions["terminal_store"]

    def csrf(self):
        return self.client.get("/api/session").json["csrf"]

    def post(self, path, payload=None):
        return self.client.post(path, json=payload or {}, headers={"X-CSRF-Token": self.csrf()})

    def login(self, operator_id, pin):
        answer = self.post("/api/login", {"operator_id": operator_id, "pin": pin})
        self.assertEqual(answer.status_code, 200, answer.json)

    def start(self, recovery=True, expect=200):
        def request(method, url, **kwargs):
            if url.endswith("/terminal/pair"):
                self.pair_body = kwargs["json"]
                return self.pairing_answer(self.store, recovery)
            return self.cloud.request(method, url, **kwargs)
        self.customers[0]["open_credit_cents"] = 2000
        with patch.object(sync, "_request", side_effect=request):
            answer = self.post("/api/restore/start", {"cloud_url": "http://127.0.0.1:8000",
                                                      "license_key": "PDV2-AAAAAA-BBBBBB-CCCCCC-DDDDDD",
                                                      "code": " k7qd-m2xp-9htw "})
        self.assertEqual(answer.status_code, expect, answer.json)
        return answer.json


class RestoreRouteTests(RouteCase):
    def test_recover_my_shop_from_the_first_screen_to_the_cashier(self):
        self.old_day()
        events = self.old.pending_events(500)
        self.cloud.ingest(events[:-2])
        stranded = [{"seq": index, "event_id": event["event_id"], "created_at": 1, "event": event}
                    for index, event in enumerate(events[-2:], start=1)]
        session = self.client.get("/api/session").json
        self.assertEqual((session["paired"], session["restore"]["active"], session["mirror"]), (False, False, None))
        self.assertEqual(self.post("/api/restore/step").status_code, 409)       # nothing to restore yet
        self.assertEqual(self.client.get("/api/restore/backups").status_code, 409)
        self.assertEqual(self.client.post("/api/restore/start", json={}).status_code, 403)  # CSRF like every route

        started = self.start()
        self.assertEqual((started["paired"], started["recovery"], started["restore"]["state"]),
                         (True, True, "pending"))
        self.assertEqual(self.pair_body["code"], "k7qd-m2xp-9htw")  # sent as typed; the cloud normalizes
        self.assertEqual(self.start(expect=409)["error"][:28], "Este dispositivo já está par")
        session = self.client.get("/api/session").json
        self.assertTrue(session["restore"]["active"])
        self.assertEqual(self.client.get("/api/restore/status").json["restore"]["state"], "pending")

        # No internet: HTTP 200 with the error inside, so the screen keeps trying by itself.
        self.cloud.fail_restore_calls = {1}
        with self.online():
            offline = self.post("/api/restore/step")
        self.assertEqual(offline.status_code, 200)
        self.assertIn("Sem conexão", offline.json["restore"]["error"])
        self.cloud.page_size = 3
        states = []
        with self.online():
            for _ in range(5):
                states.append(self.post("/api/restore/step").json["restore"])
                if states[-1]["state"] != "restoring":
                    break
        self.assertEqual(states[-1]["state"], "awaiting_backup")
        self.assertEqual(states[-1]["percent"], 100)
        self.assertTrue(all(first["percent"] <= second["percent"] for first, second in zip(states, states[1:])))

        # The cashier cannot be opened from the login either.
        self.login(self.cashier_id, "123987")
        blocked = self.post("/api/cash/open", {"opening_cents": 0})
        self.assertEqual((blocked.status_code, blocked.json["error"]), (409, RESTORE_BLOCKED))

        # "Tenho uma cópia de segurança".
        self.assertEqual(self.client.get("/api/restore/backups").json, {"supported": False, "candidates": []})
        refused = self.post("/api/restore/import-backup", {"directory": "/mnt/pendrive"})
        self.assertEqual((refused.status_code, refused.json["error"]),
                         (409, "Cópia em segundo disco indisponível nesta instalação"))
        fake = FakeMirror(pending=stranded)
        with mirror_module(fake):
            found = self.client.get("/api/restore/backups?directory=/mnt/pendrive").json
            self.assertEqual((found["supported"], found["candidates"][0]["replica_id"]), (True, "antigo"))
            self.assertEqual(self.post("/api/restore/import-backup", {"directory": ""}).status_code, 400)
            imported = self.post("/api/restore/import-backup", {"directory": "/mnt/pendrive", "replica_id": "antigo"})
        self.assertEqual(imported.status_code, 200, imported.json)
        self.assertEqual((imported.json["imported"], imported.json["restore"]["state"]), (2, "importing"))
        self.assertIn(("read_pending", "/mnt/pendrive", MIRROR_KEY, "antigo"), fake.calls)
        self.assertEqual(self.post("/api/restore/skip-backup").status_code, 409)  # the queue must go up first
        with self.online():
            for _ in range(6):
                final = self.post("/api/restore/step").json["restore"]
                if not final["active"]:
                    break
        self.assertEqual((final["state"], final["pass"], final["counts"]["sales"]), ("done", 2, 4))
        self.assertEqual(self.post("/api/restore/step").status_code, 409)
        self.assertEqual(self.post("/api/restore/retry").status_code, 409)
        opened = self.post("/api/sales", {"request_id": "liberado", "items": [{"product_id": self.product_id,
                           "quantity_milli": 1000}], "payments": [{"method": "cash", "amount_cents": 1000}]})
        self.assertEqual(opened.status_code, 200, opened.json)

    def test_without_backup_the_person_continues_and_key_is_required_to_import(self):
        self.old.open_cash(self.cashier_id, 0)
        self.deliver(self.old)
        self.start()
        with self.online():
            self.assertEqual(self.post("/api/restore/step").json["restore"]["state"], "awaiting_backup")
        with self.store.transaction() as conn:
            conn.execute("DELETE FROM meta WHERE key='mirror_key'")
        with mirror_module(FakeMirror()):
            refused = self.post("/api/restore/import-backup", {"directory": "/mnt/pendrive"})
        self.assertEqual((refused.status_code, refused.json["error"]),
                         (409, "Conecte este caixa à internet para liberar a cópia de segurança."))
        skipped = self.post("/api/restore/skip-backup")
        self.assertEqual((skipped.status_code, skipped.json["restore"]["state"]), (200, "done"))
        self.assertFalse(self.client.get("/api/session").json["restore"]["active"])

    def test_terminal_without_history_pairs_as_a_new_one(self):
        started = self.start(recovery=False)
        self.assertEqual((started["recovery"], started["restore"]["state"]), (False, "done"))
        self.login(self.cashier_id, "123987")
        self.assertEqual(self.post("/api/cash/open", {"opening_cents": 0}).status_code, 200)

    def test_pause_and_retry_through_the_screen(self):
        self.old.open_cash(self.cashier_id, 0)
        self.deliver(self.old)
        self.start()
        self.cloud.totals_patch = lambda totals: totals["cash_sessions"].update(count=7)
        with self.online():
            paused = self.post("/api/restore/step").json["restore"]
        self.assertTrue(paused["paused"])
        self.cloud.totals_patch = None
        self.assertFalse(self.post("/api/restore/retry").json["restore"]["paused"])
        with self.online():
            self.assertEqual(self.post("/api/restore/step").json["restore"]["state"], "awaiting_backup")

    def test_default_cloud_address_comes_from_the_installation(self):
        with patch.dict("os.environ", {"PDV_CLOUD_URL": "http://127.0.0.1:8000"}):
            self.assertEqual(self.client.get("/api/session").json["default_cloud_url"], "http://127.0.0.1:8000")

            def request(method, url, **kwargs):
                self.assertTrue(url.startswith("http://127.0.0.1:8000/"))
                return self.pairing_answer(self.store, False) if url.endswith("/pair") else self.cloud.request(method, url, **kwargs)
            with patch.object(sync, "_request", side_effect=request):
                answer = self.post("/api/restore/start", {"license_key": "PDV2-AAAAAA-BBBBBB", "code": "K7QD-M2XP-9HTW"})
        self.assertEqual(answer.status_code, 200, answer.json)
        self.assertEqual(self.client.get("/api/session").json["default_cloud_url"], "")


class MirrorRouteTests(RouteCase):
    def setUp(self):
        super().setUp()
        self.start(recovery=False)

    def test_routes_delegate_to_the_mirror_module(self):
        fake = FakeMirror(enabled=False)
        with mirror_module(fake), patch.dict("os.environ", {"PDV_ENV": "test"}):
            self.assertEqual(self.client.get("/api/mirror/status").status_code, 401)
            self.login(self.cashier_id, "123987")
            self.assertEqual(self.client.get("/api/mirror/status").json["state"], "ok")
            self.assertEqual(self.client.get("/api/mirror/drives").status_code, 403)   # manager only
            self.assertEqual(self.post("/api/mirror/config", {"directory": "/mnt/pendrive"}).status_code, 403)
            self.assertEqual(self.post("/api/mirror/snapshot").status_code, 403)
            self.login(self.manager_id, "654321")
            self.assertEqual(self.client.get("/api/mirror/drives").json["drives"][0]["directory"], "/mnt/pendrive")
            self.assertEqual(self.post("/api/mirror/config", {"directory": 7}).status_code, 400)
            configured = self.post("/api/mirror/config", {"directory": "/mnt/pendrive"})
            self.assertEqual((configured.status_code, configured.json["enabled"]), (200, True))
            self.assertIn(("configure", "/mnt/pendrive", True), fake.calls)
            self.assertEqual(self.post("/api/mirror/snapshot").json["name"], "snapshot-1.pdv2snap")
            self.assertEqual(self.client.get("/api/session").json["mirror"]["enabled"], True)
            # Turning it on is audited and reaches the cloud as terminal.config.
            config = [event for event in self.store.pending_events() if event["type"] == "terminal.config"]
            self.assertEqual([event["data"]["mirror"] for event in config], [{"enabled": True}])
            self.assertEqual(self.post("/api/mirror/config", {"directory": None}).json["enabled"], False)
            self.assertIn(("configure", None, True), fake.calls)
        audit = [row for row in self.rows("audit") if row["action"] == "mirror.configured"]
        self.assertEqual([row["detail"] for row in audit], ["ligada: /mnt/pendrive", "desligada"])
        with mirror_module(fake), patch.dict("os.environ", {"PDV_ENV": "production"}):
            self.post("/api/mirror/config", {"directory": "/mnt/pendrive"})
        self.assertIn(("configure", "/mnt/pendrive", False), fake.calls)  # same disk only in development

    def test_copy_errors_reach_the_screen_in_plain_words(self):
        fake = FakeMirror()
        fake.snapshot_now = lambda store: (_ for _ in ()).throw(TerminalError("Escolha outro disco ou um pendrive: "
                                                                               "esta pasta fica no mesmo disco do caixa.", 409))
        fake.drives = lambda store: (_ for _ in ()).throw(OSError("E/S"))
        fake.status = lambda store: (_ for _ in ()).throw(OSError("E/S"))
        self.login(self.manager_id, "654321")
        with mirror_module(fake), self.assertLogs(self.app.logger, "ERROR"):
            answer = self.post("/api/mirror/snapshot")
            self.assertEqual((answer.status_code, answer.json["error"][:19]), (409, "Escolha outro disco"))
            self.assertEqual(self.client.get("/api/mirror/drives").status_code, 500)
            self.assertEqual(self.client.get("/api/mirror/status").json["state"], "error")
            self.assertEqual(self.client.get("/api/session").status_code, 200)

    def test_build_without_the_module_says_so(self):
        self.login(self.manager_id, "654321")
        self.assertEqual(self.client.get("/api/mirror/status").json,
                         {"supported": False, "enabled": False, "state": "off",
                          "message": "Cópia em segundo disco indisponível nesta instalação"})
        for answer in (self.client.get("/api/mirror/drives"), self.post("/api/mirror/config", {"directory": None}),
                       self.post("/api/mirror/snapshot")):
            self.assertEqual((answer.status_code, answer.json["error"]),
                             (409, "Cópia em segundo disco indisponível nesta instalação"))


class TransferredScreenTests(RouteCase):
    def test_session_tells_the_screen_and_selling_is_refused(self):
        self.start(recovery=False)
        self.login(self.cashier_id, "123987")
        self.assertEqual(self.post("/api/cash/open", {"opening_cents": 0}).status_code, 200)
        self.store.mark_transferred("2026-11-23T12:00:00+00:00")
        license_state = self.client.get("/api/session").json["license"]
        self.assertEqual((license_state["state"], license_state["can_sell"]), ("transferred", False))
        refused = self.post("/api/sales", {"request_id": "nao", "items": [{"product_id": self.product_id,
                            "quantity_milli": 1000}], "payments": [{"method": "cash", "amount_cents": 1000}]})
        self.assertEqual(refused.status_code, 403)
        self.assertIn("transferido para outro computador", refused.json["error"])
        closed = self.post("/api/cash/close", {"counted_cents": 0, "manager_pin": "654321"})
        self.assertEqual(closed.status_code, 200, closed.json)


class ScreenTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = (ROOT / "terminal" / "templates" / "index.html").read_text(encoding="utf-8")
        cls.js = (STATIC / "terminal.js").read_text(encoding="utf-8")

    def test_recovery_wizard_is_on_the_first_screen(self):
        for text in ("Recuperar minha loja (troquei de computador)", 'id="restoreView"', "Código de recuperação",
                     "Trazendo seus dados", 'role="progressbar"', "Você tem uma cópia de segurança?",
                     "Tenho uma cópia de segurança", "Não tenho, continuar", "Tudo pronto", "Entrar no caixa",
                     "Cópia de segurança em outro disco", "Escolher disco", "Copiar agora"):
            self.assertIn(text, self.html)
        for text in ("'restoreView'", "/api/restore/start", "/api/restore/step", "/api/restore/retry",
                     "/api/restore/backups", "/api/restore/import-backup", "/api/restore/skip-backup",
                     "/api/mirror/status", "/api/mirror/drives", "/api/mirror/config", "/api/mirror/snapshot",
                     "Este caixa foi transferido", "Este caixa ainda não tinha vendas guardadas",
                     "default_cloud_url", "session.restore?.active"):
            self.assertIn(text, self.js)
        # Every element the script wires exists in the page.
        for element in ("restoreOpenBtn", "restoreBackBtn", "restoreForm", "restoreRetryBtn", "restoreHasBackupBtn",
                        "restoreFolderForm", "restoreSkipBtn", "restoreEnterBtn", "restoreBackupList",
                        "restoreBarFill", "restoreLine", "restoreNotice", "restoreSummary", "restoreCloudField",
                        "mirrorCard", "mirrorInfo", "mirrorDetail", "mirrorChooseBtn", "mirrorSnapshotBtn"):
            self.assertIn(f'id="{element}"', self.html, element)
            self.assertIn(f"'{element}'", self.js, element)

    @unittest.skipUnless(shutil.which("node"), "node não está disponível")
    def test_pure_rules_of_the_recovery_screen(self):
        script = """
          const C = require(process.argv[1]);
          const restoring = {active: true, state: 'restoring', pass: 1, applied: 380, total: 1234, percent: 30, error: '', paused: false};
          console.log(JSON.stringify({
            steps: [C.restoreStep(null), C.restoreStep({active: false, state: 'done', finished_at: 5}),
                    C.restoreStep({active: true, state: 'pending'}), C.restoreStep(restoring),
                    C.restoreStep({active: true, state: 'awaiting_backup'}), C.restoreStep({active: true, state: 'importing'})],
            progress: C.restoreProgress(restoring),
            offline: C.restoreProgress({...restoring, error: 'Sem conexão com o servidor; dados locais preservados'}).notice,
            paused: C.restoreProgress({...restoring, paused: true, error: 'Os dados não bateram'}),
            starting: C.restoreProgress({active: true, state: 'pending', percent: 0}).line,
            summary: C.restoreSummary({counts: {sales: 1180, sales_cents: 98765432, cash_sessions: 92, returns: 1, credit_receipts: 31}}),
            imported: [C.restoreImportText({imported: 2}), C.restoreImportText({imported: 0, foreign: 3}), C.restoreImportText({imported: 0})],
            mirror: [C.mirrorCard({supported: false, message: 'Indisponível'}), C.mirrorCard({supported: true, enabled: false}),
                     C.mirrorCard({supported: true, enabled: true, state: 'ok', message: 'Cópia de segurança em dia', directory: 'E:\\\\'}),
                     C.mirrorCard({supported: true, enabled: true, state: 'no_key', message: 'Conecte este caixa'})],
          }));
        """
        result = subprocess.run(["node", "-e", script, str(STATIC / "terminal-core.js")],
                                capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        self.assertEqual(data["steps"], ["start", "done", "progress", "progress", "backup", "progress"])
        self.assertEqual(data["progress"], {"percent": 30, "notice": "", "retry": False,
                                            "line": "380 de 1.234 registros · não desligue o computador"})
        self.assertEqual(data["offline"], "Sem internet. Continuamos sozinhos quando a conexão voltar.")
        self.assertEqual((data["paused"]["retry"], data["paused"]["notice"]), (True, "Os dados não bateram"))
        self.assertIn("não desligue o computador", data["starting"])
        self.assertTrue(data["summary"].startswith("1.180 vendas · R$"), data["summary"])
        self.assertIn("987.654,32", data["summary"])
        self.assertIn("92 caixas · 1 devolução · 31 recebimentos de fiado", data["summary"])
        self.assertIn("2 registros", data["imported"][0])
        self.assertIn("outra loja", data["imported"][1])
        self.assertEqual([card["canChoose"] for card in data["mirror"]], [False, True, True, True])
        self.assertEqual([card["canCopy"] for card in data["mirror"]], [False, False, True, False])
        self.assertEqual(data["mirror"][2]["text"], "Cópia de segurança em dia · E:\\")


if __name__ == "__main__":
    unittest.main()
