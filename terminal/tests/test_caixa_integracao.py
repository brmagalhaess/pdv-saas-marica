"""Pendências do caixa depois da junção das frentes: busca sem acento, revisão, conflitos, segurança."""
import json
import sqlite3
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from shared.licensing import generate_keypair, payload_digest, sign_lease
from shared.security import hash_pin
from terminal import app as terminal_app
from terminal.app import create_app
from terminal.store import Store, TerminalError
from terminal.sync import sync_once

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "terminal" / "static"


class IntegrationCase(unittest.TestCase):
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
        self.private, self.public = generate_keypair()
        self.operators = [
            {"id": self.cashier_id, "name": "Ana", "role": "cashier", "pin_hash": hash_pin("123987")},
            {"id": self.manager_id, "name": "Bruno", "role": "manager", "pin_hash": hash_pin("654321")},
        ]
        rows = [("Água mineral 500 ml", "AGUA500", "7890000000019", "Bebidas"),
                ("Pão francês", "PAO", "7890000000026", "Padaria"),
                ("Açúcar refinado 1 kg", "ACUCAR", "7890000000033", "Mercearia"),
                ("AGUARDENTE 1 L", "CANA", "7890000000040", "Bebidas"),
                ("Desconto 100%_real", "PCT", None, "Mercearia")]
        self.products = [{"id": str(uuid.uuid4()), "sku": sku, "barcode": barcode, "name": name, "unit": "UN",
                          "category": category, "price_cents": 500, "quantity_milli": 50000, "active": True}
                         for name, sku, barcode, category in rows]
        self.water = self.products[0]
        self.cashier = {"id": self.cashier_id, "name": "Ana", "role": "cashier"}
        self.manager = {"id": self.manager_id, "name": "Bruno", "role": "manager"}
        self.pair()

    def signed(self, operators):
        now = int(time.time())
        claims = {"schema": 1, "tenant_id": self.tenant_id, "shop_id": self.shop_id,
                  "terminal_id": self.terminal_id, "device_id": self.store.identity()["device_id"],
                  "issued_at": now - 5, "expires_at": now + 3600, "features": ["pdv"], "status": "active",
                  "operators_hash": payload_digest(operators)}
        return sign_lease(claims, self.private)

    def pair(self, operators=None):
        operators = self.operators if operators is None else operators
        lease = self.signed(operators)
        self.store.set_pairing({"token": "t" * 48, "tenant_id": self.tenant_id, "shop_id": self.shop_id,
                                "terminal_id": self.terminal_id, "lease": lease, "public_key": self.public},
                               "http://127.0.0.1:8000")
        self.bootstrap = {"products": self.products, "operators": operators, "lease": lease, "public_key": self.public}
        self.store.apply_bootstrap(self.bootstrap)

    def csrf(self):
        return self.client.get("/api/session").json["csrf"]

    def login(self, operator_id=None, pin="123987"):
        response = self.client.post("/api/login", json={"operator_id": operator_id or self.cashier_id, "pin": pin},
                                    headers={"X-CSRF-Token": self.csrf()})
        self.assertEqual(response.status_code, 200, response.json)

    def post(self, path, payload):
        return self.client.post(path, json=payload, headers={"X-CSRF-Token": self.csrf()})

    def names(self, query, category=""):
        return [row["name"] for row in self.store.products(query, category)]


