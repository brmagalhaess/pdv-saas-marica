"""Two caixas adicionais against the real terminal app, through the real LAN listener.

Needs the station model of the core (Store.authenticate_station and friends): until
that part is merged this module is skipped; it runs in the integration.
"""

import os
import tempfile
import time
import unittest
import uuid
from pathlib import Path

from shared.licensing import generate_keypair, payload_digest, sign_lease
from shared.security import hash_pin
from terminal import lan, station
from terminal.app import create_app
from terminal.store import Store

HOST = "127.0.0.1"
BASE = "http://127.0.0.1:8010"
ORIGIN = {"Origin": BASE}
LOCAL = {"Origin": BASE, "X-PDV-Station-Local": "1"}


@unittest.skipUnless(hasattr(Store, "authenticate_station"), "modelo de estações do núcleo ainda não integrado")
class TwoStationsTests(unittest.TestCase):
    def setUp(self):
        self._env = {key: os.environ.get(key) for key in ("PDV_LAN_PORT", "PDV_DISCOVERY_PORT", "PDV_LAN_HOST")}
        os.environ.update(PDV_LAN_PORT="0", PDV_DISCOVERY_PORT="0", PDV_LAN_HOST=HOST)
        self.addCleanup(self._restore_env)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.app = create_app({"TESTING": True, "DATA_DIR": self.temp.name, "START_WORKER": False})
        self.store = self.app.extensions["terminal_store"]
        self.tenant_id, self.shop_id, self.terminal_id = (str(uuid.uuid4()) for _ in range(3))
        self.cashier_id, self.second_cashier_id, self.manager_id = (str(uuid.uuid4()) for _ in range(3))
        private, public = generate_keypair()
        operators = [
            {"id": self.cashier_id, "name": "Ana", "role": "cashier", "pin_hash": hash_pin("123987")},
            {"id": self.second_cashier_id, "name": "Caio", "role": "cashier", "pin_hash": hash_pin("456123")},
            {"id": self.manager_id, "name": "Bruno", "role": "manager", "pin_hash": hash_pin("654321")},
        ]
        self.product = {"id": str(uuid.uuid4()), "sku": "AGUA500", "barcode": "7890000000019",
                        "name": "Água mineral 500 ml", "unit": "UN", "category": "Bebidas", "price_cents": 500,
                        "quantity_milli": 50000, "active": True}
        now = int(time.time())
        lease = sign_lease({"schema": 1, "tenant_id": self.tenant_id, "shop_id": self.shop_id,
                            "terminal_id": self.terminal_id, "device_id": self.store.identity()["device_id"],
                            "issued_at": now - 5, "expires_at": now + 3600, "features": ["pdv"], "status": "active",
                            "operators_hash": payload_digest(operators), "max_stations": 2, "hub": True}, private)
        self.store.set_pairing({"token": "t" * 48, "tenant_id": self.tenant_id, "shop_id": self.shop_id,
                                "terminal_id": self.terminal_id, "lease": lease, "public_key": public,
                                "capabilities": ["stations", "terminal-config", "long-lease"]},
                               "http://127.0.0.1:8000")
        self.store.apply_bootstrap({"products": [self.product], "operators": operators, "lease": lease,
                                    "public_key": public, "capabilities": ["stations", "terminal-config"]})
        self.assertGreater(self.store.station_limit()["limit"], 0)
        self.server = lan.start(self.app, host=HOST, port=0)
        self.addCleanup(lambda: self.app.extensions["terminal_lan"].stop())
        self.assertTrue(self.server.running, self.server.error)

    def _restore_env(self):
        for key, value in self._env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def add_station(self, name):
        """Links one more caixa with a real 6-digit code. Returns (browser, bridge, station id)."""
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        app = station.create_app({"TESTING": True, "DATA_DIR": temp.name, "DISCOVER": lambda shop_id=None: []})
        self.addCleanup(station.stop_background, app, 2.0)
        browser = app.test_client()
        code = self.store.create_station_code(self.manager_id, name)["code"]
        linked = browser.post("/station/api/link", json={
            "address": HOST, "port": self.server.port, "fingerprint": self.server.fingerprint, "code": code,
            "name": name}, base_url=BASE, headers=LOCAL)
        self.assertEqual(linked.status_code, 200, linked.json)
        self.assertFalse((Path(temp.name) / "terminal.sqlite3").exists(), "a station never has a database")
        return browser, app.extensions["station"], linked.json["station"]["id"]

    def csrf(self, browser):
        return browser.get("/api/session", base_url=BASE).json["csrf"]

    def post(self, browser, path, payload):
        return browser.post(path, json=payload, base_url=BASE,
                            headers={**ORIGIN, "X-CSRF-Token": self.csrf(browser)})

    def login(self, browser, operator_id, pin):
        response = self.post(browser, "/api/login", {"operator_id": operator_id, "pin": pin})
        self.assertEqual(response.status_code, 200, response.json)

    def sale_body(self, request_id, quantity_milli):
        return {"request_id": request_id, "items": [{"product_id": self.product["id"],
                                                     "quantity_milli": quantity_milli}],
                "discount_cents": 0, "customer_name": "", "customer_document": "", "customer_id": None,
                "seller_id": None, "manager_pin": "", "payments": [{"method": "cash", "amount_cents": 5000}]}

    def receipts(self, station_id):
        with self.store.station(station_id):
            return [job for job in self.store.print_jobs() if job["kind"] == "receipt"]

    def test_two_cashiers_sell_at_the_same_time_and_survive_a_pulled_cable(self):
        first, first_bridge, first_id = self.add_station("Caixa 2")
        second, _second_bridge, second_id = self.add_station("Caixa 3")
        self.assertNotEqual(first_id, second_id)
        for station_id in (first_id, second_id):
            with self.store.station(station_id):
                self.store.save_printer("receipt", "tcp", "127.0.0.1:9", "escpos80", self.manager_id)
        # Each caixa has its own operator session and its own open cash.
        self.login(first, self.cashier_id, "123987")
        self.login(second, self.second_cashier_id, "456123")
        for browser, station_id in ((first, first_id), (second, second_id)):
            session = browser.get("/api/session", base_url=BASE).json
            self.assertEqual((session["station"]["id"], session["station"]["is_server"]), (station_id, False))
            opened = self.post(browser, "/api/cash/open", {"opening_cents": 10000})
            self.assertEqual(opened.status_code, 200, opened.json)
        self.assertEqual(sorted(item["station_id"] for item in self.store.open_cash_sessions()),
                         sorted([first_id, second_id]))
        self.assertIsNone(self.store.cash_state(), "the caixa principal itself has no open cash")
        # Server-only routes stay closed for a station.
        refused = self.post(first, "/api/unpair", {})
        self.assertEqual((refused.status_code, refused.json["code"]), (403, "server_only"))
        # Both sell.
        sale_one = self.post(first, "/api/sales", self.sale_body("venda-caixa-2", 3000))
        sale_two = self.post(second, "/api/sales", self.sale_body("venda-caixa-3", 2000))
        self.assertEqual((sale_one.status_code, sale_two.status_code), (200, 200), (sale_one.json, sale_two.json))
        stock = self.store.products_by_ids([self.product["id"]])[0]
        if stock.get("stock_controlled"):
            self.assertEqual(stock["stock_milli"], 50000 - 3000 - 2000, "one stock for the whole shop")
        by_station = {row["id"]: row["station_id"] for row in self.store.sales()}
        self.assertEqual(by_station[sale_one.json["id"]], first_id)
        self.assertEqual(by_station[sale_two.json["id"]], second_id)
        self.assertEqual((len(self.receipts(first_id)), len(self.receipts(second_id))), (1, 1),
                         "each receipt waits in the queue of the caixa that sold")
        with self.store.station("server"):
            self.assertEqual(self.store.print_jobs(), [])
        # Cable pulled in the middle of a sale: the station stops selling...
        port = self.server.port
        self.server.stop()
        body = self.sale_body("venda-com-cabo-puxado", 1000)
        csrf_headers = {**ORIGIN, "X-CSRF-Token": "sem-servidor"}
        lost = first.post("/api/sales", json=body, base_url=BASE, headers=csrf_headers)
        self.assertEqual((lost.status_code, lost.json["code"]), (503, "server_unreachable"))
        self.assertEqual(first.get("/health", base_url=BASE).status_code, 200)
        self.assertEqual(len(self.store.sales()), 2)
        # ...and the same request_id repeated after the server is back sells exactly once.
        self.server = lan.start(self.app, host=HOST, port=port)
        self.assertTrue(self.server.running, self.server.error)
        again = self.post(first, "/api/sales", body)
        self.assertEqual(again.status_code, 200, again.json)
        repeated = self.post(first, "/api/sales", body)
        self.assertEqual((repeated.status_code, repeated.json["id"]), (200, again.json["id"]))
        self.assertEqual(len(self.store.sales()), 3, "no duplicate sale")
        self.assertEqual(len(self.receipts(first_id)), 2)
        self.assertEqual(len(self.receipts(second_id)), 1)
        self.assertEqual(first_bridge.check_once(), "up")


if __name__ == "__main__":
    unittest.main()
