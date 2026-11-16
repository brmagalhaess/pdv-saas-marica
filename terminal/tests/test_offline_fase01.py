"""Offline phases 0 and 1 on the terminal: queue order, clock, PIN lockouts, damaged
database, data directory, long lease, release key and the updater hooks."""

import importlib
import importlib.util
import json
import sqlite3
import sys
import tempfile
import time
import types
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

import shared.licensing as licensing
from shared.licensing import generate_keypair, payload_digest, sign_lease
from shared.releases import sign_release
from shared.security import hash_pin
from terminal import app as terminal_app
from terminal import store as terminal_store
from terminal import sync
from terminal.app import create_app, default_data_dir, legacy_data_dir, migrate_legacy_data
from terminal.store import Store, TerminalError

ROOT = Path(__file__).resolve().parents[2]
DAY = 24 * 3600
LONG_LEASE = patch.object(licensing, "MAX_LEASE_SECONDS", 45 * DAY)  # contract A, also before the cloud side lands
PIN_HASHES = {pin: hash_pin(pin) for pin in ("123987", "112233", "654321")}


class OfflineCase(unittest.TestCase):
    def setUp(self):
        LONG_LEASE.start()
        self.addCleanup(LONG_LEASE.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "terminal.sqlite3"
        self.app = create_app({"TESTING": True, "DATA_DIR": self.temp.name,
                               "DB_PATH": self.path, "START_WORKER": False})
        self.store = self.app.extensions["terminal_store"]
        self.client = self.app.test_client()
        self.tenant_id, self.shop_id, self.terminal_id = (str(uuid.uuid4()) for _ in range(3))
        self.cashier_id, self.second_id, self.manager_id = (str(uuid.uuid4()) for _ in range(3))
        self.product_id = str(uuid.uuid4())
        self.private, self.public = generate_keypair()
        self.release_private, self.release_public = generate_keypair()
        self.operators = [
            {"id": self.cashier_id, "name": "Ana", "role": "cashier", "pin_hash": PIN_HASHES["123987"]},
            {"id": self.second_id, "name": "Caio", "role": "cashier", "pin_hash": PIN_HASHES["112233"]},
            {"id": self.manager_id, "name": "Bruno", "role": "manager", "pin_hash": PIN_HASHES["654321"]},
        ]
        self.products = [{"id": self.product_id, "sku": "ARROZ", "barcode": "7890000000002", "name": "Arroz",
                          "unit": "UN", "category": "Mercearia", "price_cents": 10000,
                          "quantity_milli": 50000, "active": True}]
        self.lease = self.signed()
        self.pairing = {"token": "t" * 48, "tenant_id": self.tenant_id, "shop_id": self.shop_id,
                        "terminal_id": self.terminal_id, "lease": self.lease, "public_key": self.public}
        self.store.set_pairing(self.pairing, "http://127.0.0.1:8000")
        self.bootstrap = {"products": self.products, "operators": self.operators,
                          "lease": self.lease, "public_key": self.public}
        self.store.apply_bootstrap(self.bootstrap)
        self.cashier = {"id": self.cashier_id, "name": "Ana", "role": "cashier"}
        self.second = {"id": self.second_id, "name": "Caio", "role": "cashier"}

    def signed(self, issued=None, lifetime=3600, device_id=None):
        issued = int(time.time()) - 5 if issued is None else issued
        claims = {"schema": 1, "tenant_id": self.tenant_id, "shop_id": self.shop_id,
                  "terminal_id": self.terminal_id, "device_id": device_id or self.store.identity()["device_id"],
                  "issued_at": issued, "expires_at": issued + lifetime, "features": ["pdv"],
                  "status": "active", "operators_hash": payload_digest(self.operators)}
        return sign_lease(claims, self.private)

    def set_lease(self, lease):
        with self.store.transaction() as conn:
            self.store._set_meta(conn, "lease", json.dumps(lease))

    def meta(self, key):
        return self.store.meta().get(key)

    def sell(self, request_id="venda-1", operator=None):
        return self.store.sale(operator or self.cashier, {
            "request_id": request_id, "items": [{"product_id": self.product_id, "quantity_milli": 1000}],
            "payments": [{"method": "cash", "amount_cents": 10000}]})

    def csrf(self):
        return self.client.get("/api/session").json["csrf"]

    def login(self, operator_id=None, pin="123987"):
        response = self.client.post("/api/login", json={"operator_id": operator_id or self.cashier_id, "pin": pin},
                                    headers={"X-CSRF-Token": self.csrf()})
        self.assertEqual(response.status_code, 200, response.json)

    def post(self, path, payload=None):
        return self.client.post(path, json=payload or {}, headers={"X-CSRF-Token": self.csrf()})

    def lease_answer(self, lease=None, **extra):
        lease = lease or self.signed(issued=int(time.time()), lifetime=30 * DAY)
        return {"lease": lease, "public_key": self.public, "operators": self.operators,
                "server_time": lease["payload"]["issued_at"], "release_public_key": self.release_public, **extra}


class QueueOrderTests(OfflineCase):
    def test_clock_running_backwards_never_inverts_sale_and_void(self):
        self.store.open_cash(self.cashier_id, 0)
        clock = iter(range(9_000_000_000_000, 0, -3_600_000))  # every reading is one hour earlier
        with patch.object(terminal_store, "_now_ms", side_effect=lambda: next(clock)):
            sale = self.sell()
            self.store.void_sale(self.cashier, sale["id"], "Cliente desistiu", "654321")
            self.store.cash_movement(self.cashier_id, "supplied", 500, "Troco")
        with self.store.connection() as conn:
            rows = conn.execute("SELECT event_type,created_at,seq FROM outbox ORDER BY seq").fetchall()
        self.assertEqual([row["event_type"] for row in rows],
                         ["cash.opened", "sale.completed", "sale.voided", "cash.supplied"])
        self.assertEqual([row["seq"] for row in rows], [1, 2, 3, 4])
        self.assertGreater(rows[1]["created_at"], rows[2]["created_at"])  # the clock did go backwards
        self.assertEqual([event["type"] for event in self.store.pending_events()],
                         ["cash.opened", "sale.completed", "sale.voided", "cash.supplied"])
        self.assertEqual([event["type"] for event in self.store.export_data()["pending_events"]],
                         ["cash.opened", "sale.completed", "sale.voided", "cash.supplied"])
        # Stock: the void gives back what the sale took, in that order.
        self.assertEqual(self.store._pending_stock_deltas(sqlite3.connect(self.path))[self.product_id], 0)

    def test_existing_database_gets_seq_in_the_order_it_was_written(self):
        old = Path(self.temp.name) / "antigo.sqlite3"
        with sqlite3.connect(old) as conn:
            conn.execute("""CREATE TABLE outbox(id TEXT PRIMARY KEY,event_type TEXT NOT NULL,aggregate_id TEXT NOT NULL,
                            payload TEXT NOT NULL,created_at INTEGER NOT NULL,acked_at INTEGER)""")
            # Written sale first, void second, with a clock that went back in between.
            rows = [("b", "sale.completed", "s1", 2000, None), ("a", "sale.voided", "s1", 1000, None),
                    ("c", "cash.closed", "c1", 1500, 5)]
            conn.executemany("INSERT INTO outbox VALUES (?,?,?,?,?,?)",
                             [(row[0], row[1], row[2], json.dumps({"type": row[1]}), row[3], row[4]) for row in rows])
        migrated = Store(old)
        self.assertEqual([event["type"] for event in migrated.pending_events()], ["sale.completed", "sale.voided"])
        with migrated.connection() as conn:
            self.assertEqual([tuple(row) for row in conn.execute("SELECT id,seq FROM outbox ORDER BY seq")],
                             [("b", 1), ("a", 2), ("c", 3)])
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("INSERT INTO outbox(id,event_type,aggregate_id,payload,created_at,seq) "
                             "VALUES ('d','x','y','{}',1,2)")
        # Opening it again changes nothing.
        with Store(old).connection() as conn:
            self.assertEqual([row[0] for row in conn.execute("SELECT seq FROM outbox ORDER BY rowid")], [1, 2, 3])


class LastSeenTests(OfflineCase):
    def test_reading_the_license_does_not_write_every_time(self):
        now = int(time.time())
        with self.store.transaction() as conn:
            self.store._set_meta(conn, "last_seen", str(now - 10))
        for _ in range(3):
            self.assertEqual(self.store.lease_status()["state"], "active")
        self.assertEqual(self.meta("last_seen"), str(now - 10))
        with self.store.transaction() as conn:
            self.store._set_meta(conn, "last_seen", str(now - 120))
        self.store.lease_status()
        self.assertGreaterEqual(int(self.meta("last_seen")), now)

    def test_rollback_is_still_detected_and_reads_never_move_the_mark_back(self):
        future = int(time.time()) + 3600
        with self.store.transaction() as conn:
            self.store._set_meta(conn, "last_seen", str(future))
        status = self.store.lease_status()
        self.assertEqual(status["state"], "clock_rollback")
        self.assertFalse(status["can_sell"])
        self.assertEqual(status["clock"]["problem"], "behind")
        self.assertEqual(status["clock"]["reference_time"], future)
        self.assertEqual(self.meta("last_seen"), str(future))
        self.store.touch_last_seen(future - 500)
        self.assertEqual(self.meta("last_seen"), str(future))

    def test_license_is_still_read_while_another_writer_holds_the_database(self):
        with self.store.transaction() as conn:
            self.store._set_meta(conn, "last_seen", "1")
        blocker = sqlite3.connect(self.path)
        blocker.execute("BEGIN IMMEDIATE")
        try:
            with patch.object(Store, "transaction", side_effect=sqlite3.OperationalError("database is locked")):
                self.assertEqual(self.store.lease_status()["state"], "active")
        finally:
            blocker.rollback()
            blocker.close()


class ManagerLockoutTests(OfflineCase):
    def test_wrong_pins_lock_only_the_operator_who_asked(self):
        for _ in range(5):
            with self.assertRaisesRegex(TerminalError, "PIN de gerente inválido"):
                self.store.manager_override("000000", self.cashier_id)
        with self.assertRaises(TerminalError) as blocked:
            self.store.manager_override("654321", self.cashier_id)
        self.assertEqual(blocked.exception.status, 429)
        # The other cashier of the shop still gets the manager's authorization.
        self.assertEqual(self.store.manager_override("654321", self.second_id), self.manager_id)
        self.store.open_cash(self.second_id, 0)
        self.assertEqual(self.store.close_cash(self.second_id, 0, "654321")["expected_cents"], 0)

    def test_bootstrap_does_not_give_the_attempts_back(self):
        for _ in range(5):
            with self.assertRaises(TerminalError):
                self.store.manager_override("000000", self.cashier_id)
        self.store.apply_bootstrap(self.bootstrap)
        with self.assertRaisesRegex(TerminalError, "Muitas tentativas"):
            self.store.manager_override("654321", self.cashier_id)

    def test_success_clears_only_the_requesters_counter(self):
        for _ in range(4):
            with self.assertRaises(TerminalError):
                self.store.manager_override("000000", self.cashier_id)
        self.assertEqual(self.store.manager_override("654321", self.cashier_id), self.manager_id)
        for _ in range(4):
            with self.assertRaisesRegex(TerminalError, "inválido"):
                self.store.manager_override("000000", self.cashier_id)

    def test_store_wide_ceiling_stops_a_brute_force_spread_over_operators(self):
        with patch.object(terminal_store, "MANAGER_OVERRIDE_GLOBAL_FAILURES", 6):
            for requester in (self.cashier_id, self.second_id) * 3:
                with self.assertRaisesRegex(TerminalError, "inválido"):
                    self.store.manager_override("000000", requester)
            with self.assertRaises(TerminalError) as blocked:
                self.store.manager_override("654321", self.manager_id)
            self.assertEqual(blocked.exception.status, 429)
        self.assertGreater(terminal_store.MANAGER_OVERRIDE_GLOBAL_FAILURES, 4 * terminal_store.MANAGER_OVERRIDE_FAILURES)

    def test_store_wide_counter_forgets_old_typos(self):
        with patch.object(terminal_store, "MANAGER_OVERRIDE_GLOBAL_FAILURES", 4):
            for requester in (self.cashier_id, self.second_id, self.manager_id):
                with self.assertRaises(TerminalError):
                    self.store.manager_override("000000", requester)
            with self.store.transaction() as conn:  # the window started long ago
                self.store._set_meta(conn, "manager_override_window", str(int(time.time()) - 3600))
            with self.assertRaisesRegex(TerminalError, "inválido"):
                self.store.manager_override("000000", str(uuid.uuid4()))
            self.assertEqual(self.store.manager_override("654321", self.second_id), self.manager_id)

    def test_maintenance_errors_never_lock_the_cashier_authorizations(self):
        for _ in range(5):
            self.assertEqual(self.post("/api/unpair", {"confirm": True, "manager_pin": "111111"}).status_code, 403)
        self.assertEqual(self.post("/api/new-profile", {"confirm": True, "manager_pin": "654321"}).status_code, 429)
        self.login()
        response = self.post("/api/discount/authorize", {"pin": "654321", "gross_cents": 10000, "discount_cents": 100})
        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual(self.store.manager_override("654321", self.second_id), self.manager_id)

    def test_cashier_errors_do_not_lock_maintenance(self):
        self.login()
        for _ in range(5):
            response = self.post("/api/discount/authorize", {"pin": "000000", "gross_cents": 10000, "discount_cents": 100})
            self.assertEqual(response.status_code, 403)
        self.assertEqual(self.post("/api/discount/authorize",
                                   {"pin": "654321", "gross_cents": 10000, "discount_cents": 100}).status_code, 429)
        self.assertEqual(self.store.manager_override("654321"), self.manager_id)


class DamagedDatabaseTests(OfflineCase):
    def damage(self):
        self.store.open_cash(self.cashier_id, 0)
        backup = self.store.backup_now()
        for suffix in ("-wal", "-shm"):
            Path(str(self.path) + suffix).unlink(missing_ok=True)
        self.path.write_bytes(b"isto nao e um banco sqlite " * 200)
        return backup

    def test_quick_check_reports_health(self):
        self.assertIsNone(Store.quick_check(self.path))
        self.assertIsNone(Store.quick_check(Path(self.temp.name) / "nao-existe.sqlite3"))
        self.damage()
        self.assertTrue(Store.quick_check(self.path))

    def test_damaged_database_starts_without_selling_and_lists_backups(self):
        backup = self.damage()
        with self.assertLogs("terminal.app", level="ERROR"):
            app = create_app({"TESTING": True, "DATA_DIR": self.temp.name, "DB_PATH": self.path,
                              "START_WORKER": True})
        self.assertIsNone(app.extensions["terminal_store"])
        self.assertNotIn("terminal_worker", app.extensions)
        client = app.test_client()
        self.assertEqual(client.get("/").status_code, 200)
        health = client.get("/health")
        self.assertEqual(health.status_code, 503)
        self.assertEqual(health.json["database"], "damaged")
        state = client.get("/api/session").json
        self.assertEqual(state["damaged"]["database"], str(self.path))
        self.assertEqual([copy["name"] for copy in state["damaged"]["backups"]], [backup["name"]])
        self.assertTrue(Path(state["damaged"]["backups"][0]["path"]).exists())
        self.assertTrue(state["damaged"]["problem"])
        for response in (client.get("/api/products"),
                         client.post("/api/login", json={}, headers={"X-CSRF-Token": state["csrf"]}),
                         client.post("/api/sales", json={}, headers={"X-CSRF-Token": state["csrf"]})):
            self.assertEqual(response.status_code, 503)
            self.assertIn("danificado", response.json["error"])
        self.assertEqual(client.get("/api/session", headers={"Host": "evil.example"}).status_code, 403)
        # The damaged file is left exactly as found.
        self.assertTrue(self.path.read_bytes().startswith(b"isto nao e um banco"))

    def test_restoring_the_listed_backup_brings_the_terminal_back(self):
        backup = self.damage()
        self.path.write_bytes((Path(backup["directory"]) / backup["name"]).read_bytes())
        app = create_app({"TESTING": True, "DATA_DIR": self.temp.name, "DB_PATH": self.path, "START_WORKER": False})
        self.assertEqual(app.test_client().get("/health").json, {"status": "ok", "service": "terminal-local"})
        self.assertTrue(app.extensions["terminal_store"].cash_state())

    def reopen(self):
        return create_app({"TESTING": True, "DATA_DIR": self.temp.name, "DB_PATH": self.path, "START_WORKER": False})

    def test_failed_migration_on_healthy_database_is_not_damage(self):
        self.store.open_cash(self.cashier_id, 0)
        self.store.backup_now()
        before = self.path.read_bytes()
        for message in ("duplicate column name: seq", "no such column: seq", "disk I/O error",
                        "unable to open database file", "database is locked"):
            with self.subTest(message=message), patch.object(
                    Store, "_migrate_outbox_seq", side_effect=sqlite3.OperationalError(message)):
                # The process must die so the updater rolls back; no restore screen.
                with self.assertRaisesRegex(sqlite3.OperationalError, message):
                    self.reopen()
        self.assertIsNone(Store.quick_check(self.path))
        self.assertTrue(self.path.read_bytes().startswith(before[:16]))
        app = self.reopen()
        client = app.test_client()
        self.assertEqual(client.get("/health").status_code, 200)
        self.assertNotIn("damaged", client.get("/api/session").json)

    def test_corruption_found_while_opening_still_enters_damaged_mode(self):
        self.store.backup_now()
        with patch.object(Store, "_migrate_outbox_seq",
                          side_effect=sqlite3.DatabaseError("database disk image is malformed")), \
                self.assertLogs("terminal.app", level="ERROR"):
            app = self.reopen()
        client = app.test_client()
        self.assertEqual(client.get("/health").status_code, 503)
        self.assertIn("malformed", client.get("/api/session").json["damaged"]["problem"])

    def test_quick_check_does_not_call_an_unreadable_database_damaged(self):
        for message in ("unable to open database file", "database is locked", "disk I/O error"):
            with self.subTest(message=message), patch.object(
                    terminal_store.sqlite3, "connect", side_effect=sqlite3.OperationalError(message)):
                with self.assertRaises(sqlite3.OperationalError):
                    Store.quick_check(self.path)
                with self.assertRaises(sqlite3.OperationalError):
                    self.reopen()

    def test_is_corruption_only_for_damaged_files(self):
        self.assertTrue(Store.is_corruption(sqlite3.DatabaseError("file is not a database")))
        self.assertTrue(Store.is_corruption(sqlite3.DatabaseError("database disk image is malformed")))
        self.assertFalse(Store.is_corruption(sqlite3.OperationalError("duplicate column name: seq")))
        self.assertFalse(Store.is_corruption(sqlite3.IntegrityError("malformed")))
        self.assertFalse(Store.is_corruption(ValueError("malformed")))

    def test_screen_teaches_how_to_restore(self):
        html = self.client.get("/").get_data(as_text=True)
        script = (ROOT / "terminal" / "static" / "terminal.js").read_text(encoding="utf-8")
        self.assertIn('id="damagedView"', html)
        self.assertIn('id="damagedBackups"', html)
        self.assertIn("fresh.damaged", script)


class DataDirectoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.legacy, self.target = self.root / "LocalAppData" / "PDV2", self.root / "ProgramData" / "PDV2"

    def old_install(self):
        store = Store(self.legacy / "terminal.sqlite3")
        with store.transaction() as conn:
            store._set_meta(conn, "tenant_name", "Mercearia Aurora")
        profile = Store(self.legacy / "profiles" / "perfil-abc" / "terminal.sqlite3")
        with profile.transaction() as conn:
            profile._set_meta(conn, "tenant_name", "Outra Empresa")
        (self.legacy / "active-profile.json").write_text('{"profile": "perfil-abc"}', encoding="utf-8")
        (self.legacy / "session.key").write_bytes(b"k" * 32)
        return store.backup_now()

    def test_windows_default_is_program_data_and_other_systems_keep_theirs(self):
        self.assertEqual(default_data_dir("nt", {"ProgramData": str(self.root / "ProgramData"),
                                                 "LOCALAPPDATA": str(self.root / "LocalAppData")}), self.target)
        self.assertEqual(default_data_dir("nt", {}).name, "PDV2")
        self.assertIn("ProgramData", str(default_data_dir("nt", {})))
        self.assertEqual(default_data_dir("posix", {}, home=self.root), self.root / ".local" / "share" / "pdv2")
        self.assertEqual(legacy_data_dir("nt", {"LOCALAPPDATA": str(self.root / "LocalAppData")}), self.legacy)
        self.assertEqual(legacy_data_dir("nt", {"LOCALAPPDATA": "x", "PDV_TERMINAL_LEGACY_DATA": str(self.root)}),
                         self.root)
        self.assertIsNone(legacy_data_dir("nt", {}))
        self.assertIsNone(legacy_data_dir("posix", {"LOCALAPPDATA": "x"}))

    def test_migration_copies_everything_once_and_never_touches_the_origin(self):
        backup = self.old_install()
        before = {path.relative_to(self.legacy): path.stat().st_size
                  for path in self.legacy.rglob("*") if path.is_file() and not path.name.endswith(("-wal", "-shm"))}
        self.assertEqual(migrate_legacy_data(self.legacy, self.target), self.target)
        self.assertEqual(Store(self.target / "terminal.sqlite3").meta()["tenant_name"], "Mercearia Aurora")
        self.assertEqual(Store(self.target / "profiles" / "perfil-abc" / "terminal.sqlite3").meta()["tenant_name"],
                         "Outra Empresa")
        self.assertEqual((self.target / "session.key").read_bytes(), b"k" * 32)
        self.assertTrue((self.target / "backups" / backup["name"]).exists())
        self.assertEqual(json.loads((self.target / terminal_app.MIGRATION_MARKER).read_text())["source"],
                         str(self.legacy))
        self.assertFalse((self.target / ".migrating").exists())
        after = {path.relative_to(self.legacy): path.stat().st_size
                 for path in self.legacy.rglob("*") if path.is_file() and not path.name.endswith(("-wal", "-shm"))}
        self.assertEqual(after, before)
        # Second start: the new directory is the truth, the old one is not copied over it.
        fresh = Store(self.target / "terminal.sqlite3")
        with fresh.transaction() as conn:
            fresh._set_meta(conn, "tenant_name", "Mudou depois")
        self.assertEqual(migrate_legacy_data(self.legacy, self.target), self.target)
        self.assertEqual(fresh.meta()["tenant_name"], "Mudou depois")

    def test_migration_never_overwrites_a_database_already_in_the_new_place(self):
        self.old_install()
        current = Store(self.target / "terminal.sqlite3")
        with current.transaction() as conn:
            current._set_meta(conn, "tenant_name", "Já estava aqui")
        self.assertEqual(migrate_legacy_data(self.legacy, self.target), self.target)
        self.assertEqual(current.meta()["tenant_name"], "Já estava aqui")
        self.assertFalse((self.target / "profiles").exists())

    def test_failed_copy_keeps_using_the_old_place_and_leaves_nothing_half_done(self):
        self.old_install()
        (self.legacy / "profiles" / "perfil-abc" / "terminal.sqlite3").write_bytes(b"lixo " * 500)
        self.assertEqual(migrate_legacy_data(self.legacy, self.target), self.legacy)
        self.assertFalse((self.target / "terminal.sqlite3").exists())
        self.assertFalse((self.target / ".migrating").exists())
        self.assertFalse((self.target / terminal_app.MIGRATION_MARKER).exists())
        self.assertTrue((self.legacy / "terminal.sqlite3").exists())

    def test_nothing_to_migrate_is_not_an_error(self):
        self.assertEqual(migrate_legacy_data(self.legacy, self.target), self.target)
        self.assertEqual(migrate_legacy_data(self.target, self.target), self.target)

    def test_app_start_migrates_only_when_no_directory_was_chosen(self):
        self.old_install()
        with patch.object(terminal_app, "default_data_dir", return_value=self.target), \
             patch.object(terminal_app, "legacy_data_dir", return_value=self.legacy), \
             patch.dict("os.environ", clear=False) as env:
            env.pop("PDV_TERMINAL_DATA", None)
            env.pop("PDV_TERMINAL_DB", None)
            app = create_app({"TESTING": True})
            store = app.extensions["terminal_store"]
            self.assertEqual(store.path, self.target / "profiles" / "perfil-abc" / "terminal.sqlite3")
            self.assertEqual(store.meta()["tenant_name"], "Outra Empresa")
            chosen = self.root / "escolhido"
            env["PDV_TERMINAL_DATA"] = str(chosen)  # still has precedence, and is never migrated into
            other = create_app({"TESTING": True}).extensions["terminal_store"]
            self.assertEqual(other.path, chosen / "terminal.sqlite3")
            self.assertNotIn("tenant_name", other.meta())


class DatabaseErrorTests(OfflineCase):
    def test_only_a_locked_or_unreadable_database_is_reported_as_busy(self):
        self.login()
        cases = [(sqlite3.OperationalError("database is locked"), 503),
                 (sqlite3.OperationalError("unable to open database file"), 503),
                 (sqlite3.DatabaseError("database disk image is malformed"), 503),
                 (sqlite3.IntegrityError("UNIQUE constraint failed: sales.request_id"), 500),
                 (sqlite3.OperationalError("no such column: seq"), 500),
                 (sqlite3.ProgrammingError("Incorrect number of bindings supplied"), 500)]
        for error, status in cases:
            with self.subTest(error=str(error)), self.assertLogs("terminal.app", level="ERROR"), \
                 patch.object(Store, "product_search", side_effect=error):
                response = self.client.get("/api/products")
                self.assertEqual(response.status_code, status)
                self.assertNotIn(str(error), response.json["error"])
                self.assertEqual("ocupado" in response.json["error"], status == 503)


class LongLeaseTests(OfflineCase):
    def test_every_cloud_call_declares_the_long_lease_capability(self):
        fresh = Store(Path(self.temp.name) / "novo.sqlite3")
        lease = self.signed(device_id=fresh.identity()["device_id"])
        with patch("terminal.sync._request", return_value=dict(self.pairing, lease=lease)) as send, \
             patch("terminal.sync.refresh_bootstrap", return_value={}):
            sync.pair(fresh, "http://127.0.0.1:8000", "PDV2-LICENSE-7B2C", "codigo-temporario")
        self.assertEqual(send.call_args.kwargs["headers"], {"X-PDV-Capabilities": "long-lease"})
        self.store.open_cash(self.cashier_id, 0)
        ids = [event["event_id"] for event in self.store.pending_events()]
        calls = {}

        def cloud(method, url, **kwargs):
            name = url.split("/api/v1/")[1].split("?")[0]
            calls[name] = kwargs.get("headers") or {}
            if name == "terminal/bootstrap":
                return dict(self.bootstrap, next_cursor=None)
            if name == "terminal/sync":
                return {"accepted_event_ids": ids}
            if name == "terminal/lease":
                return self.lease_answer()
            return {"ok": True, "release": None}

        with patch("terminal.sync._request", side_effect=cloud):
            sync.heartbeat_once(self.store)
            sync.sync_once(self.store)
            sync.refresh_bootstrap(self.store)
            sync.renew_lease(self.store, force=True)
        for name in ("terminal/heartbeat", "terminal/sync", "terminal/bootstrap", "terminal/lease"):
            self.assertEqual(calls[name], {"Authorization": "Bearer " + "t" * 48,
                                           "X-PDV-Capabilities": "long-lease"}, name)

    def test_lease_route_renews_for_thirty_days_and_reanchors_the_clock(self):
        with self.store.transaction() as conn:
            self.store._set_meta(conn, "last_seen", "1000")
        answer = self.lease_answer()
        with patch("terminal.sync._request", return_value=answer) as send:
            result = sync.renew_lease(self.store)
            self.assertIsNone(sync.renew_lease(self.store))  # at most once an hour
        self.assertEqual(send.call_count, 1)
        self.assertEqual(send.call_args.args, ("POST", "http://127.0.0.1:8000/api/v1/terminal/lease"))
        self.assertEqual(result["expires_at"], answer["lease"]["payload"]["expires_at"])
        self.assertEqual(self.meta("last_seen"), str(answer["server_time"]))
        status = self.store.lease_status()
        self.assertEqual((status["state"], status["warning"]), ("active", False))
        self.assertEqual(status["offline_seconds_max"], 30 * DAY)
        self.assertIn(status["days_left"], (29, 30))
        with self.store.transaction() as conn:  # one hour later
            self.store._set_meta(conn, "lease_renewed_at", str(int(time.time() * 1000) - 3_600_001))
        with patch("terminal.sync._request", return_value=self.lease_answer()) as send:
            self.assertIsNotNone(sync.renew_lease(self.store))

    def test_cloud_without_the_route_is_not_an_error(self):
        missing = TerminalError("Servidor recusou pedido (404): erro", 502)
        missing.remote_status = 404
        with patch("terminal.sync._request", side_effect=missing) as send:
            self.assertIsNone(sync.renew_lease(self.store))
            self.assertIsNone(sync.renew_lease(self.store))
        self.assertEqual(send.call_count, 1)
        with patch("terminal.sync._request", return_value={"ok": True}):
            self.assertIsNone(sync.renew_lease(self.store, force=True))
        self.assertEqual(self.store.lease_status()["expires_at"], self.lease["payload"]["expires_at"])
        with patch("terminal.sync._request", side_effect=TerminalError("Sem conexão", 503)), \
             self.assertRaisesRegex(TerminalError, "Sem conexão"):
            sync.renew_lease(self.store, force=True)

    def test_forged_or_foreign_lease_is_refused(self):
        other_private, other_public = generate_keypair()
        claims = dict(self.lease["payload"], issued_at=int(time.time()), expires_at=int(time.time()) + DAY)
        cases = [self.lease_answer(sign_lease(claims, other_private)),
                 self.lease_answer(public_key=other_public),
                 self.lease_answer(self.signed(issued=int(time.time()), device_id="outro-dispositivo")),
                 self.lease_answer(operators=self.operators[:1])]
        for answer in cases:
            with patch("terminal.sync._request", return_value=answer), self.assertRaises(TerminalError):
                sync.renew_lease(self.store, force=True)
        self.assertEqual(self.store.lease_status()["expires_at"], self.lease["payload"]["expires_at"])
        self.assertEqual(len(self.store.operators()), 3)

    def test_replayed_old_lease_never_extends_or_moves_the_clock(self):
        old = self.signed(issued=int(time.time()) - 10 * DAY, lifetime=5 * DAY)  # already expired
        with self.assertRaisesRegex(TerminalError, "inválida"):
            self.store.apply_lease(self.lease_answer(old))
        self.assertEqual(self.store.lease_status()["state"], "active")

    def test_worker_renews_after_a_contact_that_worked_and_only_then(self):
        seen = []

        def cloud(method, url, **kwargs):
            seen.append(url.split("/api/v1/")[1].split("?")[0])
            if "bootstrap" in url:
                return dict(self.bootstrap, next_cursor=None)
            if url.endswith("/lease"):
                return self.lease_answer()
            return {"ok": True, "release": None}

        with patch("terminal.sync._request", side_effect=cloud):
            self.assertEqual(sync.Worker(self.store).run_once(), [])
            self.assertEqual(sync.Worker(self.store).run_once(), [])
        self.assertEqual(seen.count("terminal/lease"), 1)
        self.assertEqual(self.store.lease_status()["offline_seconds_max"], 30 * DAY)
        with self.store.transaction() as conn:
            self.store._set_meta(conn, "lease_renewed_at", "0")
            self.store._set_meta(conn, "last_heartbeat_at", "0")
        seen.clear()
        with patch("terminal.sync._request", side_effect=TerminalError("Sem conexão", 503)) as send:
            errors = sync.Worker(self.store).run_once()
        self.assertNotIn("terminal/lease", [call.args[1].split("/api/v1/")[1] for call in send.call_args_list])
        self.assertFalse(any(error.startswith("Licença") for error in errors))

    def test_warning_starts_seven_days_before_the_end(self):
        now = int(time.time())
        self.set_lease(self.signed(issued=now - 22 * DAY, lifetime=30 * DAY))
        self.assertFalse(self.store.lease_status()["warning"])
        self.set_lease(self.signed(issued=now - 25 * DAY + 600, lifetime=30 * DAY))
        status = self.store.lease_status()
        self.assertTrue(status["warning"] and status["can_sell"] and status["can_open_cash"])
        self.assertEqual(status["days_left"], 5)
        self.assertIn("vence em 5 dias", status["reason"])
        self.set_lease(self.signed(issued=now - 30 * DAY + 3600, lifetime=30 * DAY))
        self.assertIn("vence hoje", self.store.lease_status()["reason"])
        # A terminal that is online renews all the time: a short lease alone is no reason to warn.
        self.set_lease(self.signed(issued=now - 60, lifetime=3 * DAY))
        self.assertFalse(self.store.lease_status()["warning"])
        self.login()
        self.set_lease(self.signed(issued=now - 25 * DAY + 600, lifetime=30 * DAY))
        self.assertTrue(self.client.get("/api/session").json["license"]["warning"])
        self.assertTrue(self.client.get("/api/sync").json["license"]["warning"])

    def test_expired_lease_keeps_the_open_cashier_selling_but_opens_no_new_one(self):
        now = int(time.time())
        self.store.open_cash(self.cashier_id, 0)
        self.set_lease(self.signed(issued=now - 30 * DAY - 60, lifetime=30 * DAY))
        status = self.store.lease_status()
        self.assertEqual((status["state"], status["can_sell"], status["can_open_cash"]), ("expired", True, False))
        self.assertIn("continua vendendo", status["reason"])
        self.assertEqual(self.sell()["total_cents"], 10000)
        self.store.cash_movement(self.cashier_id, "withdrawn", 1000, "Sangria")
        self.assertEqual(self.store.close_cash(self.cashier_id, 9000, "654321")["difference_cents"], 0)
        status = self.store.lease_status()
        self.assertEqual((status["can_sell"], status["can_open_cash"]), (False, False))
        with self.assertRaisesRegex(TerminalError, "abrir um novo caixa") as blocked:
            self.store.open_cash(self.cashier_id, 0)
        self.assertEqual(blocked.exception.status, 403)
        with self.assertRaises(TerminalError):
            self.sell("venda-2")
        # Renewing brings everything back.
        self.store.apply_lease(self.lease_answer())
        self.store.open_cash(self.cashier_id, 0)
        self.assertEqual(self.sell("venda-3")["total_cents"], 10000)

    def test_open_cashier_does_not_sell_forever_on_an_expired_lease(self):
        self.store.open_cash(self.cashier_id, 0)
        lease = self.signed(issued=int(time.time()) - 30 * DAY - 60, lifetime=30 * DAY)
        self.set_lease(lease)
        limit = lease["payload"]["expires_at"] + terminal_store.EXPIRED_SHIFT_GRACE_SECONDS
        self.assertTrue(self.store.lease_status(now=limit - 1)["can_sell"])
        self.assertFalse(self.store.lease_status(now=limit + 1)["can_sell"])

    def test_tampered_expired_lease_gets_no_grace(self):
        self.store.open_cash(self.cashier_id, 0)
        lease = self.signed(issued=int(time.time()) - 2 * DAY, lifetime=DAY)
        lease["payload"]["features"] = ["pdv", "extra"]
        self.set_lease(lease)
        status = self.store.lease_status()
        self.assertEqual((status["state"], status["can_sell"]), ("invalid", False))

    def test_clock_behind_is_explained_instead_of_only_blocking(self):
        real = int(time.time())
        with patch("time.time", return_value=real - 2 * 3600):  # computer two hours late
            self.store.apply_lease(self.lease_answer(self.signed(issued=real, lifetime=30 * DAY)))
            status = self.store.lease_status()
            self.assertEqual((status["state"], status["can_sell"]), ("clock_rollback", False))
            self.assertEqual(status["clock"], {"problem": "behind", "computer_time": real - 2 * 3600,
                                               "reference_time": real})
        # The operator fixed the time: nothing else is needed.
        self.assertEqual(self.store.lease_status()["state"], "active")

    def test_clock_ahead_is_explained_and_fixing_it_releases_the_cashier(self):
        real = int(time.time())
        with patch("time.time", return_value=real + 60 * DAY):  # computer two months ahead
            self.store.apply_lease(self.lease_answer(self.signed(issued=real, lifetime=30 * DAY)))
            status = self.store.lease_status()
            self.assertEqual((status["state"], status["can_sell"]), ("expired", False))
            self.assertEqual(status["clock"]["problem"], "ahead")
            self.assertEqual(status["clock"]["reference_time"], real)
            self.assertIn("adiantado", status["reason"])
        status = self.store.lease_status()
        self.assertEqual(status["state"], "active")
        self.assertNotIn("clock", status)

    def test_recheck_route_renews_online_and_answers_offline(self):
        self.login()
        self.set_lease(self.signed(issued=int(time.time()) - 2 * DAY, lifetime=DAY))
        with patch("terminal.sync._request", side_effect=TerminalError("Sem conexão com o servidor", 503)):
            response = self.post("/api/license/renew")
        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual((response.json["renewed"], response.json["license"]["state"]), (False, "expired"))
        self.assertIn("Sem conexão", response.json["error"])
        with patch("terminal.sync._request", return_value=self.lease_answer()):
            response = self.post("/api/license/renew")
        self.assertEqual((response.json["renewed"], response.json["license"]["state"]), (True, "active"))

    def test_screen_has_the_warning_and_the_clock_guide(self):
        html = self.client.get("/").get_data(as_text=True)
        script = (ROOT / "terminal" / "static" / "terminal.js").read_text(encoding="utf-8")
        self.assertIn('id="leaseCard"', html)
        for piece in ("Licença vence em", "Acerte a data e a hora deste computador", "recheckLicense",
                      "/api/license/renew"):
            self.assertIn(piece, script)


class ReleaseKeyTests(OfflineCase):
    def manifest(self, private, version="2.0.1", **extra):
        return sign_release({"schema": 1, "purpose": "pdv2-release", "component": "terminal", "channel": "stable",
                             "version": version, "url": "https://updates.example.test/terminal.zip",
                             "sha256": "a" * 64, "size_bytes": 1200, **extra}, private)

    def test_release_key_is_pinned_on_first_authenticated_answer_and_used(self):
        self.assertIsNone(self.meta("release_public_key"))
        self.store.apply_bootstrap(dict(self.bootstrap, release_public_key=self.release_public))
        self.assertEqual(self.meta("release_public_key"), self.release_public)
        envelope = self.manifest(self.release_private)
        with patch("terminal.sync._request", return_value={"release": envelope, "public_key": self.release_public}):
            self.assertEqual(sync.check_release(self.store)["version"], "2.0.1")
        # A manifest signed with the license key is no longer accepted.
        with patch("terminal.sync._request", return_value={"release": self.manifest(self.private),
                                                           "public_key": self.release_public}), \
             self.assertRaisesRegex(TerminalError, "assinatura válida"):
            sync.check_release(self.store)
        with patch("terminal.sync._request", return_value={"release": self.manifest(self.private),
                                                           "public_key": self.public}), \
             self.assertRaisesRegex(TerminalError, "diferente da chave fixada"):
            sync.check_release(self.store)

    def test_pinned_key_is_never_replaced_by_a_later_answer(self):
        self.store.apply_lease(self.lease_answer())
        self.assertEqual(self.meta("release_public_key"), self.release_public)
        _, another = generate_keypair()
        self.store.apply_bootstrap(dict(self.bootstrap, release_public_key=another))
        self.store.apply_lease(self.lease_answer(release_public_key=another))
        self.assertEqual(self.meta("release_public_key"), self.release_public)

    def test_pairing_pins_the_key_and_garbage_is_ignored(self):
        fresh = Store(Path(self.temp.name) / "novo.sqlite3")
        lease = self.signed(device_id=fresh.identity()["device_id"])
        fresh.set_pairing(dict(self.pairing, lease=lease, release_public_key=self.release_public),
                          "http://127.0.0.1:8000")
        self.assertEqual(fresh.meta()["release_public_key"], self.release_public)
        self.store.apply_bootstrap(dict(self.bootstrap, release_public_key="isto não é uma chave"))
        self.assertIsNone(self.meta("release_public_key"))

    def test_terminal_that_never_got_a_release_key_keeps_using_the_license_key(self):
        with patch("terminal.sync._request", return_value={"release": self.manifest(self.private),
                                                           "public_key": self.public}):
            self.assertEqual(sync.check_release(self.store)["version"], "2.0.1")

    def test_unpairing_forgets_the_pinned_key(self):
        self.store.apply_lease(self.lease_answer())
        with self.store.transaction() as conn:
            self.store._clear_connection_meta(conn)
        self.assertIsNone(self.meta("release_public_key"))


class FakeUpdater:
    """Stand-in for terminal/updater.py (contract C)."""

    def __init__(self):
        self.steps, self.applied = [], 0
        self.state = {"current_version": "2.0.0", "available": {"version": "2.1.0", "notes": "Novidades"},
                      "staged_version": "2.1.0", "state": "staged", "error": "", "can_apply": True,
                      "blocked_reason": ""}

    def status(self, store):
        return dict(self.state)

    def auto_step(self, store, online):
        self.steps.append(online)

    def request_apply(self, store):
        self.applied += 1
        return dict(self.state, state="applying")


class UpdaterHookTests(OfflineCase):
    def use(self, module):
        watcher = patch.object(sync, "updater", module)
        watcher.start()
        self.addCleanup(watcher.stop)
        return module

    def cloud(self, method, url, **kwargs):
        if "bootstrap" in url:
            return dict(self.bootstrap, next_cursor=None)
        return {"ok": True, "release": None}

    def test_worker_calls_auto_step_and_says_whether_the_cloud_answered(self):
        fake = self.use(FakeUpdater())
        with patch("terminal.sync._request", side_effect=self.cloud):
            self.assertEqual(sync.Worker(self.store).run_once(), [])
        self.assertEqual(fake.steps, [True])
        with self.store.transaction() as conn:
            self.store._set_meta(conn, "last_heartbeat_at", "0")
        with patch("terminal.sync._request", side_effect=TerminalError("Sem conexão", 503)):
            sync.Worker(self.store).run_once()
        self.assertEqual(fake.steps, [True, False])

    def test_updater_failure_never_stops_the_worker(self):
        fake = self.use(FakeUpdater())
        fake.auto_step = lambda store, online: (_ for _ in ()).throw(RuntimeError("disco cheio"))
        self.store.open_cash(self.cashier_id, 0)
        ids = [event["event_id"] for event in self.store.pending_events()]

        def cloud(method, url, **kwargs):
            return {"accepted_event_ids": ids} if url.endswith("/sync") else self.cloud(method, url, **kwargs)

        with patch("terminal.sync._request", side_effect=cloud):
            errors = sync.Worker(self.store).run_once()
        self.assertEqual(errors, ["Atualização: RuntimeError: disco cheio"])
        self.assertEqual(self.store.pending_events(), [])

    def test_status_route_and_session_expose_the_updater_state(self):
        self.use(FakeUpdater())
        self.assertEqual(self.client.get("/api/update/status").status_code, 401)
        self.login()
        status = self.client.get("/api/update/status").json
        self.assertEqual((status["supported"], status["state"], status["staged_version"], status["can_apply"]),
                         (True, "staged", "2.1.0", True))
        self.assertEqual(self.client.get("/api/session").json["update"]["staged_version"], "2.1.0")
        self.store.open_cash(self.cashier_id, 0)
        status = self.client.get("/api/update/status").json
        self.assertEqual((status["can_apply"], status["blocked_reason"]), (False, "Feche o caixa para atualizar"))

    def test_apply_needs_a_manager_and_a_closed_cashier(self):
        fake = self.use(FakeUpdater())
        self.login()
        self.assertEqual(self.post("/api/update/apply").status_code, 403)
        self.login(self.manager_id, "654321")
        self.store.open_cash(self.manager_id, 0)
        blocked = self.post("/api/update/apply")
        self.assertEqual((blocked.status_code, fake.applied), (409, 0))
        self.assertIn("Feche o caixa", blocked.json["error"])
        self.store.close_cash(self.manager_id, 0, "654321")
        fake.state.update(can_apply=False, blocked_reason="fila de impressão ocupada")
        blocked = self.post("/api/update/apply")
        self.assertEqual((blocked.status_code, blocked.json["error"], fake.applied),
                         (409, "fila de impressão ocupada", 0))
        fake.state.update(can_apply=True, blocked_reason="")
        applied = self.post("/api/update/apply")
        self.assertEqual((applied.status_code, applied.json["state"], fake.applied), (200, "applying", 1))
        self.assertIn("terminal.update_requested", [event["action"] for event in self.store.audit()])

    def test_broken_updater_is_reported_without_breaking_the_session(self):
        fake = self.use(FakeUpdater())
        self.login(self.manager_id, "654321")
        fake.status = lambda store: (_ for _ in ()).throw(RuntimeError("manifesto ilegível"))
        with self.assertLogs("terminal.app", level="ERROR"):
            session = self.client.get("/api/session")
        self.assertEqual((session.status_code, session.json["update"]["state"]), (200, "failed"))
        with self.assertLogs("terminal.app", level="ERROR"):
            self.assertEqual(self.post("/api/update/apply").status_code, 409)
        fake.status = FakeUpdater().status
        fake.request_apply = lambda store: (_ for _ in ()).throw(RuntimeError("sem espaço"))
        with self.assertLogs("terminal.app", level="ERROR"):
            failed = self.post("/api/update/apply")
        self.assertEqual(failed.status_code, 500)
        self.assertNotIn("sem espaço", failed.json["error"])

    def test_terminal_without_the_module_only_says_update_is_unavailable(self):
        self.use(None)
        self.store.set_release_result({"version": "2.1.0", "url": "https://updates.example.test/t.zip"})
        with patch("terminal.sync._request", side_effect=self.cloud):
            self.assertEqual(sync.Worker(self.store).run_once(), [])
        self.login(self.manager_id, "654321")
        status = self.client.get("/api/update/status").json
        self.assertEqual((status["supported"], status["can_apply"]), (False, False))
        self.assertIn("indisponível", status["blocked_reason"])
        self.assertEqual(status["available"]["version"], "2.1.0")
        self.assertEqual(self.post("/api/update/apply").status_code, 409)
        # The published version is not "available" when it is the one already running.
        self.store.set_release_result({"version": sync.VERSION, "url": "https://updates.example.test/t.zip"})
        self.assertIsNone(self.client.get("/api/update/status").json["available"])

    def test_import_of_the_updater_module_is_protected(self):
        self.addCleanup(importlib.reload, sync)
        with patch.dict(sys.modules, {"terminal.updater": None}):  # makes the import raise ImportError
            self.assertIsNone(importlib.reload(sync).updater)
        fake = types.ModuleType("terminal.updater")
        with patch.dict(sys.modules, {"terminal.updater": fake}):
            self.assertIs(importlib.reload(sync).updater, fake)

    def test_screen_has_the_update_card(self):
        html = self.client.get("/").get_data(as_text=True)
        script = (ROOT / "terminal" / "static" / "terminal.js").read_text(encoding="utf-8")
        for element_id in ("updateCard", "updateAvailable", "updateState", "updateApplyBtn", "updateBlocked"):
            self.assertIn(f'id="{element_id}"', html)
        self.assertRegex(html, r'id="updateApplyBtn"[^>]*disabled')
        for piece in ("/api/update/status", "/api/update/apply", "Feche o caixa para atualizar"):
            self.assertIn(piece, script)


class DemoScriptTests(unittest.TestCase):
    def setUp(self):
        LONG_LEASE.start()
        self.addCleanup(LONG_LEASE.stop)
        spec = importlib.util.spec_from_file_location("demo_terminal_fase01", ROOT / "scripts" / "demo_terminal.py")
        self.demo = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.demo)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.app = create_app({"TESTING": True, "DATA_DIR": self.temp.name, "START_WORKER": False})
        self.store = self.app.extensions["terminal_store"]

    def test_demo_can_show_a_lease_about_to_expire(self):
        self.demo.seed(self.store, expiring=True)
        status = self.store.lease_status()
        self.assertEqual((status["state"], status["warning"], status["days_left"]), ("active", True, 5))

    def test_demo_default_has_no_warning_and_can_show_an_update_ready(self):
        self.addCleanup(setattr, sync, "updater", sync.updater)
        self.demo.seed(self.store)
        self.assertFalse(self.store.lease_status()["warning"])
        self.demo.simulate_update(self.store)
        status = sync.updater.status(self.store)
        self.assertEqual((status["state"], status["staged_version"], status["can_apply"]), ("staged", "2.1.0", True))
        self.assertEqual(sync.updater.request_apply(self.store)["state"], "applying")


if __name__ == "__main__":
    unittest.main()
