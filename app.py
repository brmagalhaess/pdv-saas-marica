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
        CREATE INDEX IF NOT EXISTS idx_users_tenant ON users(tenant_id);
        CREATE INDEX IF NOT EXISTS idx_shops_tenant ON shops(tenant_id);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_shops_tenant_id ON shops(tenant_id, id);
        CREATE INDEX IF NOT EXISTS idx_audit_tenant ON audit_events(tenant_id, created_at);
        CREATE INDEX IF NOT EXISTS idx_products_tenant ON products(tenant_id, name);
        CREATE INDEX IF NOT EXISTS idx_stock_tenant_shop_product ON stock_movements(tenant_id, shop_id, product_id);
        CREATE INDEX IF NOT EXISTS idx_terminals_tenant ON terminals(tenant_id, shop_id);
        CREATE INDEX IF NOT EXISTS idx_sync_events_tenant ON sync_events(tenant_id, received_at);
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
                rows = conn.execute("""SELECT event_id,shop_id,terminal_id,aggregate_id,payload,received_at
                    FROM sync_events WHERE tenant_id=? AND event_type='sale.completed'
                    ORDER BY received_at DESC LIMIT 100""", (user["tenant_id"],)).fetchall()
                sales = []
                for row in rows:
                    event = json.loads(row["payload"])
                    sales.append({"id": row["aggregate_id"], "shop_id": row["shop_id"],
                                  "terminal_id": row["terminal_id"],
                                  "total_cents": event["data"]["total_cents"],
                                  "created_at": event["data"]["created_at"],
                                  "received_at": row["received_at"]})
                return self.respond(200, {"sales": sales})
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
                    COALESCE(SUM(m.delta_milli),0) AS quantity_milli
                    FROM products p LEFT JOIN stock_movements m ON m.tenant_id=p.tenant_id
                      AND m.product_id=p.id AND m.shop_id=?
                    WHERE p.tenant_id=? GROUP BY p.id ORDER BY p.name,p.id LIMIT 1000""",
                    (shop_id, user["tenant_id"])).fetchall()
                return self.respond(200, {"shop_id": shop_id, "stock": [dict(x) for x in rows]})
            if path == "/api/stock/movements":
                query = parse_qs(parsed.query)
                shop_id = query.get("shop_id", [""])[0]
                shop = conn.execute("SELECT id FROM shops WHERE tenant_id=? AND id=?", (user["tenant_id"], shop_id)).fetchone()
                if not shop:
                    return self.respond(404, {"error": "Loja não encontrada"})
                product_id = query.get("product_id", [""])[0]
                rows = conn.execute("""SELECT id,shop_id,product_id,delta_milli,reason,reference,created_by,created_at
                    FROM stock_movements WHERE tenant_id=? AND shop_id=?
                      AND (?='' OR product_id=?) ORDER BY created_at DESC,id DESC LIMIT 100""",
                    (user["tenant_id"], shop_id, product_id, product_id)).fetchall()
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
                        event["type"] not in allowed_types or
                        not isinstance(event["aggregate_id"], str) or not 1 <= len(event["aggregate_id"]) <= 160 or
                        not isinstance(event["data"], dict)):
                        return self.respond(400, {"error": "Campos do evento inválidos"})
                    if event["type"] == "sale.completed":
                        sale = event["data"]
                        if (type(sale.get("total_cents")) is not int or sale["total_cents"] <= 0 or
                            type(sale.get("created_at")) is not int or
                            not isinstance(sale.get("items"), list) or not sale["items"]):
                            return self.respond(400, {"error": "Venda inválida"})
                    payload = json.dumps(event, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                    prepared.append((event, payload, hashlib.sha256(payload.encode()).hexdigest()))
                conn.execute("BEGIN IMMEDIATE")
                accepted = []
                for event, payload, digest in prepared:
                    prior = conn.execute("SELECT tenant_id,terminal_id,payload_hash FROM sync_events WHERE event_id=?",
                                         (event["event_id"],)).fetchone()
                    if prior:
                        if (prior["tenant_id"], prior["terminal_id"], prior["payload_hash"]) != (terminal["tenant_id"], terminal["id"], digest):
                            conn.rollback()
                            return self.respond(409, {"error": "Evento já recebido com conteúdo diferente"})
                    else:
                        conn.execute("""INSERT INTO sync_events VALUES (?,?,?,?,?,?,?,?,?)""",
                            (event["event_id"], terminal["tenant_id"], terminal["shop_id"],
                             terminal["id"], event["type"], event["aggregate_id"],
                             payload, digest, int(time.time())))
                    accepted.append(event["event_id"])
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
                balance = conn.execute("""SELECT COALESCE(SUM(delta_milli),0) AS quantity_milli
                    FROM stock_movements WHERE tenant_id=? AND shop_id=? AND product_id=?""",
                    (user["tenant_id"], shop_id, product_id)).fetchone()["quantity_milli"]
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
