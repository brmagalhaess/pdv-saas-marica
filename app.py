"""Minimal executable SaaS foundation for the PDV pilot (Python stdlib only)."""
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import time
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("PDV_DB", str(ROOT / "pdv.sqlite3")))
SESSION_SECONDS = 60 * 60 * 8


def db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with db() as conn:
        conn.executescript("""
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS plans (
          id TEXT PRIMARY KEY, name TEXT NOT NULL, price_cents INTEGER NOT NULL,
          terminal_limit INTEGER NOT NULL, active INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS tenants (
          id TEXT PRIMARY KEY, name TEXT NOT NULL, created_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS users (
          id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL REFERENCES tenants(id),
          email TEXT NOT NULL UNIQUE, password_hash TEXT NOT NULL,
          role TEXT NOT NULL CHECK(role IN ('owner','manager','cashier')),
          active INTEGER NOT NULL DEFAULT 1, created_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS shops (
          id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL REFERENCES tenants(id),
          name TEXT NOT NULL, created_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS subscriptions (
          tenant_id TEXT PRIMARY KEY REFERENCES tenants(id),
          plan_id TEXT NOT NULL REFERENCES plans(id),
          status TEXT NOT NULL CHECK(status IN ('trial','active','past_due','suspended','cancelled')),
          created_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS sessions (
          token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id),
          expires_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS audit_events (
          id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL REFERENCES tenants(id),
          user_id TEXT NOT NULL REFERENCES users(id),
          action TEXT NOT NULL, subject_id TEXT, created_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS terminals (
          id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL REFERENCES tenants(id),
          shop_id TEXT NOT NULL, name TEXT NOT NULL,
          token_hash TEXT NOT NULL UNIQUE, active INTEGER NOT NULL DEFAULT 1,
          created_at INTEGER NOT NULL,
          FOREIGN KEY(tenant_id, shop_id) REFERENCES shops(tenant_id, id)
        );
        CREATE TABLE IF NOT EXISTS sync_events (
          event_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL REFERENCES tenants(id),
          shop_id TEXT NOT NULL, terminal_id TEXT NOT NULL REFERENCES terminals(id),
          event_type TEXT NOT NULL, aggregate_id TEXT NOT NULL,
          payload TEXT NOT NULL, payload_hash TEXT NOT NULL,
          received_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS products (
          id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL REFERENCES tenants(id),
          sku TEXT NOT NULL, barcode TEXT, name TEXT NOT NULL,
          unit TEXT NOT NULL CHECK(unit IN ('UN','KG','L')),
          price_cents INTEGER NOT NULL CHECK(price_cents >= 0),
          cost_cents INTEGER NOT NULL DEFAULT 0 CHECK(cost_cents >= 0),
          active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
          created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
          UNIQUE(tenant_id, id), UNIQUE(tenant_id, sku), UNIQUE(tenant_id, barcode)
        );
        CREATE TABLE IF NOT EXISTS stock_movements (
          id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL REFERENCES tenants(id),
          shop_id TEXT NOT NULL, product_id TEXT NOT NULL,
          delta_milli INTEGER NOT NULL CHECK(delta_milli != 0),
          reason TEXT NOT NULL, reference TEXT,
          idempotency_key TEXT, created_by TEXT NOT NULL REFERENCES users(id),
          created_at INTEGER NOT NULL,
          FOREIGN KEY(tenant_id, shop_id) REFERENCES shops(tenant_id, id),
          FOREIGN KEY(tenant_id, product_id) REFERENCES products(tenant_id, id),
          UNIQUE(tenant_id, idempotency_key)
        );
        CREATE TABLE IF NOT EXISTS sales (
          tenant_id TEXT NOT NULL REFERENCES tenants(id), id TEXT NOT NULL,
          shop_id TEXT NOT NULL, terminal_id TEXT NOT NULL,
          event_id TEXT NOT NULL UNIQUE, request_id TEXT NOT NULL,
          cash_session_id TEXT NOT NULL, total_cents INTEGER NOT NULL,
          change_cents INTEGER NOT NULL, created_at INTEGER NOT NULL,
          received_at INTEGER NOT NULL, content_hash TEXT NOT NULL,
          stock_status TEXT NOT NULL CHECK(stock_status IN ('ok','negative')),
          PRIMARY KEY(tenant_id, id),
          UNIQUE(tenant_id, terminal_id, request_id),
          FOREIGN KEY(tenant_id, shop_id) REFERENCES shops(tenant_id, id),
          FOREIGN KEY(tenant_id, terminal_id) REFERENCES terminals(tenant_id, id)
        );
        CREATE TABLE IF NOT EXISTS sale_items (
          tenant_id TEXT NOT NULL, sale_id TEXT NOT NULL,
          line_number INTEGER NOT NULL, product_id TEXT NOT NULL,
          name TEXT NOT NULL, quantity INTEGER NOT NULL CHECK(quantity > 0),
          unit_price_cents INTEGER NOT NULL CHECK(unit_price_cents >= 0),
          line_total_cents INTEGER NOT NULL CHECK(line_total_cents >= 0),
          PRIMARY KEY(tenant_id, sale_id, line_number),
          FOREIGN KEY(tenant_id, sale_id) REFERENCES sales(tenant_id, id)
        );
        CREATE TABLE IF NOT EXISTS sale_payments (
          tenant_id TEXT NOT NULL, sale_id TEXT NOT NULL,
          line_number INTEGER NOT NULL, method TEXT NOT NULL,
          amount_cents INTEGER NOT NULL CHECK(amount_cents > 0),
          PRIMARY KEY(tenant_id, sale_id, line_number),
          FOREIGN KEY(tenant_id, sale_id) REFERENCES sales(tenant_id, id)
        );
        CREATE TABLE IF NOT EXISTS sale_stock_movements (
          tenant_id TEXT NOT NULL, sale_id TEXT NOT NULL,
          shop_id TEXT NOT NULL, product_id TEXT NOT NULL,
          delta_milli INTEGER NOT NULL CHECK(delta_milli < 0),
          created_at INTEGER NOT NULL,
          PRIMARY KEY(tenant_id, sale_id, product_id),
          FOREIGN KEY(tenant_id, sale_id) REFERENCES sales(tenant_id, id)
        );
        CREATE TABLE IF NOT EXISTS terminal_stock_movements (
          tenant_id TEXT NOT NULL, adjustment_id TEXT NOT NULL,
          event_id TEXT NOT NULL UNIQUE, shop_id TEXT NOT NULL,
          terminal_id TEXT NOT NULL, product_id TEXT NOT NULL,
          delta_milli INTEGER NOT NULL CHECK(delta_milli != 0),
          reason TEXT NOT NULL, created_at INTEGER NOT NULL,
          content_hash TEXT NOT NULL,
          PRIMARY KEY(tenant_id, terminal_id, adjustment_id)
        );
        CREATE INDEX IF NOT EXISTS idx_users_tenant ON users(tenant_id);
        CREATE INDEX IF NOT EXISTS idx_shops_tenant ON shops(tenant_id);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_shops_tenant_id ON shops(tenant_id, id);
        CREATE INDEX IF NOT EXISTS idx_audit_tenant ON audit_events(tenant_id, created_at);
        CREATE INDEX IF NOT EXISTS idx_products_tenant ON products(tenant_id, name);
        CREATE INDEX IF NOT EXISTS idx_stock_tenant_shop_product ON stock_movements(tenant_id, shop_id, product_id);
        CREATE INDEX IF NOT EXISTS idx_terminals_tenant ON terminals(tenant_id, shop_id);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_terminals_tenant_id ON terminals(tenant_id, id);
        CREATE INDEX IF NOT EXISTS idx_sync_events_tenant ON sync_events(tenant_id, received_at);
        CREATE INDEX IF NOT EXISTS idx_sales_page ON sales(tenant_id, received_at DESC, id DESC);
        CREATE INDEX IF NOT EXISTS idx_sale_stock_shop ON sale_stock_movements(tenant_id, shop_id, product_id);
        CREATE INDEX IF NOT EXISTS idx_terminal_stock_shop ON terminal_stock_movements(tenant_id, shop_id, product_id);
        """)
        conn.executemany("INSERT OR IGNORE INTO plans VALUES (?,?,?,?,1)", [
            ("essential", "Essencial", 14900, 1),
            ("management", "Gestão", 22900, 1),
            ("advanced", "Avançado", 34900, 1),
        ])