class ProductSearchTests(IntegrationCase):
    def test_search_ignores_accents_and_case(self):
        self.assertEqual(self.names("agua"), ["AGUARDENTE 1 L", "Água mineral 500 ml"])
        self.assertEqual(self.names("ÁGUA MINERAL"), ["Água mineral 500 ml"])
        self.assertEqual(self.names("pao"), ["Pão francês"])
        self.assertEqual(self.names("FRANCES"), ["Pão francês"])
        self.assertEqual(self.names("açucar"), ["Açúcar refinado 1 kg"])
        # Every typed word must match, in any order.
        self.assertEqual(self.names("mineral agua"), ["Água mineral 500 ml"])
        self.assertEqual(self.names("agua", "Bebidas"), ["AGUARDENTE 1 L", "Água mineral 500 ml"])
        self.assertEqual(self.names("agua", "Padaria"), [])
        self.assertEqual(self.names("cana"), ["AGUARDENTE 1 L"])  # part of the SKU, any case
        self.assertEqual(self.names("zzz"), [])

    def test_exact_barcode_and_sku_still_win(self):
        # "AGUA500" is the exact SKU: one product, not every name containing "agua".
        self.assertEqual(self.names("AGUA500"), ["Água mineral 500 ml"])
        self.assertEqual(self.names(self.water["barcode"]), ["Água mineral 500 ml"])
        self.assertEqual(self.names(self.water["id"]), ["Água mineral 500 ml"])

    def test_like_wildcards_typed_by_the_operator_are_literal(self):
        self.assertEqual(self.names("%"), ["Desconto 100%_real"])
        self.assertEqual(self.names("0%_r"), ["Desconto 100%_real"])
        self.assertEqual(self.names("_"), ["Desconto 100%_real"])

    def test_route_returns_page_and_total(self):
        self.login()
        found = self.client.get("/api/products?q=agua").json
        self.assertEqual([row["name"] for row in found["products"]], ["AGUARDENTE 1 L", "Água mineral 500 ml"])
        self.assertEqual((found["total"], found["limit"]), (2, 150))
        self.assertIn("Bebidas", found["categories"])
        many = [{"id": str(uuid.uuid4()), "sku": f"S{index:03d}", "barcode": None, "name": f"Maçã {index:03d}",
                 "unit": "UN", "category": "Hortifruti", "price_cents": 100, "quantity_milli": 1000, "active": True}
                for index in range(180)]
        self.store.apply_bootstrap(dict(self.bootstrap, products=self.products + many))
        page = self.client.get("/api/products?q=maca").json
        self.assertEqual((len(page["products"]), page["total"]), (150, 180))
        everything = self.client.get("/api/products").json
        self.assertEqual((len(everything["products"]), everything["total"]), (150, 185))
        self.assertEqual(len(self.store.products()), 150)

    def test_catalog_refresh_keeps_search_text_current(self):
        renamed = [dict(self.water, name="Refresco de caju")] + self.products[1:]
        self.store.apply_bootstrap(dict(self.bootstrap, products=renamed))
        self.assertEqual(self.names("mineral"), [])
        self.assertEqual(self.names("CAJÚ"), ["Refresco de caju"])

    def test_database_created_before_search_column_is_backfilled(self):
        old_path = Path(self.temp.name) / "antigo.sqlite3"
        with sqlite3.connect(old_path) as conn:
            conn.execute("""CREATE TABLE products(
                id TEXT PRIMARY KEY,sku TEXT,barcode TEXT UNIQUE,ncm TEXT,scale_code TEXT,name TEXT NOT NULL,
                unit TEXT NOT NULL,category TEXT,price_cents INTEGER NOT NULL,cost_cents INTEGER NOT NULL DEFAULT 0,
                image_url TEXT NOT NULL DEFAULT '',stock_controlled INTEGER NOT NULL DEFAULT 1,
                stock_milli INTEGER NOT NULL,active INTEGER NOT NULL,updated_at INTEGER NOT NULL)""")
            conn.execute("INSERT INTO products (id,sku,barcode,name,unit,category,price_cents,stock_milli,active,updated_at) "
                         "VALUES ('p1',NULL,'789','Feijão carioca','UN','Mercearia',849,1000,1,1)")
        old = Store(old_path)
        self.assertEqual([row["id"] for row in old.products("feijao")], ["p1"])
        self.assertEqual([row["id"] for row in Store(old_path).products("FEIJÃO CARIOCA")], ["p1"])

    def test_customer_search_ignores_accents(self):
        customers = [{"id": str(uuid.uuid4()), "name": "João Batista", "document": "", "credit_limit_cents": 0,
                      "open_credit_cents": 0, "store_credit_cents": 0},
                     {"id": str(uuid.uuid4()), "name": "Maria", "document": "123", "credit_limit_cents": 0,
                      "open_credit_cents": 0, "store_credit_cents": 0}]
        self.store.apply_bootstrap(dict(self.bootstrap, customers=customers))
        self.assertEqual([row["name"] for row in self.store.customers("joao")], ["João Batista"])
        self.assertEqual([row["name"] for row in self.store.customers("23")], ["Maria"])
        self.assertEqual(len(self.store.customers()), 2)


