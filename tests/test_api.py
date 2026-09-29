import io
import json
import tempfile
import unittest
from email.message import Message
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app


class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        app.DB_PATH = Path(cls.tmp.name) / "test.sqlite3"
        app.init_db()

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def request(self, method, path, payload=None, cookie=None, bearer=None):
        body = json.dumps(payload).encode() if payload is not None else b""
        headers = Message()
        headers["Content-Type"] = "application/json"
        headers["Content-Length"] = str(len(body))
        headers["Host"] = "localhost"
        if cookie:
            headers["Cookie"] = cookie
        if bearer:
            headers["Authorization"] = f"Bearer {bearer}"
        handler = app.API.__new__(app.API)
        handler.path = path
        handler.headers = headers
        handler.rfile = io.BytesIO(body)
        handler.wfile = io.BytesIO()
        response = {"status": None, "headers": {}}
        handler.send_response = lambda status: response.update(status=status)
        handler.send_header = lambda name, value: response["headers"].update({name: value})
        handler.end_headers = lambda: None
        getattr(handler, f"do_{method}")()
        return response["status"], json.loads(handler.wfile.getvalue()), response["headers"].get("Set-Cookie")

    def test_registration_auth_and_tenant_isolation(self):
        for label in ("A", "B"):
            status, _, _ = self.request("POST", "/api/register", {"company": label, "email": f"{label.lower()}@example.com", "password": "Strong-pass-123!"})
            self.assertEqual(status, 201)
        status, _, a_cookie = self.request("POST", "/api/login", {"email": "a@example.com", "password": "Strong-pass-123!"})
        self.assertEqual(status, 200)
        status, _, b_cookie = self.request("POST", "/api/login", {"email": "b@example.com", "password": "Strong-pass-123!"})
        self.assertEqual(status, 200)
        a_cookie = a_cookie.split(";", 1)[0]
        b_cookie = b_cookie.split(";", 1)[0]
        status, _, _ = self.request("POST", "/api/shops", {"name": "Loja A2"}, a_cookie)
        self.assertEqual(status, 201)
        self.assertEqual(len(self.request("GET", "/api/shops", cookie=a_cookie)[1]["shops"]), 2)
        b_shops = self.request("GET", "/api/shops", cookie=b_cookie)[1]["shops"]
        self.assertEqual(len(b_shops), 1)
        self.assertNotIn("Loja A2", [s["name"] for s in b_shops])
        self.assertEqual(self.request("GET", "/api/users", cookie=b_cookie)[1]["users"][0]["email"], "b@example.com")
        status, _, _ = self.request("POST", "/api/users", {"email": "cashier@example.com", "password": "Cashier-pass-123!", "role": "cashier"}, a_cookie)
        self.assertEqual(status, 201)
        status, _, cashier_cookie = self.request("POST", "/api/login", {"email": "cashier@example.com", "password": "Cashier-pass-123!"})
        self.assertEqual(status, 200)
        cashier_cookie = cashier_cookie.split(";", 1)[0]
        self.assertEqual(self.request("GET", "/api/users", cookie=cashier_cookie)[0], 403)
        self.assertEqual(self.request("POST", "/api/shops", {"name": "Proibida"}, cashier_cookie)[0], 403)

    def test_auth_required_and_weak_password_rejected(self):
        self.assertEqual(self.request("GET", "/api/shops")[0], 401)
        self.assertEqual(self.request("POST", "/api/register", {"company": "C", "email": "c@example.com", "password": "short"})[0], 400)

    def register_and_login(self, label):
        email = f"{label}@example.com"
        password = "Test-pass-42!"
        status, registered, _ = self.request("POST", "/api/register", {"company": label, "email": email, "password": password})
        self.assertEqual(status, 201)
        status, _, cookie = self.request("POST", "/api/login", {"email": email, "password": password})
        self.assertEqual(status, 200)
        return registered["shop_id"], cookie.split(";", 1)[0]

    def sale_event(self, event_id, sale_id, tenant_id, shop_id, terminal_id, product_id, number):
        return {"event_id": event_id, "type": "sale.completed", "aggregate_id": sale_id,
                "identity": {"tenant_id": tenant_id, "shop_id": shop_id, "terminal_id": terminal_id},
                "data": {"request_id": f"request-{number}", "cash_session_id": "session-1",
                         "total_cents": 1200, "change_cents": 300, "created_at": 1720000000000 + number,
                         "items": [{"product_id": product_id, "name": "Café", "quantity": 1,
                                    "unit_price_cents": 1200, "line_total_cents": 1200}],
                         "payments": [{"method": "cash", "amount_cents": 1500}]}}

    def test_password_policy_accepts_shorter_strong_password(self):
        self.assertTrue(app.valid_password("Qa123456!"))
        for password in ("alllower123!", "ALLUPPER123!", "NoNumbers!", "NoSymbol123", "Aa1!"):
            self.assertFalse(app.valid_password(password), password)
        status, _, _ = self.request("POST", "/api/register", {
            "company": "Senha", "email": "senha@example.com", "password": "Qa123456!"})
        self.assertEqual(status, 201)

    def test_product_catalog_and_stock_are_tenant_scoped(self):
        a_shop, a_cookie = self.register_and_login("catalog_a")
        b_shop, b_cookie = self.register_and_login("catalog_b")
        item = {"name": "Arroz integral", "sku": "ARROZ-1", "barcode": "7891234567890",
                "unit": "KG", "price_cents": 1290, "cost_cents": 800}
        status, created, _ = self.request("POST", "/api/products", item, a_cookie)
        self.assertEqual(status, 201)
        product_id = created["product"]["id"]
        self.assertEqual(self.request("POST", "/api/products", item, a_cookie)[0], 409)
        self.assertEqual(self.request("GET", "/api/products", cookie=b_cookie)[1]["products"], [])
        self.assertEqual(self.request("GET", f"/api/products/{product_id}", cookie=b_cookie)[0], 404)
        self.assertEqual(self.request("PATCH", f"/api/products/{product_id}", {"price_cents": 999}, b_cookie)[0], 404)
        self.assertEqual(self.request("DELETE", f"/api/products/{product_id}", cookie=b_cookie)[0], 404)
        self.assertEqual(self.request("POST", "/api/stock/movements", {
            "shop_id": b_shop, "product_id": product_id, "delta_milli": 1000, "reason": "Compra"}, b_cookie)[0], 404)
        movement = {"shop_id": a_shop, "product_id": product_id, "delta_milli": 2500,
                    "reason": "Entrada de mercadoria", "idempotency_key": "purchase-1"}
        status, first, _ = self.request("POST", "/api/stock/movements", movement, a_cookie)
        self.assertEqual(status, 201)
        self.assertEqual(first["quantity_milli"], 2500)
        status, repeated, _ = self.request("POST", "/api/stock/movements", movement, a_cookie)
        self.assertEqual(status, 200)
        self.assertTrue(repeated["replayed"])
        self.assertEqual(repeated["movement"]["id"], first["movement"]["id"])
        self.assertEqual(self.request("POST", "/api/stock/movements", {
            **movement, "delta_milli": 1000}, a_cookie)[0], 409)
        self.assertEqual(self.request("POST", "/api/stock/movements", {
            "shop_id": a_shop, "product_id": product_id, "delta_milli": -3000,
            "reason": "Ajuste inventário"}, a_cookie)[0], 409)
        stock = self.request("GET", f"/api/stock?shop_id={a_shop}", cookie=a_cookie)[1]["stock"]
        self.assertEqual(stock[0]["quantity_milli"], 2500)
        self.assertEqual(self.request("GET", f"/api/stock?shop_id={a_shop}", cookie=b_cookie)[0], 404)
        self.assertEqual(self.request("GET", f"/api/stock/movements?shop_id={b_shop}", cookie=b_cookie)[1]["movements"], [])
        self.assertEqual(self.request("PATCH", f"/api/products/{product_id}", {"price_cents": 1390}, a_cookie)[1]["product"]["price_cents"], 1390)
        self.assertEqual(self.request("DELETE", f"/api/products/{product_id}", cookie=a_cookie)[0], 200)
        self.assertEqual(self.request("GET", "/api/products", cookie=a_cookie)[1]["products"], [])
        self.assertEqual(len(self.request("GET", "/api/products?include_inactive=1", cookie=a_cookie)[1]["products"]), 1)

    def test_cashier_cannot_mutate_catalog_or_stock(self):
        shop_id, owner = self.register_and_login("catalog_roles")
        item = {"name": "Feijão", "sku": "FEI-1", "price_cents": 990}
        status, result, _ = self.request("POST", "/api/products", item, owner)
        self.assertEqual(status, 201)
        product_id = result["product"]["id"]
        status, _, _ = self.request("POST", "/api/users", {
            "email": "catalog_cashier@example.com", "password": "Cashier-pass-7!", "role": "cashier"}, owner)
        self.assertEqual(status, 201)
        status, _, cookie = self.request("POST", "/api/login", {
            "email": "catalog_cashier@example.com", "password": "Cashier-pass-7!"})
        self.assertEqual(status, 200)
        cashier = cookie.split(";", 1)[0]
        status, listing, _ = self.request("GET", "/api/products", cookie=cashier)
        self.assertEqual(status, 200)
        self.assertNotIn("cost_cents", listing["products"][0])
        self.assertEqual(self.request("POST", "/api/products", item, cashier)[0], 403)
        self.assertEqual(self.request("PATCH", f"/api/products/{product_id}", {"price_cents": 1}, cashier)[0], 403)
        self.assertEqual(self.request("DELETE", f"/api/products/{product_id}", cookie=cashier)[0], 403)
        self.assertEqual(self.request("POST", "/api/stock/movements", {
            "shop_id": shop_id, "product_id": product_id, "delta_milli": 1000,
            "reason": "Tentativa"}, cashier)[0], 403)

    def test_terminal_sync_is_idempotent_and_tenant_scoped(self):
        a_shop, a_owner = self.register_and_login("sync_a")
        b_shop, b_owner = self.register_and_login("sync_b")
        a_tenant = self.request("GET", "/api/me", cookie=a_owner)[1]["tenant"]["id"]
        b_tenant = self.request("GET", "/api/me", cookie=b_owner)[1]["tenant"]["id"]
        status, terminal, _ = self.request("POST", "/api/terminals", {"shop_id": a_shop, "name": "Caixa 1"}, a_owner)
        self.assertEqual(status, 201)
        self.assertEqual(self.request("POST", "/api/terminals", {"shop_id": a_shop, "name": "Caixa 2"}, a_owner)[0], 409)
        token = terminal["token"]
        product = self.request("POST", "/api/products", {
            "name": "Arroz", "sku": "SYNC-ARROZ", "price_cents": 990}, a_owner)[1]["product"]
        self.assertEqual(self.request("POST", "/api/stock/movements", {
            "shop_id": a_shop, "product_id": product["id"], "delta_milli": 2000,
            "reason": "Saldo inicial"}, a_owner)[0], 201)
        event = {"event_id": "a" * 32, "type": "sale.completed", "aggregate_id": "sale-1",
                 "identity": {"tenant_id": a_tenant, "shop_id": a_shop, "terminal_id": terminal["id"]},
                 "data": {"request_id": "req-1", "cash_session_id": "cash-1",
                          "total_cents": 990, "change_cents": 0, "created_at": 123456789,
                          "items": [{"product_id": product["id"], "name": "Arroz", "quantity": 1,
                                     "unit_price_cents": 990, "line_total_cents": 990}],
                          "payments": [{"method": "cash", "amount_cents": 990}]}}
        self.assertEqual(self.request("POST", "/api/sync/events", {"events": [event]})[0], 401)
        self.assertEqual(self.request("POST", "/api/sync/events", {"events": [event]}, bearer=token)[0], 200)
        self.assertEqual(self.request("POST", "/api/sync/events", {"events": [event]}, bearer=token)[0], 200)
        self.assertEqual(len(self.request("GET", "/api/sales", cookie=a_owner)[1]["sales"]), 1)
        self.assertEqual(self.request("GET", "/api/sales", cookie=b_owner)[1]["sales"], [])
        self.assertEqual(self.request("GET", f"/api/stock?shop_id={a_shop}", cookie=a_owner)[1]["stock"][0]["quantity_milli"], 1000)
        bad = {**event, "data": {**event["data"], "request_id": "other-request"}}
        self.assertEqual(self.request("POST", "/api/sync/events", {"events": [bad]}, bearer=token)[0], 409)
        foreign = {**event, "event_id": "b" * 32,
                   "identity": {"tenant_id": b_tenant, "shop_id": b_shop, "terminal_id": terminal["id"]}}
        self.assertEqual(self.request("POST", "/api/sync/events", {"events": [foreign]}, bearer=token)[0], 403)

    def test_sales_batch_rolls_back_and_replay_does_not_debit_twice(self):
        shop_id, owner = self.register_and_login("sale_atomic")
        tenant_id = self.request("GET", "/api/me", cookie=owner)[1]["tenant"]["id"]
        terminal = self.request("POST", "/api/terminals", {"shop_id": shop_id, "name": "Caixa"}, owner)[1]
        product_id = self.request("POST", "/api/products", {
            "name": "Café", "sku": "ATOMIC-CAFE", "price_cents": 1200}, owner)[1]["product"]["id"]
        self.request("POST", "/api/stock/movements", {"shop_id": shop_id, "product_id": product_id,
                     "delta_milli": 4000, "reason": "Saldo inicial"}, owner)
        valid = self.sale_event("1" * 32, "sale-atomic-1", tenant_id, shop_id, terminal["id"], product_id, 1)
        invalid = self.sale_event("2" * 32, "sale-atomic-2", tenant_id, shop_id, terminal["id"], "foreign-product", 2)
        status, _, _ = self.request("POST", "/api/sync/events", {"events": [valid, invalid]}, bearer=terminal["token"])
        self.assertEqual(status, 400)
        self.assertEqual(self.request("GET", "/api/sales", cookie=owner)[1]["sales"], [])
        self.assertEqual(self.request("GET", f"/api/stock?shop_id={shop_id}", cookie=owner)[1]["stock"][0]["quantity_milli"], 4000)
        status, accepted, _ = self.request("POST", "/api/sync/events", {"events": [valid]}, bearer=terminal["token"])
        self.assertEqual(status, 200)
        self.assertEqual(accepted["accepted_event_ids"], [valid["event_id"]])
        replay = {**valid, "event_id": "3" * 32}
        self.assertEqual(self.request("POST", "/api/sync/events", {"events": [replay]}, bearer=terminal["token"])[0], 200)
        self.assertEqual(len(self.request("GET", "/api/sales", cookie=owner)[1]["sales"]), 1)
        self.assertEqual(self.request("GET", f"/api/stock?shop_id={shop_id}", cookie=owner)[1]["stock"][0]["quantity_milli"], 3000)
        with app.db() as conn:
            count = conn.execute("SELECT COUNT(*) FROM sale_stock_movements WHERE tenant_id=?",
                                 (tenant_id,)).fetchone()[0]
        self.assertEqual(count, 1)

    def test_sales_pagination_detail_and_terminal_adjustments(self):
        shop_id, owner = self.register_and_login("sale_page")
        tenant_id = self.request("GET", "/api/me", cookie=owner)[1]["tenant"]["id"]
        terminal = self.request("POST", "/api/terminals", {"shop_id": shop_id, "name": "Caixa"}, owner)[1]
        product_id = self.request("POST", "/api/products", {
            "name": "Café", "sku": "PAGE-CAFE", "price_cents": 1200}, owner)[1]["product"]["id"]
        self.request("POST", "/api/stock/movements", {"shop_id": shop_id, "product_id": product_id,
                     "delta_milli": 3000, "reason": "Saldo inicial"}, owner)
        events = [self.sale_event(f"{i:032x}", f"sale-page-{i}", tenant_id, shop_id,
                                  terminal["id"], product_id, i) for i in (4, 5, 6)]
        self.assertEqual(self.request("POST", "/api/sync/events", {"events": events}, bearer=terminal["token"])[0], 200)
        first = self.request("GET", "/api/sales?limit=2", cookie=owner)[1]
        self.assertEqual(len(first["sales"]), 2)
        self.assertIsNotNone(first["next_cursor"])
        second = self.request("GET", f"/api/sales?limit=2&cursor={first['next_cursor']}", cookie=owner)[1]
        self.assertEqual(len(second["sales"]), 1)
        self.assertIsNone(second["next_cursor"])
        ids = {sale["id"] for sale in first["sales"] + second["sales"]}
        self.assertEqual(ids, {"sale-page-4", "sale-page-5", "sale-page-6"})
        detail = self.request("GET", "/api/sales/sale-page-4", cookie=owner)[1]["sale"]
        self.assertEqual(detail["items"][0]["line_total_cents"], 1200)
        self.assertEqual(detail["payments"][0]["amount_cents"], 1500)
        self.assertEqual(self.request("GET", f"/api/stock?shop_id={shop_id}", cookie=owner)[1]["stock"][0]["quantity_milli"], 0)
        extra = self.sale_event("7" * 32, "sale-page-7", tenant_id, shop_id, terminal["id"], product_id, 7)
        self.assertEqual(self.request("POST", "/api/sync/events", {"events": [extra]}, bearer=terminal["token"])[0], 200)
        self.assertEqual(self.request("GET", "/api/sales/sale-page-7", cookie=owner)[1]["sale"]["stock_status"], "negative")
        self.assertEqual(self.request("GET", f"/api/stock?shop_id={shop_id}", cookie=owner)[1]["stock"][0]["quantity_milli"], -1000)
        adjustment = {"event_id": "f" * 32, "type": "stock.adjusted", "aggregate_id": "adjust-page-1",
                      "identity": {"tenant_id": tenant_id, "shop_id": shop_id, "terminal_id": terminal["id"]},
                      "data": {"product_id": product_id, "delta": 2, "reason": "Recebimento",
                               "created_at": 1720000000100}}
        self.assertEqual(self.request("POST", "/api/sync/events", {"events": [adjustment]}, bearer=terminal["token"])[0], 200)
        self.assertEqual(self.request("POST", "/api/sync/events", {"events": [adjustment]}, bearer=terminal["token"])[0], 200)
        self.assertEqual(self.request("GET", f"/api/stock?shop_id={shop_id}", cookie=owner)[1]["stock"][0]["quantity_milli"], 1000)
        movements = self.request("GET", f"/api/stock/movements?shop_id={shop_id}", cookie=owner)[1]["movements"]
        self.assertEqual({row["source"] for row in movements}, {"manual", "terminal_adjustment", "sale"})


if __name__ == "__main__":
    unittest.main()