def hash_password(password):
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 310000)
    return f"pbkdf2_sha256$310000${salt.hex()}${digest.hex()}"


def verify_password(password, encoded):
    try:
        algorithm, rounds, salt, expected = encoded.split("$")
        if algorithm != "pbkdf2_sha256":
            return False
        actual = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), int(rounds))
        return hmac.compare_digest(actual, bytes.fromhex(expected))
    except (ValueError, TypeError):
        return False


def new_id():
    return secrets.token_hex(16)


def valid_password(password):
    """A modest complexity rule for new credentials; existing hashes remain valid."""
    return (8 <= len(password) <= 128 and
            bool(re.search(r"[A-Z]", password)) and
            bool(re.search(r"[a-z]", password)) and
            bool(re.search(r"[0-9]", password)) and
            bool(re.search(r"[^A-Za-z0-9]", password)))


def product_payload(data, existing=None):
    """Validate a complete product after applying a create or partial update."""
    allowed = {"name", "sku", "barcode", "unit", "price_cents", "cost_cents", "active"}
    if not data or set(data) - allowed:
        raise ValueError("Campos de produto inválidos")
    result = dict(existing) if existing is not None else {
        "name": "", "sku": "", "barcode": None, "unit": "UN",
        "price_cents": None, "cost_cents": 0, "active": 1,
    }
    result.update(data)
    for key, maximum in (("name", 160), ("sku", 60)):
        if not isinstance(result[key], str):
            raise ValueError(f"{key} inválido")
        result[key] = result[key].strip()
        if not result[key] or len(result[key]) > maximum:
            raise ValueError(f"{key} inválido")
    barcode = result["barcode"]
    if barcode is not None:
        if not isinstance(barcode, str):
            raise ValueError("Código de barras inválido")
        barcode = barcode.strip() or None
        if barcode and (len(barcode) > 32 or not barcode.isdigit()):
            raise ValueError("Código de barras inválido")
        result["barcode"] = barcode
    if result["unit"] not in ("UN", "KG", "L"):
        raise ValueError("Unidade inválida")
    for key in ("price_cents", "cost_cents"):
        value = result[key]
        if type(value) is not int or value < 0 or value > 10**12:
            raise ValueError(f"{key} inválido")
    active = result["active"]
    if type(active) is bool:
        active = int(active)
    if type(active) is not int or active not in (0, 1):
        raise ValueError("Estado do produto inválido")
    result["active"] = active
    return result


def valid_positive_int(value, maximum=10**12):
    return type(value) is int and 0 < value <= maximum


