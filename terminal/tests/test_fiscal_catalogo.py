"""Fiscal data of the catalog: stored from the cloud, compatible with old databases and clouds,
and a discreet warning (never a block) when a sold item is not ready for a NFC-e."""
import json
import sqlite3
import tempfile
import time
import unittest
import uuid
from pathlib import Path

from shared.licensing import generate_keypair, payload_digest, sign_lease
from shared.security import hash_pin
from terminal.store import Store


class FiscalCatalogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "terminal.sqlite3"

    def _paired_store(self, products, issuer=None):
        store = Store(self.path)
        private, public = generate_keypair()
        operators = [{"id": "op-1", "name": "Ana", "role": "cashier", "pin_hash": hash_pin("123987")}]
        now = int(time.time())
        claims = {"schema": 1, "tenant_id": str(uuid.uuid4()), "shop_id": str(uuid.uuid4()),
                  "terminal_id": str(uuid.uuid4()), "device_id": store.identity()["device_id"], "issued_at": now - 5,
                  "expires_at": now + 3600, "features": ["pdv"], "status": "active",
                  "operators_hash": payload_digest(operators)}
        lease = sign_lease(claims, private)
        store.set_pairing({"token": "t" * 48, "tenant_id": claims["tenant_id"], "shop_id": claims["shop_id"],
                           "terminal_id": claims["terminal_id"], "lease": lease, "public_key": public},
                          "http://127.0.0.1:8000")
        body = {"products": products, "operators": operators, "lease": lease, "public_key": public}
        if issuer:
            body["issuer"] = issuer
        store.apply_bootstrap(body)
        return store

    @staticmethod
    def _product(product_id, name, fiscal=None):
        row = {"id": product_id, "sku": name.upper(), "name": name, "unit": "UN", "category": "Mercearia",
               "price_cents": 500, "quantity_milli": 10000, "active": True}
        if fiscal is not None:
            row["fiscal"] = fiscal
        return row

    def test_resolved_fiscal_data_is_stored_and_unready_items_warn_without_blocking(self):
        ready = {"v": 1, "profile_id": "p1", "ready": True, "missing": [],
                 "fields": {"ncm": "10063021", "icms_csosn": "102", "cfop": "5102"}}
        pending = {"v": 1, "profile_id": "p1", "ready": False, "missing": ["Falta NCM: informe a classificação"],
                   "fields": {"icms_csosn": "102"}}
        store = self._paired_store([self._product("a", "Arroz", ready), self._product("b", "Bala", pending),
                                    self._product("c", "Caneta")],
                                   issuer={"legal_name": "Mercado", "crt": 1, "fiscal_ready": False})
        rows = {row["id"]: row for row in store.products()}
        self.assertEqual((rows["a"]["fiscal_ready"], rows["b"]["fiscal_ready"], rows["c"]["fiscal_ready"]), (1, 0, None))
        self.assertEqual(json.loads(rows["a"]["fiscal"])["fields"]["icms_csosn"], "102")
        operator = store.login("op-1", "123987")
        store.open_cash("op-1", 0)
        sale = store.sale(operator, {"request_id": "fiscal-1", "discount_cents": 0,
                                     "items": [{"product_id": pid, "quantity_milli": 1000} for pid in ("a", "b", "c")],
                                     "payments": [{"method": "cash", "amount_cents": 1500}]})
        self.assertEqual(sale["total_cents"], 1500)
        self.assertEqual(sale["fiscal_warnings"], [{"product_id": "b", "name": "Bala",
                                                    "missing": ["Falta NCM: informe a classificação"]}])
        # Advice only: the sale event and the stored receipt stay as they were.
        event = store.pending_events()[-1]
        self.assertNotIn("fiscal_warnings", json.dumps(event))
        self.assertNotIn("fiscal_warnings", store.sale_detail(sale["id"]))
        self.assertEqual(sale["issuer"]["crt"], 1)

    def test_malformed_fiscal_block_never_drops_the_product(self):
        store = self._paired_store([self._product("x", "Xarope", {"ready": "sim", "missing": "nada", "fields": []}),
                                    self._product("y", "Yogurte", "texto")])
        rows = {row["id"]: row for row in store.products()}
        self.assertEqual(set(rows), {"x", "y"})
        self.assertIsNone(rows["x"]["fiscal_ready"])
        self.assertEqual(json.loads(rows["x"]["fiscal"])["missing"], [])
        self.assertEqual(rows["y"]["fiscal"], "")

    def test_database_of_an_older_version_gains_the_fiscal_columns(self):
        conn = sqlite3.connect(self.path)
        conn.executescript("""
            CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE TABLE products(
                id TEXT PRIMARY KEY,sku TEXT,barcode TEXT UNIQUE,name TEXT NOT NULL,
                unit TEXT NOT NULL CHECK(unit IN ('UN','KG','L')),category TEXT,
                price_cents INTEGER NOT NULL CHECK(price_cents>=0),
                stock_milli INTEGER NOT NULL CHECK(stock_milli>=0),
                active INTEGER NOT NULL CHECK(active IN (0,1)),updated_at INTEGER NOT NULL);
            INSERT INTO products VALUES ('old','OLD',NULL,'Antigo','UN','Geral',100,0,1,0);
        """)
        conn.commit()
        conn.close()
        store = Store(self.path)
        with store.connection() as db:
            columns = {row["name"] for row in db.execute("PRAGMA table_info(products)")}
            row = db.execute("SELECT fiscal,fiscal_ready FROM products WHERE id='old'").fetchone()
        self.assertTrue({"fiscal", "fiscal_ready"} <= columns)
        self.assertEqual((row["fiscal"], row["fiscal_ready"]), ("", None))


if __name__ == "__main__":
    unittest.main()
