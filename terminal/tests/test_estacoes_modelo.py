"""Fase 3 (estações): modelo local por caixa. Migração, caixas simultâneos, PIN por caixa,
vínculo, token, impressão por caixa, restauração e o que a promoção precisa do banco."""

import hashlib
import json
import shutil
import sqlite3
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

import shared.licensing as licensing
from shared.licensing import generate_keypair, payload_digest, sign_lease
from shared.security import hash_pin
from terminal import store as terminal_store
from terminal.app import create_app
from terminal.store import SERVER_STATION_ID, Store, TerminalError

DAY = 24 * 3600
LONG_LEASE = patch.object(licensing, "MAX_LEASE_SECONDS", 45 * DAY)
PIN_HASHES = {pin: hash_pin(pin) for pin in ("123987", "112233", "654321", "909090")}
MIRROR_KEY = {"key_id": "chave-1", "key": "k" * 43 + "="}

# Schema of the tables phase 3 changes, exactly as phase 2 created them.
PHASE2_SCHEMA = """
    CREATE TABLE cash_sessions(
        id TEXT PRIMARY KEY,operator_id TEXT NOT NULL,opening_cents INTEGER NOT NULL,
        opened_at INTEGER NOT NULL,closed_at INTEGER,counted_cents INTEGER,expected_cents INTEGER);
    CREATE UNIQUE INDEX cash_one_open ON cash_sessions((1)) WHERE closed_at IS NULL;
    CREATE TABLE sales(
        id TEXT PRIMARY KEY,request_id TEXT NOT NULL UNIQUE,session_id TEXT NOT NULL,
        operator_id TEXT NOT NULL,customer_name TEXT,customer_id TEXT,
        gross_cents INTEGER NOT NULL,discount_cents INTEGER NOT NULL,
        total_cents INTEGER NOT NULL,change_cents INTEGER NOT NULL,
        snapshot TEXT NOT NULL,status TEXT NOT NULL,created_at INTEGER NOT NULL,
        voided_at INTEGER,void_reason TEXT,customer_document TEXT);
    CREATE TABLE sale_returns(
        id TEXT PRIMARY KEY,sale_id TEXT NOT NULL,session_id TEXT,operator_id TEXT NOT NULL,
        refund_method TEXT NOT NULL,total_cents INTEGER NOT NULL,items TEXT NOT NULL,
        reason TEXT NOT NULL,created_at INTEGER NOT NULL);
    CREATE TABLE credit_receipts(
        id TEXT PRIMARY KEY,customer_id TEXT NOT NULL,session_id TEXT NOT NULL,method TEXT NOT NULL,
        amount_cents INTEGER NOT NULL,operator_id TEXT NOT NULL,created_at INTEGER NOT NULL);
    CREATE TABLE print_settings(
        kind TEXT PRIMARY KEY,driver TEXT NOT NULL,target TEXT NOT NULL,format TEXT NOT NULL);
    CREATE TABLE print_jobs(
        id TEXT PRIMARY KEY,kind TEXT NOT NULL,payload BLOB NOT NULL,
        driver TEXT NOT NULL,target TEXT NOT NULL,status TEXT NOT NULL,
        attempts INTEGER NOT NULL DEFAULT 0,error TEXT,created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL);
"""
STATION_TABLES = ("cash_sessions", "sales", "sale_returns", "credit_receipts", "print_jobs")