class StoreQueriesTests(IntegrationCase):
    def test_routes_do_not_query_sqlite_directly(self):
        source = Path(terminal_app.__file__).read_text(encoding="utf-8")
        self.assertNotIn("store.connection()", source)
        self.assertNotIn("conn.execute", source)

    def test_scale_code_and_batch_live_in_the_store(self):
        weighed = {"id": str(uuid.uuid4()), "sku": "TOMATE", "barcode": None, "name": "Tomate", "unit": "KG",
                   "scale_code": "54321", "category": "Hortifruti", "price_cents": 899, "quantity_milli": 20000,
                   "active": True}
        self.store.apply_bootstrap(dict(self.bootstrap, products=self.products + [weighed]))
        self.assertEqual(self.store.product_by_scale_code("54321")["id"], weighed["id"])
        with self.assertRaises(TerminalError) as raised:
            self.store.product_by_scale_code("99999")
        self.assertEqual(raised.exception.status, 404)
        rows = self.store.products_by_ids([self.water["id"], "", weighed["id"], "desconhecido"])
        self.assertEqual({row["id"] for row in rows}, {self.water["id"], weighed["id"]})
        self.assertEqual(self.store.products_by_ids([]), [])


class ReviewRoutesTests(IntegrationCase):
    def quarantine_one(self):
        self.store.open_cash(self.cashier_id, 0)
        events = self.store.pending_events()
        ids = [event["event_id"] for event in events]
        with patch("terminal.sync._request", return_value={
                "accepted_event_ids": ids, "conflicts": [],
                "quarantined": [{"event_id": ids[0], "reason": "rejected_event", "message": "Caixa já aberto"}]}):
            sync_once(self.store)
        return ids[0]

    def test_quarantine_and_skipped_need_a_manager(self):
        event_id = self.quarantine_one()
        self.store.apply_bootstrap(dict(self.bootstrap, products=self.products + [{"id": "ruim", "name": ""}]))
        for path in ("/api/sync/quarantine", "/api/catalog/skipped"):
            self.assertEqual(self.client.get(path).status_code, 401, path)
        self.login()
        for path in ("/api/sync/quarantine", "/api/catalog/skipped"):
            self.assertEqual(self.client.get(path).status_code, 403, path)
        self.assertEqual(self.client.get("/api/session").json["sync"]["quarantined"], 1)
        self.assertEqual(self.client.get("/api/sync").json["sync"]["quarantined"], 1)
        self.login(self.manager_id, "654321")
        events = self.client.get("/api/sync/quarantine").json["events"]
        self.assertEqual([(row["event_id"], row["event_type"], row["message"]) for row in events],
                         [(event_id, "cash.opened", "Caixa já aberto")])
        self.assertEqual(self.client.get("/api/sync/quarantine?status=resolved").json["events"], [])
        self.assertEqual(self.client.get("/api/sync/quarantine?status=xyz").status_code, 400)
        skipped = self.client.get("/api/catalog/skipped").json["items"]
        self.assertEqual([(row["kind"], row["id"]) for row in skipped], [("product", "ruim")])

    def test_screen_reads_the_field_the_backend_sends(self):
        script = (STATIC / "terminal.js").read_text(encoding="utf-8")
        self.assertIn("sync?.quarantined", script)
        self.assertIn("/api/sync/quarantine", script)
        self.assertIn("/api/catalog/skipped", script)
        html = self.client.get("/").get_data(as_text=True)
        for element_id in ("reviewChip", "reviewList", "stockCount"):
            self.assertIn(f'id="{element_id}"', html)


class VoucherTextTests(IntegrationCase):
    def test_return_and_fiado_vouchers_are_sent_with_accents(self):
        customer_id = str(uuid.uuid4())
        self.store.apply_bootstrap(dict(self.bootstrap, customers=[
            {"id": customer_id, "name": "Seu Zé", "document": "", "credit_limit_cents": 5000,
             "open_credit_cents": 1500, "store_credit_cents": 0}]))
        self.store.open_cash(self.cashier_id, 1000)
        sale = self.store.sale(self.cashier, {
            "request_id": "v1", "customer_id": customer_id,
            "items": [{"product_id": self.water["id"], "quantity_milli": 2000}],
            "payments": [{"method": "cash", "amount_cents": 1000}]})
        docs = []
        real = Store._queue_voucher

        def spy(store, conn, doc):
            docs.append(doc)
            return real(store, conn, doc)

        with patch.object(Store, "_queue_voucher", spy):
            back = self.store.return_sale(self.manager, sale["id"], {
                "return_id": "d1", "refund_method": "store_credit", "reason": "Troca",
                "items": [{"product_id": self.water["id"], "quantity_milli": 1000}]})
            self.store.receive_credit(self.cashier, {"receipt_id": "r1", "customer_id": customer_id,
                                                     "amount_cents": 500, "method": "card_debit"})
        sent = json.dumps(docs, ensure_ascii=False)
        self.assertEqual(docs[0]["title"], "DEVOLUÇÃO")
        self.assertTrue(docs[0]["reference"].startswith("Devolução: "))
        self.assertIn(["pair", "Devolvido em", "Crédito na loja"], docs[0]["rows"])
        self.assertIn("Crédito na loja disponível", sent)
        for stale in ("DEVOLUCAO", "Devolucao", "Credito", "disponivel"):
            self.assertNotIn(stale, sent)
        # Same payment wording as the sale receipt.
        self.assertIn(["pair", "Forma", "Cartão de débito"], docs[1]["rows"])
        printed = json.dumps(back["receipt"], ensure_ascii=False)
        self.assertIn("DEVOLUÇÃO", printed)
        self.assertIn("Crédito na loja disponível", printed)


