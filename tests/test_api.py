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
        event = {"event_id": "a" * 32, "type": "sale.completed", "aggregate_id": "sale-1",
                 "identity": {"tenant_id": a_tenant, "shop_id": a_shop, "terminal_id": terminal["id"]},
                 "data": {"total_cents": 990, "created_at": 123456789,
                          "items": [{"product_id": "p", "quantity": 1, "line_total_cents": 990}],
                          "payments": [{"method": "cash", "amount_cents": 990}]}}
        self.assertEqual(self.request("POST", "/api/sync/events", {"events": [event]})[0], 401)
        self.assertEqual(self.request("POST", "/api/sync/events", {"events": [event]}, bearer=token)[0], 200)
        self.assertEqual(self.request("POST", "/api/sync/events", {"events": [event]}, bearer=token)[0], 200)
        self.assertEqual(len(self.request("GET", "/api/sales", cookie=a_owner)[1]["sales"]), 1)
        self.assertEqual(self.request("GET", "/api/sales", cookie=b_owner)[1]["sales"], [])
        bad = {**event, "data": {**event["data"], "total_cents": 999}}
        self.assertEqual(self.request("POST", "/api/sync/events", {"events": [bad]}, bearer=token)[0], 409)
        foreign = {**event, "event_id": "b" * 32,
                   "identity": {"tenant_id": b_tenant, "shop_id": b_shop, "terminal_id": terminal["id"]}}
        self.assertEqual(self.request("POST", "/api/sync/events", {"events": [foreign]}, bearer=token)[0], 403)


if __name__ == "__main__":
    unittest.main()