class StationCase(unittest.TestCase):
    """A paired main cashier whose license and cloud allow additional cashiers."""

    STOCK_MILLI = 50000
    CAPABILITIES = ["stations"]

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
        self.product_id, self.customer_id = str(uuid.uuid4()), str(uuid.uuid4())
        self.private, self.public = generate_keypair()
        self.operators = [
            {"id": self.cashier_id, "name": "Ana", "role": "cashier", "pin_hash": PIN_HASHES["123987"]},
            {"id": self.second_id, "name": "Caio", "role": "cashier", "pin_hash": PIN_HASHES["112233"]},
            {"id": self.manager_id, "name": "Bruno", "role": "manager", "pin_hash": PIN_HASHES["654321"]},
        ]
        self.products = [{"id": self.product_id, "sku": "ARROZ", "barcode": "7890000000002", "name": "Arroz",
                          "unit": "UN", "category": "Mercearia", "price_cents": 10000,
                          "quantity_milli": self.STOCK_MILLI, "active": True}]
        self.customers = [{"id": self.customer_id, "name": "Dona Rosa", "credit_limit_cents": 15000,
                           "open_credit_cents": 0, "store_credit_cents": 0}]
        self.lease = self.signed(max_stations=2)
        self.store.set_pairing({"token": "t" * 48, "tenant_id": self.tenant_id, "shop_id": self.shop_id,
                                "terminal_id": self.terminal_id, "lease": self.lease, "public_key": self.public,
                                "capabilities": self.CAPABILITIES}, "http://127.0.0.1:8000")
        self.bootstrap()
        self.cashier = {"id": self.cashier_id, "name": "Ana", "role": "cashier"}
        self.second = {"id": self.second_id, "name": "Caio", "role": "cashier"}
        self.manager = {"id": self.manager_id, "name": "Bruno", "role": "manager"}

    def signed(self, max_stations=None, device_id=None, issued=None, **extra):
        issued = int(time.time()) - 5 if issued is None else issued
        claims = {"schema": 1, "tenant_id": self.tenant_id, "shop_id": self.shop_id,
                  "terminal_id": self.terminal_id, "device_id": device_id or self.store.identity()["device_id"],
                  "issued_at": issued, "expires_at": issued + 3600, "features": ["pdv"],
                  "status": "active", "operators_hash": payload_digest(self.operators), **extra}
        if max_stations is not None:
            claims["max_stations"] = max_stations
        return sign_lease(claims, self.private)

    def bootstrap(self, **changes):
        self.store.apply_bootstrap({"products": self.products, "operators": self.operators,
                                    "customers": self.customers, "lease": self.lease, "public_key": self.public,
                                    **changes})

    def set_limit(self, limit, **extra):
        """A newer license from the cloud, with another number of additional cashiers."""
        self.lease = self.signed(max_stations=limit, issued=int(time.time()), **extra)
        with self.store.transaction() as conn:
            self.store._set_meta(conn, "lease", json.dumps(self.lease))

    def set_capabilities(self, capabilities):
        with self.store.transaction() as conn:
            self.store._set_meta(conn, "cloud_capabilities", json.dumps(capabilities))

    def link(self, name="", **kwargs):
        code = self.store.create_station_code(self.manager_id)["code"]
        return self.store.enroll_station(code, name=name, **kwargs)

    def sell(self, request_id, operator=None, payments=None, quantity=1000, **extra):
        return self.store.sale(operator or self.cashier, {
            "request_id": request_id, "items": [{"product_id": self.product_id, "quantity_milli": quantity}],
            "payments": payments or [{"method": "cash", "amount_cents": 10 * quantity}], **extra})

    def rows(self, query, params=()):
        with self.store.connection() as conn:
            return [dict(row) for row in conn.execute(query, params)]

    def events(self, kind=None):
        found = [json.loads(row["payload"]) for row in self.rows("SELECT payload FROM outbox ORDER BY seq")]
        return [event for event in found if kind is None or event["type"] == kind]

    def stock(self, store=None):
        with (store or self.store).connection() as conn:
            return conn.execute("SELECT stock_milli FROM products WHERE id=?", (self.product_id,)).fetchone()[0]

    def assertRaisesCode(self, status, code=None):
        outer = self

        class Check:
            def __enter__(self):
                return self

            def __exit__(self, kind, exc, _trace):
                outer.assertIsInstance(exc, TerminalError, "nenhum TerminalError foi levantado")
                outer.assertEqual((exc.status, exc.code), (status, code), str(exc))
                self.exception = exc
                return True
        return Check()


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "antigo.sqlite3"
        with sqlite3.connect(self.path) as conn:
            conn.executescript(PHASE2_SCHEMA)
            conn.execute("INSERT INTO cash_sessions VALUES ('c0','op',0,1,2,0,0)")
            conn.execute("INSERT INTO cash_sessions VALUES ('c1','op',500,10,NULL,NULL,NULL)")
            for index in range(3):
                conn.execute("INSERT INTO sales VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                             (f"s{index}", f"r{index}", "c1", "op", None, None, 1000, 0, 1000, 0, "{}",
                              "completed", 20 + index, None, None, None))
            conn.execute("INSERT INTO sale_returns VALUES ('d1','s0','c1','op','cash',1000,'[]','Avaria',30)")
            conn.execute("INSERT INTO credit_receipts VALUES ('f1','cli','c1','cash',700,'op',31)")
            conn.execute("INSERT INTO print_settings VALUES ('receipt','file','/tmp/recibos','escpos58')")
            conn.execute("INSERT INTO print_settings VALUES ('label','tcp','10.0.0.9:9100','zpl')")
            conn.execute("INSERT INTO print_jobs VALUES ('j1','receipt',x'00','file','x','queued',0,NULL,40,40)")
            conn.execute("INSERT INTO print_jobs VALUES ('j2','receipt',x'00','file','x','printed',1,NULL,41,41)")

    @staticmethod
    def dump(path):
        with sqlite3.connect(path) as conn:
            schema = conn.execute("SELECT type,name,sql FROM sqlite_master ORDER BY type,name").fetchall()
            data = {table: conn.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()
                    for table in (*STATION_TABLES, "print_settings", "stations")}
        return schema, data

    @staticmethod
    def columns(path, table):
        with sqlite3.connect(path) as conn:
            return [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]

    def test_phase2_database_becomes_the_server_station(self):
        store = Store(self.path)
        with store.connection() as conn:
            for table, count in zip(STATION_TABLES, (2, 3, 1, 1, 2)):
                self.assertEqual([tuple(row) for row in conn.execute(
                    f"SELECT station_id,COUNT(*) FROM {table} GROUP BY station_id")], [("server", count)], table)
            indexes = {row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
            self.assertNotIn("cash_one_open", indexes)
            self.assertLessEqual({"cash_one_open_station", "print_jobs_station"}, indexes)
            self.assertEqual([tuple(row) for row in conn.execute("SELECT * FROM print_settings ORDER BY kind")],
                             [("label", "tcp", "10.0.0.9:9100", "zpl", "server"),
                              ("receipt", "file", "/tmp/recibos", "escpos58", "server")])
            key = [row["name"] for row in sorted(conn.execute("PRAGMA table_info(print_settings)"),
                                                 key=lambda row: row["pk"]) if row["pk"]]
            self.assertEqual(key, ["station_id", "kind"])
            self.assertEqual([tuple(row) for row in conn.execute("SELECT id,name,token_hash FROM stations")],
                             [("server", "Caixa principal", None)])
        # The open cashier of the old file is the open cashier of the main computer, and it sells on.
        self.assertEqual(store.cash_state()["id"], "c1")
        self.assertEqual(store.printer_settings()[1], {"kind": "receipt", "driver": "file",
                                                       "target": "/tmp/recibos", "format": "escpos58"})

    def test_opening_again_changes_nothing(self):
        Store(self.path)
        first = self.dump(self.path)
        Store(self.path)
        Store(self.path)
        self.assertEqual(self.dump(self.path), first)

    def test_new_and_migrated_databases_have_the_same_columns(self):
        Store(self.path)
        fresh = Path(self.temp.name) / "novo.sqlite3"
        Store(fresh)
        for table in (*STATION_TABLES, "print_settings"):
            self.assertEqual(self.columns(self.path, table), self.columns(fresh, table), table)
            self.assertEqual(self.columns(fresh, table)[-1], "station_id", table)

    def test_named_inserts_of_older_code_keep_working(self):
        store = Store(self.path)
        with store.transaction() as conn:
            conn.execute("UPDATE cash_sessions SET closed_at=50 WHERE id='c1'")
            conn.execute("INSERT INTO cash_sessions(id,operator_id,opening_cents,opened_at) VALUES ('c9','op',0,1)")
            conn.execute("""INSERT INTO print_jobs(id,kind,payload,driver,target,status,attempts,created_at,updated_at)
                            VALUES ('j9','receipt',x'00','file','x','queued',0,1,1)""")
            conn.execute("INSERT INTO print_settings(kind,driver,target,format) VALUES ('receipt','file','y','escpos80') "
                         "ON CONFLICT(station_id,kind) DO UPDATE SET target=excluded.target")
        self.assertEqual(store.cash_state()["id"], "c9")
        self.assertEqual(store.begin_print()["id"], "j9")  # the oldest queued job of the main cashier

    def test_two_open_cashiers_survive_a_restart(self):
        store = Store(self.path)
        with store.transaction() as conn:
            conn.execute("INSERT INTO cash_sessions(id,operator_id,opening_cents,opened_at,station_id) "
                         "VALUES ('c2','op',0,60,'outro')")
        again = Store(self.path)  # the global index must not come back
        self.assertTrue(again.any_cash_open())
        with again.station("outro"):
            self.assertEqual(again.cash_state()["id"], "c2")


class ContextTests(StationCase):
    def test_station_context_is_per_thread_and_restored(self):
        self.assertEqual(self.store.current_station(), SERVER_STATION_ID)
        seen = {}
        with self.store.station("abc"):
            self.assertEqual(self.store.current_station(), "abc")
            with self.store.station("def"):
                self.assertEqual(self.store.current_station(), "def")
            self.assertEqual(self.store.current_station(), "abc")
            thread = threading.Thread(target=lambda: seen.update(other=self.store.current_station()))
            thread.start()
            thread.join()
        self.assertEqual(seen, {"other": SERVER_STATION_ID})
        self.assertEqual(self.store.current_station(), SERVER_STATION_ID)
        self.store.set_station("abc")
        self.store.set_station(None)
        self.assertEqual(self.store.current_station(), SERVER_STATION_ID)

    def test_terminal_error_carries_an_optional_code(self):
        self.assertIsNone(TerminalError("x").code)
        error = TerminalError("y", 409, code="station_limit")
        self.assertEqual((str(error), error.status, error.code), ("y", 409, "station_limit"))


class CashPerStationTests(StationCase):
    def test_two_cashiers_open_at_the_same_time(self):
        station = self.link()["station_id"]
        server_cash = self.store.open_cash(self.cashier_id, 1000)
        with self.store.station(station):
            station_cash = self.store.open_cash(self.second_id, 2000)
            with self.assertRaisesCode(409):
                self.store.open_cash(self.second_id, 0)
            self.sell("venda-estacao", self.second)
            summary = self.store.cash_summary()
            self.assertEqual((summary["id"], summary["sales_count"], summary["expected_cents"]),
                             (station_cash["id"], 1, 12000))
        with self.assertRaisesCode(409):
            self.store.open_cash(self.cashier_id, 0)
        self.sell("venda-1")
        self.sell("venda-2")
        summary = self.store.cash_summary()
        self.assertEqual((summary["id"], summary["sales_count"], summary["expected_cents"]),
                         (server_cash["id"], 2, 21000))
        self.assertEqual([row["station_id"] for row in self.store.open_cash_sessions()], [SERVER_STATION_ID, station])
        self.assertEqual(self.store.open_cash_sessions()[1]["station_name"], "Caixa 2")
        # Closing one never closes the other.
        with self.store.station(station):
            closed = self.store.close_cash(self.second_id, 12000, "654321")
            self.assertEqual(closed["difference_cents"], 0)
            self.assertIsNone(self.store.cash_state())
            with self.assertRaisesCode(409):
                self.store.cash_summary()
        self.assertEqual(self.store.cash_state()["id"], server_cash["id"])
        self.assertTrue(self.store.any_cash_open())
        self.store.close_cash(self.cashier_id, 21000, "654321")
        self.assertFalse(self.store.any_cash_open())
        listed = {row["request_id"]: row for row in self.store.sales()}
        self.assertEqual((listed["venda-estacao"]["station_id"], listed["venda-estacao"]["station_name"]),
                         (station, "Caixa 2"))
        self.assertEqual(listed["venda-1"]["station_name"], "Caixa principal")

    def test_only_the_main_cashier_closes_the_cashier_of_another_computer(self):
        first, second = self.link()["station_id"], self.link()["station_id"]
        with self.store.station(first):
            self.store.open_cash(self.cashier_id, 0)
        with self.store.station(second):
            with self.assertRaisesCode(403, "server_only"):
                self.store.close_cash(self.second_id, 0, "654321", station_id=first)
        with self.assertRaisesCode(403):
            self.store.close_cash(self.manager_id, 0, "000000", station_id=first)
        summary = self.store.close_cash(self.manager_id, 0, "654321", station_id=first)
        self.assertEqual(summary["counted_cents"], 0)
        closing = self.events("cash.closed")[0]
        self.assertEqual(closing["station"], {"id": first, "name": "Caixa 2"})
        with self.assertRaisesCode(409):
            self.store.close_cash(self.manager_id, 0, "654321", station_id=first)

    def test_expired_license_keeps_only_the_open_cashiers_selling(self):
        station = self.link()["station_id"]
        with self.store.station(station):
            self.store.open_cash(self.second_id, 0)
        expired = self.signed(max_stations=2, issued=int(time.time()) - 7200)
        with self.store.transaction() as conn:
            self.store._set_meta(conn, "lease", json.dumps(expired))
        self.assertFalse(self.store.lease_status()["can_sell"])  # the main cashier has none open
        with self.store.station(station):
            self.assertTrue(self.store.lease_status()["can_sell"])

    def test_license_status_reports_the_additional_cashiers(self):
        self.assertEqual(self.store.lease_status()["max_stations"], 2)
        self.set_limit(None)
        self.assertEqual(self.store.lease_status()["max_stations"], 0)

    def test_audit_names_the_additional_cashier(self):
        station = self.link("Balcão")["station_id"]
        self.store.open_cash(self.cashier_id, 0)
        with self.store.station(station):
            self.store.open_cash(self.second_id, 0)
            self.store.cash_movement(self.second_id, "supplied", 500, "Troco")
        details = {(row["action"], row["detail"]) for row in self.rows("SELECT action,detail FROM audit")}
        self.assertIn(("cash.opened", None), details)
        self.assertIn(("cash.opened", "caixa=Balcão"), details)
        self.assertIn(("cash.supplied", "Troco; caixa=Balcão"), details)


class ConcurrencyTests(StationCase):
    STOCK_MILLI = 120000

    def test_three_cashiers_sell_the_same_limited_stock(self):
        stations = [SERVER_STATION_ID, self.link()["station_id"], self.link()["station_id"]]
        operators = [self.cashier, self.second, self.manager]
        for station, operator in zip(stations, operators):
            with self.store.station(station):
                self.store.open_cash(operator["id"], 1000)
        sold, refused, crashed = {station: 0 for station in stations}, [], []
        start = threading.Barrier(3)

        def work(station, operator):
            try:
                with self.store.station(station):
                    start.wait(timeout=30)
                    for index in range(50):
                        try:
                            self.sell(f"{station}-{index}", operator)
                            sold[station] += 1
                        except TerminalError as exc:
                            refused.append((exc.status, str(exc)))
            except Exception as exc:  # pragma: no cover - reported below
                crashed.append(repr(exc))

        threads = [threading.Thread(target=work, args=pair) for pair in zip(stations, operators)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=120)
        self.assertEqual(crashed, [])
        # 150 attempts for 120 units: the stock never goes below zero.
        self.assertEqual(sum(sold.values()), 120)
        self.assertEqual(len(refused), 30)
        self.assertTrue(all(status == 409 and "Estoque insuficiente" in text for status, text in refused))
        self.assertEqual(self.stock(), 0)
        completed = self.events("sale.completed")
        self.assertEqual(sum(event["data"]["items"][0]["quantity_milli"] for event in completed), self.STOCK_MILLI)
        sequences = [row["seq"] for row in self.rows("SELECT seq FROM outbox ORDER BY rowid")]
        self.assertEqual(sequences, sorted(set(sequences)))
        self.assertEqual(sequences, list(range(1, len(sequences) + 1)))
        by_station = {station: 0 for station in stations}
        for event in completed:
            self.assertEqual(event["station"]["id"], event["data"]["request_id"].rsplit("-", 1)[0])
            by_station[event["station"]["id"]] += 1
        self.assertEqual(by_station, sold)
        for station in stations:
            with self.store.station(station):
                summary = self.store.cash_summary()
                self.assertEqual((summary["sales_count"], summary["expected_cents"]),
                                 (sold[station], 1000 + sold[station] * 10000))
        self.assertEqual({row["station_id"]: row["total"] for row in self.rows(
            "SELECT station_id,COUNT(*) total FROM sales GROUP BY station_id")}, sold)

    def test_fiado_limit_holds_across_two_cashiers(self):
        stations = [self.link()["station_id"], self.link()["station_id"]]
        for station in stations:
            with self.store.station(station):
                self.store.open_cash(self.cashier_id, 0)
        results, start = [], threading.Barrier(2)

        def work(station):
            with self.store.station(station):
                start.wait(timeout=30)
                try:
                    self.sell("fiado-" + station, customer_id=self.customer_id,
                              payments=[{"method": "credit_account", "amount_cents": 10000}])
                    results.append("ok")
                except TerminalError as exc:
                    results.append((exc.status, str(exc)))

        threads = [threading.Thread(target=work, args=(station,)) for station in stations]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
        # The limit is R$ 150,00 and each sale takes R$ 100,00: only one may pass.
        self.assertEqual(results.count("ok"), 1, results)
        refused = next(item for item in results if item != "ok")
        self.assertEqual(refused[0], 403)
        self.assertIn("Limite de fiado excedido", refused[1])
        self.assertEqual(self.store.customers("Rosa")[0]["open_credit_cents"], 10000)


class EventEnvelopeTests(StationCase):
    def test_without_the_capability_the_envelope_has_no_station(self):
        station = self.link()["station_id"]
        self.set_capabilities(["restore", "terminal-config"])
        with self.store.station(station):
            self.store.open_cash(self.cashier_id, 0)
            self.sell("sem-etiqueta")
        for event in self.events():
            self.assertEqual(set(event), {"event_id", "type", "aggregate_id", "identity", "data"})

    def test_with_the_capability_the_station_goes_beside_the_data(self):
        station = self.link("Caixa da frente")["station_id"]
        self.store.open_cash(self.cashier_id, 0)
        with self.store.station(station):
            self.store.open_cash(self.second_id, 0)
            sale = self.sell("com-etiqueta", self.second)
        opened = self.events("cash.opened")
        self.assertEqual([event["station"] for event in opened],
                         [{"id": "server", "name": "Caixa principal"}, {"id": station, "name": "Caixa da frente"}])
        event = self.events("sale.completed")[0]
        self.assertEqual(set(event), {"event_id", "type", "aggregate_id", "identity", "data", "station"})
        self.assertEqual(event["station"], {"id": station, "name": "Caixa da frente"})
        self.assertRegex(event["event_id"], r"^[0-9a-f]{32}$")
        self.assertEqual(event["aggregate_id"], sale["id"])
        self.assertEqual(event["identity"], {"tenant_id": self.tenant_id, "shop_id": self.shop_id,
                                             "terminal_id": self.terminal_id})
        # The data the cloud hashes is exactly what it was before phase 3.
        self.assertEqual(set(event["data"]), {"request_id", "cash_session_id", "created_at", "gross_cents",
                                              "promo_discount_cents", "discount_cents", "total_cents", "change_cents",
                                              "customer_id", "customer_name", "customer_document", "operator_id",
                                              "seller_id", "items", "payments"})
        self.assertEqual((sale["station_id"], sale["station_name"]), (station, "Caixa da frente"))
        self.assertEqual(self.store.sale_detail(sale["id"])["station_name"], "Caixa da frente")


class IdempotencyTests(StationCase):
    def run_as(self, station, func):
        """Same call from another thread, as another computer of the shop would make it."""
        result = {}

        def work():
            try:
                with self.store.station(station):
                    result["value"] = func()
            except Exception as exc:
                result["error"] = exc
        thread = threading.Thread(target=work)
        thread.start()
        thread.join(timeout=60)
        if "error" in result:
            raise result["error"]
        return result["value"]

    def test_repeated_requests_from_any_cashier_never_duplicate(self):
        first, second = self.link()["station_id"], self.link()["station_id"]
        with self.store.station(first):
            self.store.open_cash(self.cashier_id, 0)
        self.bootstrap(customers=[dict(self.customers[0], open_credit_cents=5000)])
        sale = self.run_as(first, lambda: self.sell("cabo-puxado"))
        # The cable was pulled before the answer arrived: the screen repeats with the same request_id.
        again = self.run_as(first, lambda: self.sell("cabo-puxado"))
        elsewhere = self.run_as(second, lambda: self.sell("cabo-puxado"))  # no open cashier there
        on_server = self.sell("cabo-puxado")
        self.assertEqual({item["id"] for item in (again, elsewhere, on_server)}, {sale["id"]})
        self.assertEqual(again["receipt"], sale["receipt"])
        self.assertEqual(len(self.events("sale.completed")), 1)
        self.assertEqual(self.stock(), self.STOCK_MILLI - 1000)

        request = {"return_id": "devolucao-1", "refund_method": "cash", "reason": "Avaria",
                   "items": [{"product_id": self.product_id, "quantity_milli": 1000}], "manager_pin": "654321"}
        returned = self.run_as(first, lambda: self.store.return_sale(self.cashier, sale["id"], request))
        repeated = self.run_as(second, lambda: self.store.return_sale(self.cashier, sale["id"], request))
        self.assertEqual((repeated["id"], repeated["total_cents"]), (returned["id"], returned["total_cents"]))
        self.assertEqual(len(self.events("sale.returned")), 1)
        self.assertEqual(self.stock(), self.STOCK_MILLI)

        receipt = {"receipt_id": "recebimento-1", "customer_id": self.customer_id, "method": "cash",
                   "amount_cents": 2000}
        self.run_as(first, lambda: self.store.receive_credit(self.cashier, receipt))
        replay = self.run_as(second, lambda: self.store.receive_credit(self.cashier, receipt))
        self.assertTrue(replay["replayed"])
        self.assertEqual(len(self.events("credit.received")), 1)
        self.assertEqual(self.store.customers("Rosa")[0]["open_credit_cents"], 3000)
        self.assertEqual(self.rows("SELECT station_id FROM sale_returns UNION ALL SELECT station_id FROM credit_receipts"),
                         [{"station_id": first}, {"station_id": first}])


class PinPerStationTests(StationCase):
    def wrong_logins(self, operator_id, times=5):
        for _ in range(times):
            with self.assertRaisesCode(401):
                self.store.login(operator_id, "000000")

    def test_wrong_pins_lock_one_operator_on_one_cashier(self):
        first, second = self.link()["station_id"], self.link()["station_id"]
        with self.store.station(first):
            self.wrong_logins(self.cashier_id)
            with self.assertRaisesCode(429):
                self.store.login(self.cashier_id, "123987")
            self.assertEqual(self.store.login(self.second_id, "112233")["name"], "Caio")  # another operator
        self.assertEqual(self.store.login(self.cashier_id, "123987")["name"], "Ana")
        with self.store.station(second):
            self.assertEqual(self.store.login(self.cashier_id, "123987")["name"], "Ana")
        self.assertEqual([row["operator_id"] for row in self.rows("SELECT operator_id FROM login_attempts")],
                         [f"{self.cashier_id}|{first}"])
        # The bootstrap (every 30 seconds) neither gives the attempts back nor removes the lock.
        self.bootstrap()
        with self.store.station(first):
            with self.assertRaisesCode(429):
                self.store.login(self.cashier_id, "123987")

    def test_a_changed_pin_unlocks_every_cashier(self):
        first = self.link()["station_id"]
        with self.store.station(first):
            self.wrong_logins(self.cashier_id)
        self.wrong_logins(self.cashier_id)
        self.operators[0] = dict(self.operators[0], pin_hash=PIN_HASHES["909090"])
        self.lease = self.signed(max_stations=2, issued=int(time.time()))
        self.bootstrap()
        self.assertEqual(self.rows("SELECT * FROM login_attempts"), [])
        with self.store.station(first):
            self.assertEqual(self.store.login(self.cashier_id, "909090")["name"], "Ana")
        self.assertEqual(self.store.login(self.cashier_id, "909090")["name"], "Ana")

    def test_attempts_of_a_removed_operator_are_forgotten_on_every_cashier(self):
        first = self.link()["station_id"]
        with self.store.station(first):
            self.wrong_logins(self.second_id, times=2)
            self.wrong_logins(self.cashier_id, times=1)
        self.operators = [self.operators[0], self.operators[2]]
        self.lease = self.signed(max_stations=2, issued=int(time.time()))
        self.bootstrap()
        self.assertEqual([row["operator_id"] for row in self.rows("SELECT operator_id FROM login_attempts")],
                         [f"{self.cashier_id}|{first}"])

    def test_manager_authorization_is_counted_per_cashier(self):
        first, second = self.link()["station_id"], self.link()["station_id"]
        with self.store.station(first):
            for _ in range(5):
                with self.assertRaisesCode(403):
                    self.store.manager_override("000000", self.cashier_id)
            with self.assertRaisesCode(429):
                self.store.manager_override("654321", self.cashier_id)
        self.assertEqual(self.store.manager_override("654321", self.cashier_id), self.manager_id)
        with self.store.station(second):
            self.assertEqual(self.store.manager_override("654321", self.cashier_id), self.manager_id)
        keys = {row["operator_id"]: row["failures"] for row in self.rows("SELECT * FROM login_attempts")}
        self.assertEqual(keys, {f"manager_override:{self.cashier_id}|{first}": 5, f"manager_override|{first}": 5})
        self.assertIn(f"manager_override_window|{first}", self.store.meta())
        self.assertNotIn("manager_override_window", self.store.meta())

    def test_the_ceiling_of_one_cashier_never_locks_the_main_one(self):
        first = self.link()["station_id"]
        with self.store.transaction() as conn:
            self.store._set_meta(conn, f"manager_override_window|{first}", str(int(time.time())))
            conn.execute("INSERT INTO login_attempts VALUES (?,?,0)",
                         (f"manager_override|{first}", terminal_store.MANAGER_OVERRIDE_GLOBAL_FAILURES - 1))
        with self.store.station(first):
            with self.assertRaisesCode(403):
                self.store.manager_override("000000", self.cashier_id)
            with self.assertRaisesCode(429):  # any requester, on this cashier only
                self.store.manager_override("654321", self.second_id)
        self.assertEqual(self.store.manager_override("654321", self.second_id), self.manager_id)
        # A bootstrap keeps the lockouts of the manager PIN, also the per-cashier ones.
        self.bootstrap()
        self.assertIn(f"manager_override|{first}",
                      [row["operator_id"] for row in self.rows("SELECT operator_id FROM login_attempts")])


class EnrollTests(StationCase):
    def code(self, name=""):
        return self.store.create_station_code(self.manager_id, name)

    @staticmethod
    def other(code):
        return f"{(int(code) + 1) % 10 ** 6:06d}"

    def test_code_shape_and_single_use(self):
        created = self.code()
        self.assertRegex(created["code"], r"^\d{6}$")
        self.assertEqual(created["expires_in"], 300)
        self.assertAlmostEqual(created["expires_at"], time.time() * 1000 + 300000, delta=5000)
        spaced = created["code"][:3] + " " + created["code"][3:]
        linked = self.store.enroll_station(spaced, hostname="PC-CAIXA2", app_version="2.1.0", address="192.168.0.21")
        self.assertEqual(linked["station_name"], "Caixa 2")
        self.assertRegex(linked["station_id"], r"^[0-9a-f]{32}$")
        self.assertGreaterEqual(len(linked["token"]), 40)
        with self.assertRaisesCode(401, "invalid_code"):
            self.store.enroll_station(created["code"])
        self.assertEqual(self.link()["station_name"], "Caixa 3")
        self.assertEqual(self.store.station_limit(), {"limit": 2, "used": 2, "available": 0})
        row = self.store.stations()[1]
        self.assertEqual((row["hostname"], row["app_version"], row["last_address"], row["linked"], row["online"],
                          row["cash_open"], row["is_server"]),
                         ("PC-CAIXA2", "2.1.0", "192.168.0.21", True, True, False, False))

    def test_names_come_from_the_request_then_from_the_code(self):
        self.assertEqual(self.store.enroll_station(self.code("Balcão")["code"])["station_name"], "Balcão")
        self.assertEqual(self.store.enroll_station(self.code("Balcão")["code"], name="Fundos")["station_name"], "Fundos")

    def test_malformed_and_expired_codes(self):
        with self.assertRaisesCode(400):
            self.store.enroll_station("12345")
        with self.assertRaisesCode(400):
            self.store.enroll_station(None)
        with self.assertRaisesCode(401, "invalid_code"):
            self.store.enroll_station("123456")  # nobody asked for a code
        created = self.code()
        later = terminal_store._now_ms() + 301 * 1000
        with patch.object(terminal_store, "_now_ms", return_value=later):
            with self.assertRaisesCode(401, "invalid_code"):
                self.store.enroll_station(created["code"])
        self.assertEqual(self.store.station_limit()["used"], 0)

    def test_only_one_code_is_valid_at_a_time(self):
        first = self.code()["code"]
        second = self.code()["code"]
        if first != second:
            with self.assertRaisesCode(401, "invalid_code"):
                self.store.enroll_station(first)
            self.assertEqual(self.rows("SELECT failures FROM station_enroll_codes"), [{"failures": 1}])
        self.assertTrue(self.store.enroll_station(second)["token"])

    def test_five_wrong_codes_invalidate_the_code(self):
        code = self.code()["code"]
        for _ in range(5):
            with self.assertRaisesCode(401, "invalid_code"):
                self.store.enroll_station(self.other(code))
        self.assertEqual(self.rows("SELECT * FROM station_enroll_codes"), [])
        with self.assertRaisesCode(401, "invalid_code"):
            self.store.enroll_station(code)
        actions = [row["action"] for row in self.rows("SELECT action FROM audit")]
        self.assertEqual(actions.count("station.enroll_locked"), 1)

    def test_a_plan_without_additional_cashiers_refuses(self):
        code = self.code()["code"]
        for change in (lambda: self.set_limit(0), lambda: self.set_limit(None),
                       lambda: self.set_limit(2, hub=False), lambda: self.set_capabilities(["restore"])):
            self.set_limit(2)
            self.set_capabilities(["stations"])
            change()
            self.assertEqual(self.store.station_limit()["limit"], 0)
            with self.assertRaisesCode(409, "station_limit") as refused:
                self.code()
            self.assertIn("não inclui caixas adicionais", str(refused.exception))
            with self.assertRaisesCode(409, "station_limit"):
                self.store.enroll_station(code)
        # The code was never spent: it works once the plan allows.
        self.set_limit(2)
        self.set_capabilities(["stations"])
        self.assertTrue(self.store.enroll_station(code)["token"])

    def test_a_full_plan_refuses_without_spending_the_code(self):
        self.link()
        code = self.code()["code"]
        self.set_limit(1)
        with self.assertRaisesCode(409, "station_limit") as refused:
            self.store.enroll_station(code)
        self.assertIn("inclui 1 caixa(s) adicional(is) e todos já estão em uso", str(refused.exception))
        with self.assertRaisesCode(409, "station_limit"):
            self.code()
        self.assertEqual(self.store.station_limit(), {"limit": 1, "used": 1, "available": 0})
        self.set_limit(2)
        self.assertEqual(self.store.enroll_station(code)["station_name"], "Caixa 3")

    def test_a_known_station_links_again_and_keeps_its_open_cashier(self):
        first = self.link("Balcão")
        with self.store.station(first["station_id"]):
            cash = self.store.open_cash(self.cashier_id, 700)
        again = self.store.enroll_station(self.code()["code"], station_id=first["station_id"])
        self.assertEqual((again["station_id"], again["station_name"]), (first["station_id"], "Balcão"))
        self.assertNotEqual(again["token"], first["token"])
        with self.assertRaisesCode(401, "station_unauthorized"):
            self.store.authenticate_station(first["token"])
        self.assertEqual(self.store.authenticate_station(again["token"]), {"id": first["station_id"], "name": "Balcão"})
        with self.store.station(first["station_id"]):
            self.assertEqual(self.store.cash_state()["id"], cash["id"])
        self.assertEqual(self.store.station_limit()["used"], 1)
        # A removed computer comes back with the same identity too.
        self.store.revoke_station(first["station_id"], self.manager_id)
        back = self.store.enroll_station(self.code()["code"], station_id=first["station_id"])
        self.assertEqual(self.store.authenticate_station(back["token"])["id"], first["station_id"])
        # An identity this shop never saw is just a new computer.
        self.assertNotEqual(self.store.enroll_station(self.code()["code"], station_id="desconhecida")["station_id"],
                            "desconhecida")

    def test_a_full_plan_still_gives_a_code_to_relink_a_known_station(self):
        first, second = self.link(), self.link()
        with self.assertRaisesCode(409, "station_limit"):
            self.code()
        with self.store.transaction() as conn:  # what a promotion or a recovery leaves behind
            conn.execute("UPDATE stations SET token_hash=NULL WHERE id=?", (second["station_id"],))
        code = self.code()["code"]
        with self.assertRaisesCode(409, "station_limit"):
            self.store.enroll_station(code)  # a new computer still does not fit
        self.assertTrue(self.store.enroll_station(code, station_id=second["station_id"])["token"])
        self.assertTrue(first["token"])

    def test_code_needs_a_valid_license(self):
        expired = self.signed(max_stations=2, issued=int(time.time()) - 7200)
        with self.store.transaction() as conn:
            self.store._set_meta(conn, "lease", json.dumps(expired))
        with self.assertRaisesCode(403):
            self.code()


class TokenTests(StationCase):
    def test_unknown_revoked_and_over_the_limit(self):
        for bad in (None, 12, "", "curto", "x" * 200, "y" * 43):
            with self.assertRaisesCode(401, "station_unauthorized"):
                self.store.authenticate_station(bad)
        first = self.link()
        time.sleep(0.005)
        second = self.link()
        self.assertEqual(self.store.authenticate_station(first["token"]), {"id": first["station_id"], "name": "Caixa 2"})
        self.assertEqual(self.store.authenticate_station(second["token"])["name"], "Caixa 3")
        self.set_limit(1)  # the plan shrank: the oldest link keeps working
        self.assertEqual(self.store.authenticate_station(first["token"])["id"], first["station_id"])
        with self.assertRaisesCode(403, "station_limit"):
            self.store.authenticate_station(second["token"])
        self.set_limit(2)
        revoked = self.store.revoke_station(first["station_id"], self.manager_id)
        self.assertTrue(revoked["revoked_at"])
        self.assertFalse(revoked["linked"])
        with self.assertRaisesCode(401, "station_revoked") as refused:
            self.store.authenticate_station(first["token"])
        self.assertIn("removido", str(refused.exception))
        self.assertEqual(self.store.authenticate_station(second["token"])["id"], second["station_id"])
        self.assertEqual(self.store.station_limit(), {"limit": 2, "used": 1, "available": 1})
        self.assertEqual([row["id"] for row in self.store.stations(include_revoked=False)],
                         ["server", second["station_id"]])

    def test_the_main_cashier_cannot_be_removed(self):
        with self.assertRaisesCode(409):
            self.store.revoke_station("server")
        with self.assertRaisesCode(404):
            self.store.revoke_station("nao-existe")
        with self.assertRaisesCode(404):
            self.store.rename_station("nao-existe", "Outro")

    def test_rename_and_last_contact(self):
        linked = self.link()
        self.assertEqual(self.store.rename_station(linked["station_id"], "  Caixa do fundo  ", self.manager_id)["name"],
                         "Caixa do fundo")
        with self.assertRaises(TerminalError):
            self.store.rename_station(linked["station_id"], "x" * 41)
        with self.store.transaction() as conn:
            conn.execute("UPDATE stations SET last_seen_at=? WHERE id=?",
                         (terminal_store._now_ms() - 91000, linked["station_id"]))
        self.assertFalse(self.store.stations()[1]["online"])
        self.store.touch_station(linked["station_id"], address="192.168.0.30", app_version="2.1.1")
        row = self.store.stations()[1]
        self.assertEqual((row["online"], row["last_address"], row["app_version"], row["hostname"]),
                         (True, "192.168.0.30", "2.1.1", ""))
        self.store.touch_station(linked["station_id"])
        self.assertEqual(self.store.stations()[1]["last_address"], "192.168.0.30")

    def test_the_token_never_leaves_the_database(self):
        code = self.store.create_station_code(self.manager_id)["code"]
        linked = self.store.enroll_station(code)
        with self.store.station(linked["station_id"]):
            self.store.open_cash(self.cashier_id, 0)
        self.store.rename_station(linked["station_id"], "Balcão", self.manager_id)
        self.store.revoke_station(linked["station_id"], self.manager_id)
        digest = hashlib.sha256(linked["token"].encode()).hexdigest()
        visible = json.dumps([self.store.export_data(), self.store.stations(), self.store.audit(),
                              self.rows("SELECT * FROM audit"), self.events(), self.store.open_cash_sessions()],
                             default=str)
        for secret in (linked["token"], digest, code):
            self.assertNotIn(secret, visible)
        self.assertNotIn("token_hash", visible)
        self.assertEqual([row["name"] for row in self.store.export_data()["stations"]], ["Caixa principal", "Balcão"])

    def test_changing_company_forgets_the_additional_cashiers(self):
        linked = self.link()
        self.store.create_station_code(self.manager_id)
        with self.store.station(linked["station_id"]):
            self.store.save_printer("receipt", "file", str(Path(self.temp.name) / "print"), "escpos58")
        with self.store.transaction() as conn:
            self.store._clear_company_cache(conn)
        self.assertEqual([row["id"] for row in self.store.stations()], ["server"])
        self.assertEqual(self.rows("SELECT * FROM station_enroll_codes"), [])
        self.assertEqual(self.rows("SELECT * FROM print_settings"), [])


class PrintPerStationTests(StationCase):
    def test_each_cashier_prints_on_its_own_printer(self):
        station = self.link()["station_id"]
        target = str(Path(self.temp.name) / "impressora-estacao")
        self.store.open_cash(self.cashier_id, 0)
        with self.store.station(station):
            self.store.save_printer("receipt", "file", target, "escpos58")
            self.assertEqual(self.store.printer_settings(), [{"kind": "receipt", "driver": "file", "target": target,
                                                              "format": "escpos58"}])
            self.store.open_cash(self.second_id, 0)
            sale = self.sell("com-recibo", self.second)
        self.assertEqual(self.store.printer_settings(), [])
        self.sell("sem-impressora")  # the main cashier has no printer: no job
        jobs = self.rows("SELECT station_id,target,status FROM print_jobs")
        self.assertEqual(jobs, [{"station_id": station, "target": target, "status": "queued"}])
        self.assertEqual(self.store.print_jobs(), [])
        self.assertIsNone(self.store.begin_print())  # the worker of the main computer never takes it
        with self.assertRaisesCode(409):
            self.store.reprint(sale["id"])
        with self.store.station(station):
            self.assertEqual(len(self.store.print_jobs()), 1)
            job = self.store.begin_print()
            self.assertEqual((job["station_id"], job["target"]), (station, target))
        self.assertEqual(self.store.recover_print(), 0)
        self.store.finish_print(job["id"])  # wrong cashier: nothing changes
        self.assertEqual(self.rows("SELECT status FROM print_jobs"), [{"status": "sending"}])
        with self.assertRaisesCode(404):
            self.store.retry_print(self.cashier_id, job["id"])
        with self.store.station(station):
            self.assertEqual(self.store.recover_print(), 1)
            self.assertEqual(self.store.print_jobs()[0]["status"], "uncertain")
            self.store.retry_print(self.second_id, job["id"])
            again = self.store.begin_print()
            self.store.finish_print(again["id"])
            self.assertEqual(self.store.print_jobs()[0]["status"], "printed")
            self.assertEqual(self.store.reprint(sale["id"])["status"], "queued")

    def test_start_only_recovers_the_queue_of_the_main_computer(self):
        station = self.link()["station_id"]
        with self.store.transaction() as conn:
            for job_id, owner in (("j-server", "server"), ("j-estacao", station)):
                conn.execute("""INSERT INTO print_jobs(id,kind,payload,driver,target,status,attempts,created_at,
                                updated_at,station_id) VALUES (?,'receipt',x'00','file','x','sending',1,1,1,?)""",
                             (job_id, owner))
        Store(self.path)
        self.assertEqual({row["id"]: row["status"] for row in self.rows("SELECT id,status FROM print_jobs")},
                         {"j-server": "uncertain", "j-estacao": "sending"})

    def test_config_event_carries_the_main_printers_and_the_station_list(self):
        self.set_capabilities(["stations", "terminal-config"])
        target = str(Path(self.temp.name) / "print")
        linked = self.link("Balcão")
        with self.store.station(linked["station_id"]):
            self.store.save_printer("receipt", "file", target + "-estacao", "escpos58")
        self.store.save_printer("receipt", "file", target, "escpos80")
        self.store.revoke_station(linked["station_id"], self.manager_id)
        configs = [event["data"] for event in self.events("terminal.config")]
        self.assertEqual(configs[0]["stations"], [{"id": linked["station_id"], "name": "Balcão", "revoked": False}])
        self.assertEqual(configs[0]["print_settings"], [])
        self.assertEqual(configs[-1]["print_settings"], [{"kind": "receipt", "driver": "file", "target": target,
                                                          "format": "escpos80"}])
        self.assertEqual(configs[-1]["stations"], [{"id": linked["station_id"], "name": "Balcão", "revoked": True}])
        self.assertIsNone(self.store.ensure_config_event())
        # A cloud without the capability never receives the list.
        self.set_capabilities(["terminal-config"])
        self.store.rename_station(linked["station_id"], "Outro nome")
        self.assertNotIn("stations", self.events("terminal.config")[-1]["data"])


class RestoreWithStationsTests(StationCase):
    def test_two_open_cashiers_of_different_stations_come_back_open(self):
        new = Store(Path(self.temp.name) / "novo" / "terminal.sqlite3")
        lease = self.signed(max_stations=2, device_id=new.identity()["device_id"])
        new.set_pairing({"token": "n" * 48, "tenant_id": self.tenant_id, "shop_id": self.shop_id,
                         "terminal_id": self.terminal_id, "lease": lease, "public_key": self.public,
                         "capabilities": ["stations", "restore"], "recovery": True}, "http://127.0.0.1:8000")
        session = {"kind": "cash_session", "operator_id": self.cashier_id, "opening_cents": 0, "closed_at": None,
                   "counted_cents": None, "expected_cents": None, "movements": []}
        sale = {"request_id": "r1", "cash_session_id": "c-estacao", "created_at": 5, "gross_cents": 1000,
                "promo_discount_cents": 0, "discount_cents": 0, "total_cents": 1000, "change_cents": 0,
                "customer_id": None, "customer_name": "", "customer_document": "", "operator_id": self.cashier_id,
                "seller_id": None, "items": [], "payments": [{"method": "cash", "amount_cents": 1000}]}
        page = {"records": [
            dict(session, id="c-server", opened_at=1),
            dict(session, id="c-estacao", opened_at=2, station_id="st-a", station_name="Caixa 2"),
            dict(session, id="c-estacao-antigo", opened_at=1, station_id="st-a", station_name="Caixa 2"),
            {"kind": "sale", "id": "v1", "status": "completed", "station_id": "st-a", "data": sale,
             "returns": [{"id": "d1", "refund_method": "cash", "total_cents": 1000, "created_at": 6, "items": []}]},
            {"kind": "sale", "id": "v2", "status": "completed", "data": dict(sale, request_id="r2")},
            {"kind": "credit_receipt", "id": "f1", "customer_id": self.customer_id, "method": "cash",
             "amount_cents": 500, "created_at": 7},
            {"kind": "config", "data": {"version": 1, "print_settings": [
                {"kind": "receipt", "driver": "file", "target": "/tmp/x", "format": "escpos58"}],
                "stations": [{"id": "st-a", "name": "Outro nome", "revoked": False},
                             {"id": "st-b", "name": "Caixa 3", "revoked": True}, {"id": "server", "name": "x"}]}},
        ], "next_cursor": "c2", "progress": {"total": 7}}
        result = new.apply_restore(page, "")
        self.assertEqual((result["applied"], result["warnings"]), (7, 1))  # the older open one enters closed
        with new.connection() as conn:
            opened = {row["id"]: row["station_id"] for row in conn.execute(
                "SELECT id,station_id FROM cash_sessions WHERE closed_at IS NULL")}
            self.assertEqual(opened, {"c-server": "server", "c-estacao": "st-a"})
            self.assertEqual({row["id"]: row["station_id"] for row in conn.execute("SELECT id,station_id FROM sales")},
                             {"v1": "st-a", "v2": "server"})
            self.assertEqual([row[0] for row in conn.execute(
                "SELECT station_id FROM sale_returns UNION ALL SELECT station_id FROM credit_receipts")],
                ["server", "server"])
            self.assertEqual([tuple(row) for row in conn.execute("SELECT * FROM print_settings")],
                             [("receipt", "file", "/tmp/x", "escpos58", "server")])
        stations = {row["id"]: row for row in new.stations()}
        self.assertEqual(set(stations), {"server", "st-a", "st-b"})
        self.assertEqual((stations["st-a"]["name"], stations["st-a"]["linked"], stations["st-a"]["cash_open"]),
                         ("Caixa 2", False, True))
        self.assertTrue(stations["st-b"]["revoked_at"])
        with new.station("st-a"):
            self.assertEqual(new.cash_state()["id"], "c-estacao")
        # Giving the recovery up forgets the stations that came with it.
        new.restore_abandon()
        self.assertEqual([row["id"] for row in new.stations()], ["server"])


class JournalTests(StationCase):
    """apply_journal_events: a copy of the database plus the events recorded after it."""

    TABLES = {
        "products": "SELECT id,stock_milli FROM products ORDER BY id",
        "customers": "SELECT id,open_credit_cents,store_credit_cents FROM customers ORDER BY id",
        "cash_sessions": "SELECT * FROM cash_sessions ORDER BY id",
        "cash_movements": "SELECT * FROM cash_movements ORDER BY id",
        "sales": "SELECT id,request_id,session_id,operator_id,customer_id,total_cents,status,void_reason,"
                 "created_at,voided_at,station_id FROM sales ORDER BY id",
        "sale_returns": "SELECT * FROM sale_returns ORDER BY id",
        "credit_receipts": "SELECT * FROM credit_receipts ORDER BY id",
        "outbox": "SELECT id,event_type,aggregate_id,payload,seq FROM outbox ORDER BY seq",
    }

    def setUp(self):
        super().setUp()
        self.station_id = self.link("Caixa 2")["station_id"]
        self.store.open_cash(self.cashier_id, 5000)
        self.sell("antes-da-copia")
        # The full copy an additional cashier keeps of the main one.
        copy = self.store.backup_now()
        self.target_path = Path(self.temp.name) / "promovido" / "terminal.sqlite3"
        self.target_path.parent.mkdir()
        shutil.copy(Path(copy["directory"]) / copy["name"], self.target_path)
        self.target = Store(self.target_path)
        self.copied_seq = self.rows("SELECT MAX(seq) seq FROM outbox")[0]["seq"]

    def journal(self):
        return [{"seq": row["seq"], "event_id": row["id"], "created_at": row["created_at"],
                 "event": json.loads(row["payload"])}
                for row in self.rows("SELECT id,seq,created_at,payload FROM outbox WHERE seq>? ORDER BY seq",
                                     (self.copied_seq,))]

    def state(self, store):
        with store.connection() as conn:
            return {name: [tuple(row) for row in conn.execute(query)] for name, query in self.TABLES.items()}

    def work_after_the_copy(self):
        """Every kind of event, on the main cashier and on an additional one."""
        fiado = self.sell("fiado", customer_id=self.customer_id,
                          payments=[{"method": "credit_account", "amount_cents": 10000}])
        self.store.receive_credit(self.cashier, {"receipt_id": "rec-1", "customer_id": self.customer_id,
                                                 "method": "cash", "amount_cents": 4000})
        self.store.return_sale(self.cashier, fiado["id"], {
            "return_id": "dev-fiado", "refund_method": "account", "reason": "Avaria", "manager_pin": "654321",
            "items": [{"product_id": self.product_id, "quantity_milli": 1000}]})  # 60 abated, 40 become credit
        self.sell("com-credito", customer_id=self.customer_id,
                  payments=[{"method": "store_credit", "amount_cents": 3000}, {"method": "cash", "amount_cents": 7000}])
        cancelled = self.sell("cancelada", quantity=2000)
        self.store.void_sale(self.manager, cancelled["id"], "Cliente desistiu")
        self.store.cash_movement(self.cashier_id, "supplied", 2000, "Troco")
        self.store.cash_movement(self.cashier_id, "withdrawn", 1000, "Sangria")
        self.store.adjust_stock(self.manager, self.product_id, -3000, "Quebra")
        with self.store.station(self.station_id):
            self.store.open_cash(self.second_id, 100)
            kept = self.sell("estacao-1", self.second, quantity=3000)
            self.store.return_sale(self.second, kept["id"], {
                "return_id": "dev-credito", "refund_method": "store_credit", "reason": "Troca",
                "customer_id": self.customer_id, "manager_pin": "654321",
                "items": [{"product_id": self.product_id, "quantity_milli": 1000}]})
            self.store.return_sale(self.second, kept["id"], {
                "return_id": "dev-dinheiro", "refund_method": "cash", "reason": "Troca", "manager_pin": "654321",
                "items": [{"product_id": self.product_id, "quantity_milli": 1000}]})
        self.store.close_cash(self.cashier_id, 0, "654321")

    def test_every_event_rebuilds_the_local_state(self):
        self.work_after_the_copy()
        events = self.journal()
        self.assertEqual({item["event"]["type"] for item in events},
                         {"sale.completed", "sale.voided", "sale.returned", "credit.received", "cash.opened",
                          "cash.supplied", "cash.withdrawn", "cash.closed", "stock.adjusted"})
        audits = len(self.target.audit())
        with patch.object(terminal_store.uuid, "uuid4", wraps=uuid.uuid4):
            result = self.target.apply_journal_events(events)
        self.assertEqual(result, {"applied": len(events), "skipped": 0, "invalid": 0, "foreign": 0, "reseq": 0,
                                  "warnings": 0, "last_seq": events[-1]["seq"]})
        self.assertEqual(self.state(self.target), self.state(self.store))
        with self.target.connection() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM print_jobs").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM outbox WHERE acked_at IS NULL").fetchone()[0],
                             len(self.rows("SELECT 1 FROM outbox")))
            self.assertEqual([tuple(row) for row in conn.execute("SELECT id,name,token_hash FROM stations WHERE id=?",
                                                                 (self.station_id,))][0][:2],
                             (self.station_id, "Caixa 2"))
        trail = self.target.audit()
        self.assertEqual(len(trail), audits + 1)
        self.assertEqual(trail[0]["action"], "promotion.journal_applied")
        # The rebuilt sale prints like the original one.
        with self.target.station(self.station_id):
            self.assertEqual(self.target.cash_summary(), self.cash_summary_of(self.station_id))
        sale_id = self.rows("SELECT id FROM sales WHERE request_id='estacao-1'")[0]["id"]
        rebuilt = self.target.sale_detail(sale_id)
        self.assertEqual((rebuilt["station_id"], rebuilt["station_name"], rebuilt["operator_name"]),
                         (self.station_id, "Caixa 2", "Caio"))
        # A second run is harmless.
        again = self.target.apply_journal_events(events)
        self.assertEqual((again["applied"], again["skipped"], again["warnings"]), (0, len(events), 0))
        self.assertEqual(self.state(self.target), self.state(self.store))
        self.assertEqual(self.stock(self.target), self.stock())

    def cash_summary_of(self, station_id):
        with self.store.station(station_id):
            return self.store.cash_summary()

    def test_foreign_invalid_and_renumbered_events(self):
        with self.store.station(self.station_id):
            self.store.open_cash(self.second_id, 0)
            self.sell("estacao-1", self.second)
        events = self.journal()
        foreign = json.loads(json.dumps(events[1]))
        foreign["event_id"] = foreign["event"]["event_id"] = uuid.uuid4().hex
        foreign["event"]["identity"]["shop_id"] = str(uuid.uuid4())
        # Same number as the last real event: the real one keeps it, this one goes to the end.
        taken = {"seq": events[-1]["seq"], "event_id": uuid.uuid4().hex, "created_at": None, "event": {
            "type": "stock.adjusted", "aggregate_id": "a1", "identity": events[0]["event"]["identity"],
            "data": {"product_id": self.product_id, "delta_milli": -2000, "reason": "Quebra", "created_at": 9}}}
        taken["event"]["event_id"] = taken["event_id"]
        broken = {"seq": 99, "event_id": uuid.uuid4().hex, "created_at": None, "event": {
            "type": "sale.returned", "aggregate_id": "sem-dados", "identity": events[0]["event"]["identity"],
            "data": {}}}
        broken["event"]["event_id"] = broken["event_id"]
        bad = [None, {"seq": 5}, {"seq": 0, "event_id": "x", "event": {}},
               dict(events[0], event_id="0" * 32), dict(events[0], seq="7")]
        before = self.stock(self.target)
        result = self.target.apply_journal_events([*events, foreign, taken, broken, *bad])
        self.assertEqual((result["applied"], result["foreign"], result["invalid"], result["reseq"], result["warnings"]),
                         (len(events) + 2, 1, len(bad), 1, 1))
        self.assertEqual(self.stock(self.target), before - 1000 - 2000)
        with self.target.connection() as conn:
            stored = {row["id"]: row["seq"] for row in conn.execute("SELECT id,seq FROM outbox")}
            self.assertNotIn(foreign["event_id"], stored)
            self.assertIn(broken["event_id"], stored)  # it happened at the counter: it still goes up
            self.assertEqual(stored[taken["event_id"]], events[-1]["seq"] + 1)
            self.assertEqual(stored[broken["event_id"]], 99)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM sale_returns").fetchone()[0], 0)
        for item in events:
            self.assertEqual(stored[item["event_id"]], item["seq"])
        with self.assertRaises(TerminalError):
            self.target.apply_journal_events("não é lista")

    def test_a_second_opening_on_the_same_station_enters_closed(self):
        with self.store.station(self.station_id):
            self.store.open_cash(self.second_id, 0)
        events = self.journal()
        duplicate = json.loads(json.dumps(events[0]))
        duplicate["seq"] += 1
        duplicate["event_id"] = duplicate["event"]["event_id"] = uuid.uuid4().hex
        duplicate["event"]["aggregate_id"] = "outro-caixa"
        duplicate["event"]["data"]["created_at"] += 10
        result = self.target.apply_journal_events([events[0], duplicate])
        self.assertEqual((result["applied"], result["warnings"]), (2, 1))
        with self.target.connection() as conn:
            rows = {row["id"]: row for row in conn.execute("SELECT * FROM cash_sessions WHERE station_id=?",
                                                           (self.station_id,))}
        self.assertIsNone(rows[events[0]["event"]["aggregate_id"]]["closed_at"])
        self.assertEqual(rows["outro-caixa"]["closed_at"], rows["outro-caixa"]["opened_at"])

    def test_events_of_a_build_without_stations_belong_to_the_main_cashier(self):
        self.store.close_cash(self.cashier_id, 15000, "654321")
        self.set_capabilities([])
        self.store.open_cash(self.cashier_id, 0)
        self.sell("sem-etiqueta")
        self.target.apply_journal_events(self.journal())
        self.assertEqual(self.target.cash_state()["id"], self.store.cash_state()["id"])
        with self.target.connection() as conn:
            self.assertEqual(conn.execute("SELECT station_id FROM sales WHERE request_id='sem-etiqueta'").fetchone()[0],
                             "server")