class ConflictKeyTests(IntegrationCase):
    def conflict(self, event_id, reason, **extra):
        return {"id": str(uuid.uuid4()), "event_id": event_id, "event_type": "sale.completed",
                "aggregate_id": "s1", "reason": reason, "status": "open", "detail": {"reason": reason}, **extra}

    def test_every_reason_of_one_event_is_kept(self):
        first = [self.conflict("e1", "negative_stock"), self.conflict("e1", "price_changed"),
                 self.conflict("e2", "negative_stock")]
        self.store.record_sync_conflicts(first)
        rows = self.store.sync_conflicts()
        self.assertEqual(sorted((row["event_id"], row["reason"]) for row in rows),
                         [("e1", "negative_stock"), ("e1", "price_changed"), ("e2", "negative_stock")])
        self.assertEqual(self.store.sync_status()["conflicts"], 3)
        # A replayed answer updates the detail and never duplicates or reopens a row.
        kept = next(row for row in rows if (row["event_id"], row["reason"]) == ("e1", "price_changed"))
        self.store.resolve_sync_conflict(kept["id"], "resolved", "Conferido")
        self.store.record_sync_conflicts([self.conflict("e1", "price_changed", detail={"novo": True}),
                                          # same cloud id arriving for another reason must not be lost
                                          dict(self.conflict("e1", "customer_missing"), id=first[0]["id"])])
        rows = self.store.sync_conflicts()
        self.assertEqual(len(rows), 4)
        again = next(row for row in rows if row["id"] == kept["id"])
        self.assertEqual((again["status"], again["resolution"], again["detail"]), ("resolved", "Conferido", {"novo": True}))
        self.assertEqual(len({row["id"] for row in rows}), 4)

    def test_resolving_another_reason_does_not_release_the_quarantine(self):
        self.store.open_cash(self.cashier_id, 0)
        event = self.store.pending_events()[0]
        answer = {"accepted_event_ids": [event["event_id"]],
                  "conflicts": [self.conflict(event["event_id"], "rejected_event"),
                                self.conflict(event["event_id"], "cash_mismatch")],
                  "quarantined": [{"event_id": event["event_id"], "reason": "rejected_event", "message": "x"}]}
        with patch("terminal.sync._request", return_value=answer):
            sync_once(self.store)
        rows = {row["reason"]: row for row in self.store.sync_conflicts()}
        self.assertEqual(sorted(rows), ["cash_mismatch", "rejected_event"])
        self.store.resolve_sync_conflict(rows["cash_mismatch"]["id"], "resolved")
        self.assertEqual(self.store.sync_status()["quarantined"], 1)
        self.store.resolve_sync_conflict(rows["rejected_event"]["id"], "resolved")
        self.assertEqual(self.store.sync_status()["quarantined"], 0)

    def test_existing_database_unique_by_event_is_migrated_with_its_rows(self):
        old_path = Path(self.temp.name) / "antigo.sqlite3"
        with sqlite3.connect(old_path) as conn:
            conn.execute("""CREATE TABLE sync_conflicts(
                id TEXT PRIMARY KEY,event_id TEXT NOT NULL UNIQUE,event_type TEXT NOT NULL,
                aggregate_id TEXT NOT NULL,reason TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'open',detail TEXT NOT NULL,
                created_at INTEGER NOT NULL,resolved_at INTEGER,
                resolution TEXT NOT NULL DEFAULT '')""")
            conn.execute("CREATE INDEX sync_conflicts_status ON sync_conflicts(status,created_at)")
            conn.execute("INSERT INTO sync_conflicts VALUES ('c1','e1','sale.completed','s1','negative_stock',"
                         "'acknowledged','{\"a\":1}',7,NULL,'visto')")
        old = Store(old_path)
        row = old.sync_conflicts()[0]
        self.assertEqual((row["id"], row["status"], row["detail"], row["created_at"], row["resolution"]),
                         ("c1", "acknowledged", {"a": 1}, 7, "visto"))
        old.record_sync_conflicts([self.conflict("e1", "price_changed")])
        self.assertEqual(len(old.sync_conflicts()), 2)
        reopened = Store(old_path)  # the migration runs once
        self.assertEqual(len(reopened.sync_conflicts()), 2)
        with reopened.connection() as conn:
            tables = [name for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
            indexes = [name for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")]
        self.assertNotIn("sync_conflicts_old", tables)
        self.assertIn("sync_conflicts_status", indexes)


class StaleSyncErrorTests(IntegrationCase):
    def test_empty_queue_with_answering_cloud_clears_the_old_error(self):
        self.store.set_heartbeat_result()
        self.store.set_sync_result("Servidor recusou pedido (500): erro")
        with patch("terminal.sync._request") as send:
            self.assertEqual(sync_once(self.store), {"sent": 0, "remaining": 0})
        send.assert_not_called()
        self.assertFalse(self.store.sync_status()["last_sync_error"])

    def test_empty_queue_keeps_the_error_while_the_cloud_is_unreachable(self):
        self.store.set_sync_result("Sem conexão com o servidor; dados locais preservados")
        sync_once(self.store)  # never heard from the cloud
        self.assertTrue(self.store.sync_status()["last_sync_error"])
        self.store.set_heartbeat_result()
        self.store.set_heartbeat_result("Sem conexão com o servidor; dados locais preservados")
        sync_once(self.store)
        self.assertTrue(self.store.sync_status()["last_sync_error"])

    def test_delivered_batch_clears_the_error(self):
        self.store.open_cash(self.cashier_id, 0)
        self.store.set_sync_result("Confirmação incompleta do servidor")
        ids = [event["event_id"] for event in self.store.pending_events()]
        with patch("terminal.sync._request", return_value={"accepted_event_ids": ids}):
            sync_once(self.store)
        self.assertFalse(self.store.sync_status()["last_sync_error"])


class MaintenanceSecurityTests(IntegrationCase):
    def test_unpair_of_a_paired_terminal_needs_a_manager(self):
        for payload in ({"confirm": True}, {"confirm": True, "manager_pin": "123987"},
                        {"confirm": True, "manager_pin": "000000"}):
            response = self.post("/api/unpair", payload)
            self.assertEqual(response.status_code, 403, response.json)
            self.assertIn("PIN de gerente", response.json["error"])
        self.assertEqual(self.store.identity()["terminal_id"], self.terminal_id)
        response = self.post("/api/unpair", {"confirm": True, "manager_pin": "654321"})
        self.assertEqual(response.status_code, 200, response.json)
        self.assertIsNone(self.store.identity()["terminal_id"])

    def test_cashier_session_is_not_enough_and_manager_session_is(self):
        self.login()
        response = self.post("/api/unpair", {"confirm": True})
        self.assertEqual(response.status_code, 403, response.json)
        self.login(self.manager_id, "654321")
        # Authorized by the session; what stops it now is the history rule (a login is history).
        response = self.post("/api/unpair", {"confirm": True})
        self.assertEqual(response.status_code, 409, response.json)
        self.assertIn("histórico", response.json["error"])
        self.assertEqual(self.store.identity()["terminal_id"], self.terminal_id)

    def test_new_profile_of_a_paired_terminal_needs_a_manager_pin(self):
        response = self.post("/api/new-profile", {"confirm": True})
        self.assertEqual(response.status_code, 403, response.json)
        self.assertIs(self.app.extensions["terminal_store"], self.store)
        self.assertFalse((Path(self.temp.name) / "active-profile.json").exists())
        response = self.post("/api/new-profile", {"confirm": True, "manager_pin": "654321"})
        self.assertEqual(response.status_code, 200, response.json)
        self.assertIsNot(self.app.extensions["terminal_store"], self.store)

    def test_wrong_pins_lock_the_maintenance_routes(self):
        for _ in range(5):
            self.assertEqual(self.post("/api/unpair", {"confirm": True, "manager_pin": "111111"}).status_code, 403)
        self.assertEqual(self.post("/api/unpair", {"confirm": True, "manager_pin": "654321"}).status_code, 429)
        self.assertEqual(self.store.identity()["terminal_id"], self.terminal_id)

    def test_unpaired_terminal_keeps_both_routes_open(self):
        self.assertEqual(self.post("/api/unpair", {"confirm": True, "manager_pin": "654321"}).status_code, 200)
        self.assertEqual(self.post("/api/unpair", {"confirm": True}).status_code, 200)
        response = self.post("/api/new-profile", {"confirm": True})
        self.assertEqual(response.status_code, 200, response.json)

    def test_pairing_without_any_manager_pin_can_still_be_corrected(self):
        self.assertEqual(self.post("/api/unpair", {"confirm": True, "manager_pin": "654321"}).status_code, 200)
        self.pair(operators=[self.operators[0]])  # only a cashier reached this terminal
        self.assertEqual(self.post("/api/unpair", {"confirm": True}).status_code, 200)

    def test_bootstrap_is_rate_limited(self):
        with patch("terminal.sync.refresh_bootstrap", return_value={"products": 0, "operators": 0}) as refresh:
            for _ in range(terminal_app.BOOTSTRAP_CALLS):
                self.assertEqual(self.post("/api/bootstrap", {}).status_code, 200)
            blocked = self.post("/api/bootstrap", {})
            self.assertEqual(blocked.status_code, 429, blocked.json)
            self.assertIn("aguarde", blocked.json["error"])
            self.assertEqual(refresh.call_count, terminal_app.BOOTSTRAP_CALLS)
            with patch("terminal.app.time.monotonic", return_value=time.monotonic() + 61):
                self.assertEqual(self.post("/api/bootstrap", {}).status_code, 200)

    def test_sqlite_failure_becomes_a_clear_503(self):
        self.login()
        with patch.object(Store, "product_search", side_effect=sqlite3.OperationalError("database is locked")):
            response = self.client.get("/api/products")
        self.assertEqual(response.status_code, 503)
        self.assertIn("Banco de dados local", response.json["error"])
        self.assertNotIn("database is locked", response.json["error"])
        with patch.object(Store, "sale", side_effect=sqlite3.DatabaseError("disk image is malformed")):
            response = self.post("/api/sales", {})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(self.client.get("/api/products").status_code, 200)


class PaymentFocusSourceTests(unittest.TestCase):
    """The key handling itself is exercised in the browser; this pins the pieces that fixed the leak."""

    @classmethod
    def setUpClass(cls):
        cls.js = (STATIC / "terminal.js").read_text(encoding="utf-8")

    def body(self, start, end):
        return self.js[self.js.index(start):self.js.index(end)]

    def test_entering_payment_takes_the_reading_field_out_of_play(self):
        start = self.body("function startPayment", "async function selectPayMethod")
        self.assertIn("scan.value = ''", start)
        self.assertIn("scan.blur()", start)
        self.assertIn("$('paymentMethods').focus()", start)

    def test_digit_picks_the_form_in_the_same_key_event(self):
        pick = self.body("function pickPayDigit", "function paymentName")
        self.assertNotIn("setTimeout", pick)
        self.assertNotIn("queuePayDigit", self.js)
        select = self.body("async function selectPayMethod", "let payBurstUntil")
        # The amount field is focused before any await of the function.
        self.assertLess(select.index("focusPay()"), select.index("await "))
        keys = self.body("function handlePayKey", "// Single keyboard entry point")
        self.assertIn("if (target === scan)", keys)
        self.assertRegex(keys, r"/\^\[0-9\]\$/\.test\(key\)\) \{ event\.preventDefault\(\); pickPayDigit\(key\)")
        self.assertIn("if (key.length === 1) event.preventDefault();", keys)

    def test_leaving_payment_clears_the_reading_field(self):
        cancel = self.body("function cancelPayment", "function startPayment")
        self.assertIn("'productSearch'", cancel)
        clear = self.body("function clearLocalSale", "function resetSale")
        self.assertIn("'productSearch'", clear)
        self.assertIn("focusScan()", self.body("function resetSale", "async function discardSale"))

    def test_text_inserted_without_key_events_is_dropped_while_paying(self):
        guard = self.body("$('productSearch').addEventListener('input'", "$('cartItems').addEventListener")
        self.assertIn("if (state.pay.active) { $('productSearch').value = ''; focusPay(); return; }", guard)


if __name__ == "__main__":
    unittest.main()