def validate_sale(data):
    """Validate the immutable financial snapshot sent by the offline terminal."""
    if not isinstance(data, dict):
        raise ValueError("Venda inválida")
    for name in ("request_id", "cash_session_id"):
        value = data.get(name)
        if not isinstance(value, str) or not 1 <= len(value) <= 160:
            raise ValueError(f"{name} inválido")
    if (not valid_positive_int(data.get("total_cents")) or
        type(data.get("change_cents")) is not int or not 0 <= data["change_cents"] <= 10**12 or
        type(data.get("created_at")) is not int or not 0 < data["created_at"] <= 2**63 - 1):
        raise ValueError("Totais ou data da venda inválidos")
    items, payments = data.get("items"), data.get("payments")
    if not isinstance(items, list) or not 1 <= len(items) <= 200:
        raise ValueError("Itens da venda inválidos")
    if not isinstance(payments, list) or not 1 <= len(payments) <= 20:
        raise ValueError("Pagamentos da venda inválidos")
    seen, total, normalized_items = set(), 0, []
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("Item inválido")
        product_id, name = item.get("product_id"), item.get("name")
        quantity, price, line_total = (item.get("quantity"), item.get("unit_price_cents"),
                                       item.get("line_total_cents"))
        if (not isinstance(product_id, str) or not 1 <= len(product_id) <= 160 or
            product_id in seen or not isinstance(name, str) or not 1 <= len(name.strip()) <= 160 or
            not valid_positive_int(quantity, 10**6) or
            type(price) is not int or not 0 <= price <= 10**12 or
            type(line_total) is not int or line_total != quantity * price or line_total > 10**12):
            raise ValueError("Item inválido")
        seen.add(product_id)
        total += line_total
        normalized_items.append({"product_id": product_id, "name": name.strip(),
                                 "quantity": quantity, "unit_price_cents": price,
                                 "line_total_cents": line_total})
    if total != data["total_cents"]:
        raise ValueError("Total da venda não confere com os itens")
    paid, cash_paid, normalized_payments = 0, 0, []
    for payment in payments:
        if (not isinstance(payment, dict) or payment.get("method") not in ("cash", "pix", "card") or
            not valid_positive_int(payment.get("amount_cents"))):
            raise ValueError("Pagamento inválido")
        amount = payment["amount_cents"]
        paid += amount
        if payment["method"] == "cash":
            cash_paid += amount
        normalized_payments.append({"method": payment["method"], "amount_cents": amount})
    if paid != total + data["change_cents"] or data["change_cents"] > cash_paid:
        raise ValueError("Pagamentos não fecham a venda")
    return normalized_items, normalized_payments


def validate_terminal_adjustment(data):
    if not isinstance(data, dict):
        raise ValueError("Ajuste inválido")
    product_id, reason, delta, created_at = (data.get("product_id"), data.get("reason"),
                                             data.get("delta"), data.get("created_at"))
    if (not isinstance(product_id, str) or not 1 <= len(product_id) <= 160 or
        not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 240 or
        type(delta) is not int or delta == 0 or abs(delta) > 10**6 or
        type(created_at) is not int or not 0 < created_at <= 2**63 - 1):
        raise ValueError("Ajuste inválido")
    return product_id, delta * 1000, reason.strip(), created_at


def stock_balance(conn, tenant_id, shop_id, product_id):
    """Central ledger: manual movements + terminal adjustments + one debit per sale."""
    return conn.execute("""SELECT
        (SELECT COALESCE(SUM(delta_milli),0) FROM stock_movements
         WHERE tenant_id=? AND shop_id=? AND product_id=?) +
        (SELECT COALESCE(SUM(delta_milli),0) FROM terminal_stock_movements
         WHERE tenant_id=? AND shop_id=? AND product_id=?) +
        (SELECT COALESCE(SUM(delta_milli),0) FROM sale_stock_movements
         WHERE tenant_id=? AND shop_id=? AND product_id=?) AS balance""",
        (tenant_id, shop_id, product_id) * 3).fetchone()["balance"]


class SyncConflict(Exception):
    pass


