"""Integração HTTP real via socketpair, sem porta TCP no ambiente de testes."""

import io
import json
import socket
import tempfile
import threading
import unittest
from http.client import HTTPConnection
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request

import app
from local_pdv import LocalPDV, PDVError
from local_pdv.sync import sync_once


class QuietAPI(app.API):
    def log_message(self, *_args):
        pass


class MemoryResponse:
    def __init__(self, body, headers):
        self.body = io.BytesIO(body)
        self.headers = headers

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.body.close()

    def read(self):
        return self.body.read()


def socketpair_transport(request, timeout=5):
    """Envia bytes HTTP/1.1 ao handler real usando um par de sockets locais."""
    server_socket, client_socket = socket.socketpair()
    errors = []

    def serve_one():
        try:
            QuietAPI(server_socket, ("127.0.0.1", 0), SimpleNamespace())
        except Exception as exc:
            errors.append(exc)
        finally:
            server_socket.close()

    thread = threading.Thread(target=serve_one, daemon=True)
    thread.start()
    connection = HTTPConnection("127.0.0.1", timeout=timeout)
    connection.sock = client_socket
    parsed = urlparse(request.full_url)
    target = (parsed.path or "/") + (f"?{parsed.query}" if parsed.query else "")
    try:
        connection.request(request.get_method(), target, body=request.data,
                           headers=dict(request.header_items()))
        response = connection.getresponse()
        body = response.read()
        status, reason, headers = response.status, response.reason, response.headers
    finally:
        connection.close()
        thread.join(timeout=timeout)
    if errors:
        raise errors[0]
    if status >= 400:
        raise HTTPError(request.full_url, status, reason, headers, io.BytesIO(body))
    return MemoryResponse(body, headers)


class SyncHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old_db_path = app.DB_PATH
        app.DB_PATH = Path(self.temp.name) / "saas.sqlite3"
        app.init_db()
        self.base = "http://127.0.0.1:8000"

    def tearDown(self):
        app.DB_PATH = self.old_db_path
        self.temp.cleanup()

    def request(self, method, path, payload=None, cookie=None):
        headers = {"Content-Type": "application/json"}
        if cookie:
            headers["Cookie"] = cookie
        data = json.dumps(payload).encode() if payload is not None else None
        request = Request(self.base + path, data=data, headers=headers, method=method)
        with socketpair_transport(request) as response:
            return json.loads(response.read()), response.headers

    def test_sale_reaches_server_once_after_lost_response_and_restart(self):
        registered, _ = self.request("POST", "/api/register", {
            "company": "Mercearia teste", "email": "dono@example.test", "password": "Senha-demo-42!"})
        _, headers = self.request("POST", "/api/login", {
            "email": "dono@example.test", "password": "Senha-demo-42!"})
        cookie = headers["Set-Cookie"].split(";", 1)[0]
        me, _ = self.request("GET", "/api/me", cookie=cookie)
        tenant_id = me["tenant"]["id"]
        shop_id = registered["shop_id"]
        product, _ = self.request("POST", "/api/products", {
            "name": "Arroz 1 kg", "sku": "ARROZ-1", "barcode": "7890000000001",
            "price_cents": 699, "unit": "UN"}, cookie=cookie)
        product_id = product["product"]["id"]
        self.request("POST", "/api/stock/movements", {
            "shop_id": shop_id, "product_id": product_id, "delta_milli": 3000,
            "reason": "Carga inicial", "idempotency_key": "carga-arroz"}, cookie=cookie)
        terminal, _ = self.request("POST", "/api/terminals", {
            "shop_id": shop_id, "name": "Caixa 1"}, cookie=cookie)

        path = Path(self.temp.name) / "caixa.sqlite3"
        pdv = LocalPDV(path, tenant_id, shop_id, terminal["id"])
        pdv.upsert_product(product_id, "Arroz 1 kg", 699, 3, "7890000000001")
        pdv.open_cash(10000)
        sale = pdv.complete_sale("venda-http-1", [{"product_id": product_id, "quantity": 1}],
                                 [{"method": "cash", "amount_cents": 700}])
        self.assertEqual(sale["change_cents"], 1)
        self.assertEqual(pdv.status()["pending_events"], 2)
        sale_event_id = next(e["event_id"] for e in pdv.pending_events() if e["type"] == "sale.completed")

        with self.assertRaisesRegex(PDVError, "HTTP 401"):
            sync_once(pdv, self.base, "x" * 40, socketpair_transport)
        self.assertEqual(pdv.status()["pending_events"], 2)

        def lose_response(request, timeout):
            with socketpair_transport(request, timeout=timeout) as response:
                response.read()
            raise URLError("resposta perdida depois de gravar no servidor")

        with self.assertRaisesRegex(PDVError, "pendentes"):
            sync_once(pdv, self.base, terminal["token"], lose_response)
        self.assertEqual(pdv.status()["pending_events"], 2)
        sales, _ = self.request("GET", "/api/sales", cookie=cookie)
        self.assertEqual(len(sales["sales"]), 1)

        restarted = LocalPDV(path)
        self.assertEqual(sync_once(restarted, self.base, terminal["token"], socketpair_transport),
                         {"sent": 2, "remaining": 0})
        self.assertEqual(sync_once(restarted, self.base, terminal["token"], socketpair_transport),
                         {"sent": 0, "remaining": 0})
        sales, _ = self.request("GET", "/api/sales", cookie=cookie)
        self.assertEqual(len(sales["sales"]), 1)
        self.assertEqual(sales["sales"][0]["id"], sale["id"])
        self.assertEqual(sales["sales"][0]["stock_status"], "ok")
        detail, _ = self.request("GET", f"/api/sales/{sale['id']}", cookie=cookie)
        self.assertEqual(detail["sale"]["items"][0]["product_id"], product_id)
        self.assertEqual(detail["sale"]["items"][0]["quantity"], 1)
        self.assertEqual(detail["sale"]["payments"], [{"method": "cash", "amount_cents": 700}])
        with app.db() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM sync_events WHERE event_id=?",
                                          (sale_event_id,)).fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM sale_stock_movements WHERE tenant_id=? AND sale_id=?",
                                          (tenant_id, sale["id"])).fetchone()[0], 1)
        stock, _ = self.request("GET", f"/api/stock?shop_id={shop_id}", cookie=cookie)
        self.assertEqual(stock["stock"][0]["quantity_milli"], 2000)


if __name__ == "__main__":
    unittest.main()