class PromotionTests(StationCase):
    def setUp(self):
        super().setUp()
        self.first = self.link("Caixa 2")
        self.second_station = self.link("Caixa 3")
        self.station_id = self.first["station_id"]
        self.target = str(Path(self.temp.name) / "impressora-estacao")
        self.store.save_printer("receipt", "file", str(Path(self.temp.name) / "impressora-servidor"), "escpos80")
        self.server_cash = self.store.open_cash(self.cashier_id, 1000)
        self.sell("servidor-1")
        with self.store.station(self.station_id):
            self.store.save_printer("receipt", "file", self.target, "escpos58")
            self.station_cash = self.store.open_cash(self.second_id, 2000)
            self.sell("estacao-1", self.second)
        with self.store.station(self.second_station["station_id"]):
            self.other_cash = self.store.open_cash(self.manager_id, 0)
        self.store.login(self.cashier_id, "123987")
        with self.assertRaisesCode(401):
            self.store.login(self.cashier_id, "000000")
        self.set_limit(3)
        self.store.create_station_code(self.manager_id)

    def promote(self):
        return self.store.finish_promotion(self.station_id, "Caixa 2", MIRROR_KEY, self.manager_id)

    def test_station_becomes_the_server_with_two_cashiers_open(self):
        self.assertEqual(self.store.promotion_status()["state"], "none")
        result = self.promote()
        old = result["previous_server_station"]
        self.assertRegex(old, r"^anterior-[0-9a-f]{8}$")
        self.assertEqual((result["promoted"], result["previous_server_cash_open"], result["generation"]), (True, True, 2))
        # The cashier that was open on the promoted computer goes on, now as the main one.
        self.assertEqual(self.store.cash_state()["id"], self.station_cash["id"])
        self.assertEqual(self.store.cash_summary()["expected_cents"], 12000)
        with self.store.station(old):
            self.assertEqual(self.store.cash_state()["id"], self.server_cash["id"])
        with self.store.station(self.second_station["station_id"]):
            self.assertEqual(self.store.cash_state()["id"], self.other_cash["id"])
        self.assertEqual({row["request_id"]: row["station_id"] for row in self.rows(
            "SELECT request_id,station_id FROM sales")}, {"servidor-1": old, "estacao-1": "server"})
        self.assertEqual(self.store.printer_settings(), [{"kind": "receipt", "driver": "file", "target": self.target,
                                                          "format": "escpos58"}])
        self.assertEqual({row["station_id"]: row["status"] for row in self.rows(
            "SELECT station_id,status FROM print_jobs")}, {old: "uncertain", "server": "queued"})
        stations = {row["id"]: row for row in self.store.stations()}
        self.assertEqual(set(stations), {"server", old, self.second_station["station_id"]})
        self.assertEqual((stations[old]["name"], bool(stations[old]["revoked_at"]), stations[old]["cash_open"]),
                         ("Caixa principal anterior", True, True))
        self.assertFalse(stations[self.second_station["station_id"]]["linked"])
        for token in (self.first["token"], self.second_station["token"]):
            with self.assertRaisesCode(401, "station_unauthorized"):
                self.store.authenticate_station(token)
        self.assertEqual(self.rows("SELECT * FROM login_attempts"), [])
        self.assertEqual(self.rows("SELECT * FROM station_enroll_codes"), [])
        meta = self.store.meta()
        self.assertEqual(json.loads(meta["mirror_key"]), MIRROR_KEY)
        self.assertEqual((meta["promotion_state"], meta["server_generation"]), ("awaiting_cloud", "2"))
        for gone in ("token", "last_heartbeat_at", "last_heartbeat_error", "last_sync_error"):
            self.assertNotIn(gone, meta)
        status = self.store.promotion_status()
        self.assertEqual((status["promoted"], status["state"], status["previous_server_cash_open"]),
                         (True, "awaiting_cloud", True))
        self.assertGreater(status["pending_events"], 0)
        self.assertAlmostEqual(status["promoted_at"], time.time() * 1000, delta=5000)
        self.assertEqual(self.store.audit()[0]["action"], "terminal.promoted")
        # It goes on selling with the license that came in the copy, without a cloud token.
        license_now = self.store.lease_status()
        self.assertEqual((license_now["state"], license_now["can_sell"], license_now["can_open_cash"]),
                         ("active", True, True))
        self.assertFalse(self.store.connection_info()["paired"])
        self.assertEqual(self.sell("depois-da-promocao", self.second)["station_id"], "server")
        # The manager counts the drawer of the computer that stopped and closes it from here.
        self.store.close_cash(self.manager_id, 11000, "654321", station_id=old)
        self.assertFalse(self.store.promotion_status()["previous_server_cash_open"])
        # The other cashier links again with a new code and keeps identity and open cashier.
        code = self.store.create_station_code(self.manager_id)["code"]
        back = self.store.enroll_station(code, station_id=self.second_station["station_id"])
        self.assertEqual(back["station_id"], self.second_station["station_id"])
        with self.store.station(back["station_id"]):
            self.assertEqual(self.store.cash_state()["id"], self.other_cash["id"])

    def test_promotion_is_refused_on_an_incomplete_database(self):
        with self.assertRaisesCode(409):
            self.store.finish_promotion("server", "x", MIRROR_KEY)
        with self.store.transaction() as conn:
            self.store._set_meta(conn, "restore_state", "restoring")
        with self.assertRaisesCode(409):
            self.promote()
        unpaired = Store(Path(self.temp.name) / "vazio" / "terminal.sqlite3")
        with self.assertRaisesCode(409):
            unpaired.finish_promotion("abc", "Caixa 2", MIRROR_KEY)
        self.assertEqual(self.store.promotion_status()["state"], "none")

    def pairing_answer(self, **changes):
        device = self.store.promotion_device_id()
        answer = {"token": "novo-" + "z" * 43, "tenant_id": self.tenant_id, "shop_id": self.shop_id,
                  "terminal_id": self.terminal_id, "public_key": self.public, "recovery": True,
                  "lease": self.signed(max_stations=2, device_id=device, issued=int(time.time())),
                  "capabilities": ["stations", "restore"], "terminal": {"name": "Caixa 1"}}
        answer.update(changes)
        return answer

    def test_adopt_pairing_binds_the_same_terminal_to_this_computer(self):
        with self.assertRaisesCode(409):
            self.store.adopt_pairing(self.pairing_answer(), "http://127.0.0.1:8000")  # nothing was promoted
        old_device = self.store.identity()["device_id"]
        self.promote()
        device = self.store.promotion_device_id()
        self.assertEqual(self.store.promotion_device_id(), device)
        self.assertNotEqual(device, old_device)
        for field in ("terminal_id", "shop_id", "tenant_id"):
            with self.assertRaisesCode(409, "other_terminal") as refused:
                self.store.adopt_pairing(self.pairing_answer(**{field: str(uuid.uuid4())}), "http://127.0.0.1:8000")
            self.assertIn("outra loja ou de outro caixa", str(refused.exception))
        with self.assertRaisesCode(403):  # a license of another device
            self.store.adopt_pairing(self.pairing_answer(lease=self.signed(max_stations=2, issued=int(time.time()))),
                                     "http://127.0.0.1:8000")
        with self.assertRaisesCode(409):
            self.store.adopt_pairing(self.pairing_answer(public_key=generate_keypair()[1]), "http://127.0.0.1:8000")
        with self.assertRaises(TerminalError):
            self.store.adopt_pairing({"token": "x"}, "http://127.0.0.1:8000")
        # Every refusal left the promoted database exactly as it was.
        self.assertEqual((self.store.identity()["device_id"], self.store.promotion_status()["state"]),
                         (old_device, "awaiting_cloud"))
        self.assertNotIn("token", self.store.meta())
        answer = self.pairing_answer()
        self.assertEqual(self.store.adopt_pairing(answer, "https://nuvem.example"), {"regularized": True})
        meta = self.store.meta()
        self.assertEqual((meta["token"], meta["cloud_url"], meta["device_id"], meta["promotion_state"]),
                         (answer["token"], "https://nuvem.example", device, "done"))
        self.assertFalse(self.store.restore_status()["active"])  # `recovery: true` starts nothing here
        self.assertTrue(self.store.lease_status()["can_sell"])
        self.assertTrue(self.store.connection_info()["paired"])
        self.assertEqual(self.store.promotion_status()["state"], "done")
        self.assertEqual(self.store.audit()[0]["action"], "terminal.promotion_regularized")
        self.assertEqual(self.store.cash_state()["id"], self.station_cash["id"])
        with self.assertRaisesCode(409):
            self.store.adopt_pairing(answer, "https://nuvem.example")


if __name__ == "__main__":
    unittest.main()