def persist_sale(conn, terminal, event):
    """Materialize one immutable sale and one central stock debit per item."""
    tenant_id, shop_id, terminal_id = (terminal["tenant_id"], terminal["shop_id"], terminal["id"])
    sale_id, sale = event["aggregate_id"], event["data"]
    items, payments = validate_sale(sale)
    content = json.dumps(sale, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    content_hash = hashlib.sha256(content.encode()).hexdigest()
    previous = conn.execute("""SELECT terminal_id,content_hash FROM sales
        WHERE tenant_id=? AND id=?""", (tenant_id, sale_id)).fetchone()
    if previous:
        if previous["terminal_id"] != terminal_id or previous["content_hash"] != content_hash:
            raise SyncConflict("Venda já recebida com conteúdo diferente")
        return
    same_request = conn.execute("""SELECT id FROM sales
        WHERE tenant_id=? AND terminal_id=? AND request_id=?""",
        (tenant_id, terminal_id, sale["request_id"])).fetchone()
    if same_request:
        raise SyncConflict("Identificador de venda já usado por outra venda")
    stock_status = "ok"
    for item in items:
        exists = conn.execute("SELECT 1 FROM products WHERE tenant_id=? AND id=?",
                              (tenant_id, item["product_id"])).fetchone()
        if not exists:
            raise ValueError("Produto da venda não pertence à empresa")
        if stock_balance(conn, tenant_id, shop_id, item["product_id"]) - item["quantity"] * 1000 < 0:
            stock_status = "negative"
    received_at = int(time.time())
    conn.execute("""INSERT INTO sales
        (tenant_id,id,shop_id,terminal_id,event_id,request_id,cash_session_id,total_cents,
         change_cents,created_at,received_at,content_hash,stock_status)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (tenant_id, sale_id, shop_id, terminal_id, event["event_id"], sale["request_id"],
         sale["cash_session_id"], sale["total_cents"], sale["change_cents"],
         sale["created_at"], received_at, content_hash, stock_status))
    conn.executemany("""INSERT INTO sale_items
        (tenant_id,sale_id,line_number,product_id,name,quantity,unit_price_cents,line_total_cents)
        VALUES (?,?,?,?,?,?,?,?)""",
        [(tenant_id, sale_id, number, item["product_id"], item["name"], item["quantity"],
          item["unit_price_cents"], item["line_total_cents"])
         for number, item in enumerate(items)])
    conn.executemany("""INSERT INTO sale_payments
        (tenant_id,sale_id,line_number,method,amount_cents) VALUES (?,?,?,?,?)""",
        [(tenant_id, sale_id, number, payment["method"], payment["amount_cents"])
         for number, payment in enumerate(payments)])
    conn.executemany("""INSERT INTO sale_stock_movements
        (tenant_id,sale_id,shop_id,product_id,delta_milli,created_at)
        VALUES (?,?,?,?,?,?)""",
        [(tenant_id, sale_id, shop_id, item["product_id"], -item["quantity"] * 1000,
          sale["created_at"]) for item in items])


def persist_terminal_adjustment(conn, terminal, event):
    tenant_id, shop_id, terminal_id = (terminal["tenant_id"], terminal["shop_id"], terminal["id"])
    product_id, delta_milli, reason, created_at = validate_terminal_adjustment(event["data"])
    content = json.dumps(event["data"], ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    content_hash = hashlib.sha256(content.encode()).hexdigest()
    previous = conn.execute("""SELECT content_hash FROM terminal_stock_movements
        WHERE tenant_id=? AND terminal_id=? AND adjustment_id=?""",
        (tenant_id, terminal_id, event["aggregate_id"])).fetchone()
    if previous:
        if previous["content_hash"] != content_hash:
            raise SyncConflict("Ajuste já recebido com conteúdo diferente")
        return
    if not conn.execute("SELECT 1 FROM products WHERE tenant_id=? AND id=?",
                        (tenant_id, product_id)).fetchone():
        raise ValueError("Produto do ajuste não pertence à empresa")
    conn.execute("""INSERT INTO terminal_stock_movements
        (tenant_id,adjustment_id,event_id,shop_id,terminal_id,product_id,delta_milli,
         reason,created_at,content_hash) VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (tenant_id, event["aggregate_id"], event["event_id"], shop_id, terminal_id,
         product_id, delta_milli, reason, created_at, content_hash))


class API(BaseHTTPRequestHandler):
    server_version = "PDVSaaS/0.1"

    def log_message(self, fmt, *args):
        print("%s %s" % (self.address_string(), fmt % args))

    def respond(self, status, payload, cookie=None):
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(body)

    def body(self):
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0 or length > 65536:
            raise ValueError("Corpo ausente ou muito grande")
        value = json.loads(self.rfile.read(length))
        if not isinstance(value, dict):
            raise ValueError("JSON deve ser um objeto")
        return value

    def session(self, conn):
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
            token = cookie["pdv_session"].value
        except (KeyError, Exception):
            return None
        digest = hashlib.sha256(token.encode()).hexdigest()
        return conn.execute("""SELECT u.id, u.tenant_id, u.email, u.role FROM sessions s
            JOIN users u ON u.id=s.user_id WHERE s.token_hash=? AND s.expires_at>? AND u.active=1""",
            (digest, int(time.time()))).fetchone()

    def terminal(self, conn):
        authorization = self.headers.get("Authorization", "")
        if not authorization.startswith("Bearer "):
            return None
        token = authorization[7:]
        if not 32 <= len(token) <= 128:
            return None
        digest = hashlib.sha256(token.encode()).hexdigest()
        return conn.execute("""SELECT id,tenant_id,shop_id FROM terminals
            WHERE token_hash=? AND active=1""", (digest,)).fetchone()

    def same_origin(self):
        origin = self.headers.get("Origin")
        if not origin:
            return True
        host = self.headers.get("Host", "")
        return urlparse(origin).netloc == host and urlparse(origin).scheme in ("http", "https")

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/":
            body = (ROOT / "index.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/logo.svg":
            body = (ROOT / "logo-pdv2.svg").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "image/svg+xml")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/health":
            with db() as conn:
                conn.execute("SELECT 1")
            return self.respond(200, {"status": "ok"})
        with db() as conn:
            if path == "/api/plans":
                rows = conn.execute("SELECT id,name,price_cents,terminal_limit FROM plans WHERE active=1 ORDER BY price_cents").fetchall()
                return self.respond(200, {"plans": [dict(x) for x in rows]})
            user = self.session(conn)
            if not user:
                return self.respond(401, {"error": "Não autenticado"})
            if path == "/api/me":
                tenant = conn.execute("SELECT id,name FROM tenants WHERE id=?", (user["tenant_id"],)).fetchone()
                sub = conn.execute("SELECT plan_id,status FROM subscriptions WHERE tenant_id=?", (user["tenant_id"],)).fetchone()
                return self.respond(200, {"user": dict(user), "tenant": dict(tenant), "subscription": dict(sub)})
            if path == "/api/shops":
                rows = conn.execute("SELECT id,name,created_at FROM shops WHERE tenant_id=? ORDER BY created_at", (user["tenant_id"],)).fetchall()
                return self.respond(200, {"shops": [dict(x) for x in rows]})
            if path == "/api/terminals":
                if user["role"] != "owner":
                    return self.respond(403, {"error": "Sem permissão"})
                rows = conn.execute("""SELECT id,shop_id,name,active,created_at FROM terminals
                    WHERE tenant_id=? ORDER BY created_at""", (user["tenant_id"],)).fetchall()
                return self.respond(200, {"terminals": [dict(x) for x in rows]})
            if path == "/api/sales":
                query = parse_qs(parsed.query)
                try:
                    limit = int(query.get("limit", ["50"])[0])
                    if not 1 <= limit <= 100:
                        raise ValueError
                    cursor = query.get("cursor", [""])[0]
                    if cursor:
                        cursor_time, cursor_id = cursor.split(":", 1)
                        cursor_time = int(cursor_time)
                        if not 0 <= cursor_time <= 2**63 - 1 or not 1 <= len(cursor_id) <= 160:
                            raise ValueError
                except ValueError:
                    return self.respond(400, {"error": "Paginação inválida"})
                shop_id = query.get("shop_id", [""])[0]
                if shop_id and not conn.execute("SELECT 1 FROM shops WHERE tenant_id=? AND id=?",
                                                (user["tenant_id"], shop_id)).fetchone():
                    return self.respond(404, {"error": "Loja não encontrada"})
                where, params = "tenant_id=?", [user["tenant_id"]]
                if shop_id:
                    where += " AND shop_id=?"
                    params.append(shop_id)
                if cursor:
                    where += " AND (received_at<? OR (received_at=? AND id<?))"
                    params.extend((cursor_time, cursor_time, cursor_id))
                rows = conn.execute(f"""SELECT id,shop_id,terminal_id,total_cents,change_cents,
                    created_at,received_at,stock_status FROM sales WHERE {where}
                    ORDER BY received_at DESC,id DESC LIMIT ?""", (*params, limit + 1)).fetchall()
                page = rows[:limit]
                next_cursor = f"{page[-1]['received_at']}:{page[-1]['id']}" if len(rows) > limit else None
                return self.respond(200, {"sales": [dict(row) for row in page], "next_cursor": next_cursor})
            if path.startswith("/api/sales/"):
                sale_id = path.removeprefix("/api/sales/")
                row = conn.execute("""SELECT id,shop_id,terminal_id,event_id,request_id,cash_session_id,
                    total_cents,change_cents,created_at,received_at,stock_status
                    FROM sales WHERE tenant_id=? AND id=?""", (user["tenant_id"], sale_id)).fetchone()
                if not row:
                    return self.respond(404, {"error": "Venda não encontrada"})
                sale = dict(row)
                sale["items"] = [dict(item) for item in conn.execute("""SELECT product_id,name,quantity,
                    unit_price_cents,line_total_cents FROM sale_items
                    WHERE tenant_id=? AND sale_id=? ORDER BY line_number""", (user["tenant_id"], sale_id))]
                sale["payments"] = [dict(payment) for payment in conn.execute("""SELECT method,amount_cents
                    FROM sale_payments WHERE tenant_id=? AND sale_id=? ORDER BY line_number""",
                    (user["tenant_id"], sale_id))]
                return self.respond(200, {"sale": sale})
            if path == "/api/products":
                query = parse_qs(parsed.query)
                search = query.get("q", [""])[0].strip()
                if len(search) > 120:
                    return self.respond(400, {"error": "Busca muito longa"})
                include_inactive = query.get("include_inactive", ["0"])[0] == "1" and user["role"] in ("owner", "manager")
                rows = conn.execute("""SELECT id,sku,barcode,name,unit,price_cents,cost_cents,active,created_at,updated_at
                    FROM products WHERE tenant_id=? AND (?=1 OR active=1)
                    AND (name LIKE ? OR sku LIKE ? OR barcode LIKE ?)
                    ORDER BY name, id LIMIT 100""",
                    (user["tenant_id"], int(include_inactive), *(f"%{search}%",) * 3)).fetchall()
                products = [dict(x) for x in rows]
                if user["role"] == "cashier":
                    for product in products:
                        product.pop("cost_cents")
                return self.respond(200, {"products": products})
            if path.startswith("/api/products/"):
                product_id = path.removeprefix("/api/products/")
                row = conn.execute("""SELECT id,sku,barcode,name,unit,price_cents,cost_cents,active,created_at,updated_at
                    FROM products WHERE tenant_id=? AND id=?""", (user["tenant_id"], product_id)).fetchone()
                if not row or (not row["active"] and user["role"] == "cashier"):
                    return self.respond(404, {"error": "Produto não encontrado"})
                product = dict(row)
                if user["role"] == "cashier":
                    product.pop("cost_cents")
                return self.respond(200, {"product": product})
            if path == "/api/stock":
                shop_id = parse_qs(parsed.query).get("shop_id", [""])[0]
                shop = conn.execute("SELECT id FROM shops WHERE tenant_id=? AND id=?", (user["tenant_id"], shop_id)).fetchone()
                if not shop:
                    return self.respond(404, {"error": "Loja não encontrada"})
                rows = conn.execute("""SELECT p.id AS product_id,p.sku,p.name,p.unit,p.active,
                    COALESCE((SELECT SUM(delta_milli) FROM stock_movements m
                      WHERE m.tenant_id=p.tenant_id AND m.shop_id=? AND m.product_id=p.id),0) +
                    COALESCE((SELECT SUM(delta_milli) FROM terminal_stock_movements a
                      WHERE a.tenant_id=p.tenant_id AND a.shop_id=? AND a.product_id=p.id),0) +
                    COALESCE((SELECT SUM(delta_milli) FROM sale_stock_movements s
                      WHERE s.tenant_id=p.tenant_id AND s.shop_id=? AND s.product_id=p.id),0)
                    AS quantity_milli FROM products p
                    WHERE p.tenant_id=? ORDER BY p.name,p.id LIMIT 1000""",
                    (shop_id, shop_id, shop_id, user["tenant_id"])).fetchall()
                return self.respond(200, {"shop_id": shop_id, "stock": [dict(x) for x in rows]})
            if path == "/api/stock/movements":
                query = parse_qs(parsed.query)
                shop_id = query.get("shop_id", [""])[0]
                shop = conn.execute("SELECT id FROM shops WHERE tenant_id=? AND id=?", (user["tenant_id"], shop_id)).fetchone()
                if not shop:
                    return self.respond(404, {"error": "Loja não encontrada"})
                product_id = query.get("product_id", [""])[0]
                rows = conn.execute("""SELECT id,shop_id,product_id,delta_milli,reason,reference,
                    created_by,created_at,source,terminal_id,sale_id FROM (
                    SELECT id,shop_id,product_id,delta_milli,reason,reference,created_by,created_at,
                      'manual' AS source,NULL AS terminal_id,NULL AS sale_id,created_at AS sort_at
                    FROM stock_movements WHERE tenant_id=? AND shop_id=? AND (?='' OR product_id=?)
                    UNION ALL
                    SELECT adjustment_id AS id,shop_id,product_id,delta_milli,reason,
                      NULL AS reference,NULL AS created_by,created_at,
                      'terminal_adjustment' AS source,terminal_id,NULL AS sale_id,created_at / 1000 AS sort_at
                    FROM terminal_stock_movements WHERE tenant_id=? AND shop_id=? AND (?='' OR product_id=?)
                    UNION ALL
                    SELECT s.sale_id || ':' || s.product_id AS id,s.shop_id,s.product_id,s.delta_milli,
                      'Venda' AS reason,s.sale_id AS reference,NULL AS created_by,s.created_at,
                      'sale' AS source,v.terminal_id,s.sale_id,v.received_at AS sort_at
                    FROM sale_stock_movements s JOIN sales v ON v.tenant_id=s.tenant_id AND v.id=s.sale_id
                    WHERE s.tenant_id=? AND s.shop_id=? AND (?='' OR s.product_id=?))
                    ORDER BY sort_at DESC,id DESC LIMIT 100""",
                    (user["tenant_id"], shop_id, product_id, product_id) * 3).fetchall()
                return self.respond(200, {"movements": [dict(x) for x in rows]})
            if path == "/api/users":
                if user["role"] not in ("owner", "manager"):
                    return self.respond(403, {"error": "Sem permissão"})
                rows = conn.execute("SELECT id,email,role,active FROM users WHERE tenant_id=? ORDER BY email", (user["tenant_id"],)).fetchall()
                return self.respond(200, {"users": [dict(x) for x in rows]})
            if path == "/api/audit":
                if user["role"] != "owner":
                    return self.respond(403, {"error": "Sem permissão"})
                rows = conn.execute("SELECT action,subject_id,created_at FROM audit_events WHERE tenant_id=? ORDER BY created_at DESC LIMIT 100", (user["tenant_id"],)).fetchall()
                return self.respond(200, {"events": [dict(x) for x in rows]})
        self.respond(404, {"error": "Não encontrado"})

    def do_POST(self):
        if not self.same_origin():
            return self.respond(403, {"error": "Origem não permitida"})
        path = urlparse(self.path).path
        try:
            data = self.body()
        except (ValueError, json.JSONDecodeError):
            return self.respond(400, {"error": "JSON inválido"})
        with db() as conn:
            if path == "/api/sync/events":
                terminal = self.terminal(conn)
                if not terminal:
                    return self.respond(401, {"error": "Terminal não autenticado"})
                events = data.get("events")
                if not isinstance(events, list) or not 1 <= len(events) <= 50:
                    return self.respond(400, {"error": "Envie de 1 a 50 eventos"})
                prepared = []
                allowed_types = {"cash.opened", "cash.closed", "sale.completed", "stock.adjusted"}
                for event in events:
                    if not isinstance(event, dict) or set(event) != {"event_id", "type", "aggregate_id", "identity", "data"}:
                        return self.respond(400, {"error": "Evento inválido"})
                    identity = event["identity"]
                    if (not isinstance(identity, dict) or
                        identity.get("tenant_id") != terminal["tenant_id"] or
                        identity.get("shop_id") != terminal["shop_id"] or
                        identity.get("terminal_id") != terminal["id"]):
                        return self.respond(403, {"error": "Identidade do evento não corresponde ao terminal"})
                    if (not isinstance(event["event_id"], str) or len(event["event_id"]) != 32 or
                        not all(c in "0123456789abcdef" for c in event["event_id"]) or
                        not isinstance(event["type"], str) or event["type"] not in allowed_types or
                        not isinstance(event["aggregate_id"], str) or not 1 <= len(event["aggregate_id"]) <= 160 or
                        not isinstance(event["data"], dict)):
                        return self.respond(400, {"error": "Campos do evento inválidos"})
                    try:
                        if event["type"] == "sale.completed":
                            validate_sale(event["data"])
                        elif event["type"] == "stock.adjusted":
                            validate_terminal_adjustment(event["data"])
                    except ValueError as exc:
                        return self.respond(400, {"error": str(exc)})
                    payload = json.dumps(event, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                    prepared.append((event, payload, hashlib.sha256(payload.encode()).hexdigest()))
                conn.execute("BEGIN IMMEDIATE")
                accepted = []
                try:
                    for event, payload, digest in prepared:
                        prior = conn.execute("SELECT tenant_id,terminal_id,payload_hash FROM sync_events WHERE event_id=?",
                                             (event["event_id"],)).fetchone()
                        if prior:
                            if (prior["tenant_id"], prior["terminal_id"], prior["payload_hash"]) != (terminal["tenant_id"], terminal["id"], digest):
                                raise SyncConflict("Evento já recebido com conteúdo diferente")
                        else:
                            conn.execute("""INSERT INTO sync_events VALUES (?,?,?,?,?,?,?,?,?)""",
                                (event["event_id"], terminal["tenant_id"], terminal["shop_id"],
                                 terminal["id"], event["type"], event["aggregate_id"],
                                 payload, digest, int(time.time())))
                        if event["type"] == "sale.completed":
                            persist_sale(conn, terminal, event)
                        elif event["type"] == "stock.adjusted":
                            persist_terminal_adjustment(conn, terminal, event)
                        accepted.append(event["event_id"])
                except (ValueError, SyncConflict, sqlite3.IntegrityError) as exc:
                    conn.rollback()
                    return self.respond(409 if isinstance(exc, SyncConflict) else 400,
                                        {"error": str(exc) if not isinstance(exc, sqlite3.IntegrityError) else "Evento conflitante"})
                conn.commit()
                return self.respond(200, {"accepted_event_ids": accepted})
            if path == "/api/register":
                name = str(data.get("company", "")).strip()
                email = str(data.get("email", "")).strip().lower()
                password = str(data.get("password", ""))
                if not name or len(name) > 120 or "@" not in email or len(email) > 254 or not valid_password(password):
                    return self.respond(400, {"error": "Informe empresa, e-mail válido e senha de 8 a 128 caracteres com maiúscula, minúscula, número e símbolo"})
                tenant_id, user_id, shop_id = new_id(), new_id(), new_id()
                now = int(time.time())
                try:
                    conn.execute("INSERT INTO tenants VALUES (?,?,?)", (tenant_id, name, now))
                    conn.execute("INSERT INTO users VALUES (?,?,?,?,?,1,?)", (user_id, tenant_id, email, hash_password(password), "owner", now))
                    conn.execute("INSERT INTO shops VALUES (?,?,?,?)", (shop_id, tenant_id, "Loja principal", now))
                    conn.execute("INSERT INTO subscriptions VALUES (?,?,?,?)", (tenant_id, "essential", "trial", now))
                    conn.execute("INSERT INTO audit_events VALUES (?,?,?,?,?,?)", (new_id(), tenant_id, user_id, "tenant.created", tenant_id, now))
                    conn.commit()
                except sqlite3.IntegrityError:
                    return self.respond(409, {"error": "E-mail já cadastrado"})
                return self.respond(201, {"tenant_id": tenant_id, "shop_id": shop_id})
            if path == "/api/login":
                email = str(data.get("email", "")).strip().lower()
                password = str(data.get("password", ""))
                user = conn.execute("SELECT id,password_hash FROM users WHERE email=? AND active=1", (email,)).fetchone()
                if not user or not verify_password(password, user["password_hash"]):
                    return self.respond(401, {"error": "Credenciais inválidas"})
                token = secrets.token_urlsafe(32)
                conn.execute("INSERT INTO sessions VALUES (?,?,?)", (hashlib.sha256(token.encode()).hexdigest(), user["id"], int(time.time()) + SESSION_SECONDS))
                conn.commit()
                secure = "; Secure" if self.headers.get("X-Forwarded-Proto") == "https" else ""
                return self.respond(200, {"status": "ok"}, f"pdv_session={token}; HttpOnly; SameSite=Strict; Path=/; Max-Age={SESSION_SECONDS}{secure}")
            user = self.session(conn)
            if not user:
                return self.respond(401, {"error": "Não autenticado"})
            if path == "/api/logout":
                cookie = SimpleCookie()
                cookie.load(self.headers.get("Cookie", ""))
                if "pdv_session" in cookie:
                    digest = hashlib.sha256(cookie["pdv_session"].value.encode()).hexdigest()
                    conn.execute("DELETE FROM sessions WHERE token_hash=?", (digest,))
                    conn.commit()
                return self.respond(200, {"status": "ok"}, "pdv_session=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0")
            if path == "/api/shops":
                if user["role"] != "owner":
                    return self.respond(403, {"error": "Sem permissão"})
                name = str(data.get("name", "")).strip()
                if not name or len(name) > 120:
                    return self.respond(400, {"error": "Nome de loja inválido"})
                shop_id = new_id()
                now = int(time.time())
                conn.execute("INSERT INTO shops VALUES (?,?,?,?)", (shop_id, user["tenant_id"], name, now))
                conn.execute("INSERT INTO audit_events VALUES (?,?,?,?,?,?)", (new_id(), user["tenant_id"], user["id"], "shop.created", shop_id, now))
                conn.commit()
                return self.respond(201, {"id": shop_id, "name": name})
            if path == "/api/terminals":
                if user["role"] != "owner":
                    return self.respond(403, {"error": "Sem permissão"})
                shop_id = data.get("shop_id")
                name = data.get("name")
                if not isinstance(shop_id, str) or not isinstance(name, str) or not 1 <= len(name.strip()) <= 120:
                    return self.respond(400, {"error": "Terminal inválido"})
                if not conn.execute("SELECT 1 FROM shops WHERE tenant_id=? AND id=?", (user["tenant_id"], shop_id)).fetchone():
                    return self.respond(404, {"error": "Loja não encontrada"})
                plan = conn.execute("""SELECT p.terminal_limit FROM subscriptions s JOIN plans p ON p.id=s.plan_id
                    WHERE s.tenant_id=? AND s.status IN ('trial','active')""", (user["tenant_id"],)).fetchone()
                count = conn.execute("SELECT COUNT(*) FROM terminals WHERE tenant_id=? AND active=1", (user["tenant_id"],)).fetchone()[0]
                if not plan or count >= plan["terminal_limit"]:
                    return self.respond(409, {"error": "Limite de terminais do plano atingido"})
                terminal_id, token = new_id(), secrets.token_urlsafe(40)
                now = int(time.time())
                conn.execute("INSERT INTO terminals VALUES (?,?,?,?,?,1,?)",
                             (terminal_id, user["tenant_id"], shop_id, name.strip(), hashlib.sha256(token.encode()).hexdigest(), now))
                conn.execute("INSERT INTO audit_events VALUES (?,?,?,?,?,?)",
                             (new_id(), user["tenant_id"], user["id"], "terminal.created", terminal_id, now))
                conn.commit()
                return self.respond(201, {"id": terminal_id, "shop_id": shop_id, "name": name.strip(), "token": token})
            if path == "/api/users":
                if user["role"] != "owner":
                    return self.respond(403, {"error": "Sem permissão"})
                email = str(data.get("email", "")).strip().lower()
                password = str(data.get("password", ""))
                role = str(data.get("role", ""))
                if "@" not in email or len(email) > 254 or not valid_password(password) or role not in ("manager", "cashier"):
                    return self.respond(400, {"error": "E-mail, senha ou papel inválido"})
                user_id = new_id()
                now = int(time.time())
                try:
                    conn.execute("INSERT INTO users VALUES (?,?,?,?,?,1,?)", (user_id, user["tenant_id"], email, hash_password(password), role, now))
                    conn.execute("INSERT INTO audit_events VALUES (?,?,?,?,?,?)", (new_id(), user["tenant_id"], user["id"], "user.created", user_id, now))
                    conn.commit()
                except sqlite3.IntegrityError:
                    return self.respond(409, {"error": "E-mail já cadastrado"})
                return self.respond(201, {"id": user_id, "email": email, "role": role})
            if path == "/api/products":
                if user["role"] not in ("owner", "manager"):
                    return self.respond(403, {"error": "Sem permissão"})
                try:
                    product = product_payload(data)
                except ValueError as exc:
                    return self.respond(400, {"error": str(exc)})
                product_id, now = new_id(), int(time.time())
                try:
                    conn.execute("""INSERT INTO products
                        (id,tenant_id,sku,barcode,name,unit,price_cents,cost_cents,active,created_at,updated_at)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                        (product_id, user["tenant_id"], product["sku"], product["barcode"], product["name"],
                         product["unit"], product["price_cents"], product["cost_cents"], product["active"], now, now))
                    conn.execute("INSERT INTO audit_events VALUES (?,?,?,?,?,?)",
                                 (new_id(), user["tenant_id"], user["id"], "product.created", product_id, now))
                    conn.commit()
                except sqlite3.IntegrityError:
                    return self.respond(409, {"error": "SKU ou código de barras já cadastrado"})
                return self.respond(201, {"product": {"id": product_id, **product, "created_at": now, "updated_at": now}})
            if path == "/api/stock/movements":
                if user["role"] not in ("owner", "manager"):
                    return self.respond(403, {"error": "Sem permissão"})
                shop_id, product_id = data.get("shop_id"), data.get("product_id")
                delta = data.get("delta_milli")
                reason, reference = data.get("reason"), data.get("reference")
                key = data.get("idempotency_key")
                if (not isinstance(shop_id, str) or not isinstance(product_id, str) or
                    type(delta) is not int or delta == 0 or abs(delta) > 10**9 or
                    not isinstance(reason, str) or not 3 <= len(reason.strip()) <= 240 or
                    (reference is not None and (not isinstance(reference, str) or len(reference) > 120)) or
                    (key is not None and (not isinstance(key, str) or not 1 <= len(key) <= 120))):
                    return self.respond(400, {"error": "Movimentação inválida"})
                reason = reason.strip()
                reference = reference.strip() if reference is not None else None
                conn.execute("BEGIN IMMEDIATE")
                if key:
                    existing = conn.execute("""SELECT id,shop_id,product_id,delta_milli,reason,reference,created_by,created_at
                        FROM stock_movements WHERE tenant_id=? AND idempotency_key=?""",
                        (user["tenant_id"], key)).fetchone()
                    if existing:
                        same = (existing["shop_id"] == shop_id and existing["product_id"] == product_id and
                                existing["delta_milli"] == delta and existing["reason"] == reason and
                                existing["reference"] == reference)
                        if not same:
                            return self.respond(409, {"error": "Chave de repetição usada com dados diferentes"})
                        return self.respond(200, {"movement": dict(existing), "replayed": True})
                shop = conn.execute("SELECT id FROM shops WHERE tenant_id=? AND id=?", (user["tenant_id"], shop_id)).fetchone()
                product = conn.execute("SELECT id FROM products WHERE tenant_id=? AND id=? AND active=1", (user["tenant_id"], product_id)).fetchone()
                if not shop or not product:
                    return self.respond(404, {"error": "Loja ou produto não encontrado"})
                balance = stock_balance(conn, user["tenant_id"], shop_id, product_id)
                if balance + delta < 0:
                    return self.respond(409, {"error": "Estoque insuficiente para a movimentação"})
                movement_id, now = new_id(), int(time.time())
                conn.execute("""INSERT INTO stock_movements
                    (id,tenant_id,shop_id,product_id,delta_milli,reason,reference,idempotency_key,created_by,created_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (movement_id, user["tenant_id"], shop_id, product_id, delta, reason, reference, key, user["id"], now))
                conn.execute("INSERT INTO audit_events VALUES (?,?,?,?,?,?)",
                             (new_id(), user["tenant_id"], user["id"], "stock.moved", movement_id, now))
                conn.commit()
                return self.respond(201, {"movement": {"id": movement_id, "shop_id": shop_id,
                    "product_id": product_id, "delta_milli": delta, "reason": reason, "reference": reference,
                    "created_by": user["id"], "created_at": now}, "quantity_milli": balance + delta})
        self.respond(404, {"error": "Não encontrado"})

    def do_PATCH(self):
        if not self.same_origin():
            return self.respond(403, {"error": "Origem não permitida"})
        path = urlparse(self.path).path
        if not path.startswith("/api/products/"):
            return self.respond(404, {"error": "Não encontrado"})
        try:
            data = self.body()
        except (ValueError, json.JSONDecodeError):
            return self.respond(400, {"error": "JSON inválido"})
        with db() as conn:
            user = self.session(conn)
            if not user:
                return self.respond(401, {"error": "Não autenticado"})
            if user["role"] not in ("owner", "manager"):
                return self.respond(403, {"error": "Sem permissão"})
            product_id = path.removeprefix("/api/products/")
            row = conn.execute("""SELECT sku,barcode,name,unit,price_cents,cost_cents,active
                FROM products WHERE tenant_id=? AND id=?""", (user["tenant_id"], product_id)).fetchone()
            if not row:
                return self.respond(404, {"error": "Produto não encontrado"})
            try:
                product = product_payload(data, row)
            except ValueError as exc:
                return self.respond(400, {"error": str(exc)})
            now = int(time.time())
            try:
                conn.execute("""UPDATE products SET sku=?,barcode=?,name=?,unit=?,price_cents=?,
                    cost_cents=?,active=?,updated_at=? WHERE tenant_id=? AND id=?""",
                    (product["sku"], product["barcode"], product["name"], product["unit"],
                     product["price_cents"], product["cost_cents"], product["active"], now,
                     user["tenant_id"], product_id))
                conn.execute("INSERT INTO audit_events VALUES (?,?,?,?,?,?)",
                             (new_id(), user["tenant_id"], user["id"], "product.updated", product_id, now))
                conn.commit()
            except sqlite3.IntegrityError:
                return self.respond(409, {"error": "SKU ou código de barras já cadastrado"})
            return self.respond(200, {"product": {"id": product_id, **product, "updated_at": now}})

    def do_DELETE(self):
        if not self.same_origin():
            return self.respond(403, {"error": "Origem não permitida"})
        path = urlparse(self.path).path
        if not path.startswith("/api/products/"):
            return self.respond(404, {"error": "Não encontrado"})
        with db() as conn:
            user = self.session(conn)
            if not user:
                return self.respond(401, {"error": "Não autenticado"})
            if user["role"] not in ("owner", "manager"):
                return self.respond(403, {"error": "Sem permissão"})
            product_id = path.removeprefix("/api/products/")
            row = conn.execute("SELECT active FROM products WHERE tenant_id=? AND id=?",
                               (user["tenant_id"], product_id)).fetchone()
            if not row:
                return self.respond(404, {"error": "Produto não encontrado"})
            if row["active"]:
                now = int(time.time())
                conn.execute("UPDATE products SET active=0,updated_at=? WHERE tenant_id=? AND id=?",
                             (now, user["tenant_id"], product_id))
                conn.execute("INSERT INTO audit_events VALUES (?,?,?,?,?,?)",
                             (new_id(), user["tenant_id"], user["id"], "product.archived", product_id, now))
                conn.commit()
            return self.respond(200, {"id": product_id, "active": 0})


if __name__ == "__main__":
    init_db()
    host = os.environ.get("PDV_HOST", "127.0.0.1")
    port = int(os.environ.get("PDV_PORT", "8000"))
    print(f"PDV SaaS Phase 1: http://{host}:{port}")
    ThreadingHTTPServer((host, port), API).serve_forever()
