"""SQLite state and business rules for a single offline terminal."""

import json
import os
import secrets
import sqlite3
import time
import uuid
from datetime import datetime
from collections import defaultdict
from contextlib import closing, contextmanager
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

from .printers import LABEL_PRINTER_REQUIRED, label_bytes, receipt_bytes, receipt_layout


# The cloud issues a normal offline lease for 72h (past_due is 24h).
# Keep this value exposed to the terminal UI and diagnostics.
OFFLINE_LEASE_SECONDS = 72 * 60 * 60
SELL_ROLES = {"owner", "admin", "manager", "cashier"}
MANAGER_ROLES = {"owner", "admin", "manager"}
CREDIT_METHODS = ("credit_account", "store_credit")
BUILTIN_NAMES = {"cash": "Dinheiro", "pix": "Pix", "card_credit": "Crédito", "card_debit": "Débito",
                 "credit_account": "A prazo (fiado)", "store_credit": "Crédito na loja"}
BUILTIN_PAYMENTS = tuple(BUILTIN_NAMES)
REFUND_METHODS = ("cash", "store_credit", "account")


def _brl(cents):
    return "R$ " + f"{cents / 100:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _return_amount(line_total_cents, line_quantity_milli, returned_milli):
    """Same half-up rule as the cloud (credit_logic.return_line_amount)."""
    return (2 * line_total_cents * returned_milli + line_quantity_milli) // (2 * line_quantity_milli)


DISCOUNT_BPS = {"cashier": 1000, "manager": 5000, "admin": 10000, "owner": 10000}
BUSINESS_DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


class TerminalError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def _now_ms():
    return time.time_ns() // 1_000_000


def _uuid():
    return str(uuid.uuid4())


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _positive_int(value, label, zero=False):
    if type(value) is not int or value < 0 or (not zero and value == 0) or value > 10**12:
        raise TerminalError(f"{label} inválido")
    return value


def _str(value, label, maximum=160):
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > maximum:
        raise TerminalError(f"{label} inválido")
    return value.strip()


def _line_gross(price_cents, quantity_milli):
    return int((Decimal(price_cents) * Decimal(quantity_milli) / Decimal(1000)).quantize(
        Decimal("1"), rounding=ROUND_HALF_UP))


def _allocate_discount(lines, discount, gross_total):
    """Apportion cents proportionally without discounting a line below zero."""
    shares = [divmod(discount * line["gross_cents"], gross_total) for line in lines]
    allocated = [share[0] for share in shares]
    remaining = discount - sum(allocated)
    order = sorted(range(len(lines)), key=lambda index: (-shares[index][1], index))
    for index in order:
        if not remaining:
            break
        if allocated[index] < lines[index]["gross_cents"]:
            allocated[index] += 1
            remaining -= 1
    if remaining:
        raise TerminalError("Não foi possível distribuir o desconto")
    for line, portion in zip(lines, allocated):
        line["discount_cents"] = portion
        line["line_total_cents"] = line["gross_cents"] - portion


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.path.parent.chmod(0o700)
        except OSError:
            pass
        with self.connection() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS products(
                    id TEXT PRIMARY KEY,sku TEXT,barcode TEXT UNIQUE,ncm TEXT,scale_code TEXT,name TEXT NOT NULL,
                    unit TEXT NOT NULL CHECK(unit IN ('UN','KG','L')),category TEXT,
                    price_cents INTEGER NOT NULL CHECK(price_cents>=0),
                    cost_cents INTEGER NOT NULL DEFAULT 0 CHECK(cost_cents>=0),
                    image_url TEXT NOT NULL DEFAULT '',
                    stock_controlled INTEGER NOT NULL DEFAULT 1 CHECK(stock_controlled IN (0,1)),
                    stock_milli INTEGER NOT NULL CHECK(stock_milli>=0),
                    active INTEGER NOT NULL CHECK(active IN (0,1)),updated_at INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS operators(
                    id TEXT PRIMARY KEY,name TEXT NOT NULL,role TEXT NOT NULL,
                    pin_hash TEXT NOT NULL,active INTEGER NOT NULL DEFAULT 1);
                CREATE TABLE IF NOT EXISTS login_attempts(
                    operator_id TEXT PRIMARY KEY,failures INTEGER NOT NULL,locked_until INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS cash_sessions(
                    id TEXT PRIMARY KEY,operator_id TEXT NOT NULL,opening_cents INTEGER NOT NULL,
                    opened_at INTEGER NOT NULL,closed_at INTEGER,counted_cents INTEGER,expected_cents INTEGER);
                CREATE UNIQUE INDEX IF NOT EXISTS cash_one_open ON cash_sessions((1)) WHERE closed_at IS NULL;
                CREATE TABLE IF NOT EXISTS cash_movements(
                    id TEXT PRIMARY KEY,session_id TEXT NOT NULL,kind TEXT NOT NULL,
                    amount_cents INTEGER NOT NULL,reason TEXT NOT NULL,operator_id TEXT NOT NULL,
                    created_at INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS sales(
                    id TEXT PRIMARY KEY,request_id TEXT NOT NULL UNIQUE,session_id TEXT NOT NULL,
                    operator_id TEXT NOT NULL,customer_name TEXT,customer_id TEXT,
                    gross_cents INTEGER NOT NULL,discount_cents INTEGER NOT NULL,
                    total_cents INTEGER NOT NULL,change_cents INTEGER NOT NULL,
                    snapshot TEXT NOT NULL,status TEXT NOT NULL,created_at INTEGER NOT NULL,
                    voided_at INTEGER,void_reason TEXT);
                CREATE TABLE IF NOT EXISTS customers(
                    id TEXT PRIMARY KEY,name TEXT NOT NULL,document TEXT,
                    credit_limit_cents INTEGER NOT NULL DEFAULT 0,open_credit_cents INTEGER NOT NULL DEFAULT 0,
                    store_credit_cents INTEGER NOT NULL DEFAULT 0,active INTEGER NOT NULL DEFAULT 1);
                CREATE TABLE IF NOT EXISTS sale_returns(
                    id TEXT PRIMARY KEY,sale_id TEXT NOT NULL,session_id TEXT,operator_id TEXT NOT NULL,
                    refund_method TEXT NOT NULL,total_cents INTEGER NOT NULL,items TEXT NOT NULL,
                    reason TEXT NOT NULL,created_at INTEGER NOT NULL);
                CREATE INDEX IF NOT EXISTS sale_returns_sale ON sale_returns(sale_id);
                CREATE TABLE IF NOT EXISTS suspended_carts(
                    id TEXT PRIMARY KEY,label TEXT NOT NULL,cart TEXT NOT NULL,
                    operator_id TEXT NOT NULL,created_at INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS outbox(
                    id TEXT PRIMARY KEY,event_type TEXT NOT NULL,aggregate_id TEXT NOT NULL,
                    payload TEXT NOT NULL,created_at INTEGER NOT NULL,acked_at INTEGER);
                CREATE INDEX IF NOT EXISTS outbox_pending ON outbox(acked_at,created_at);
                CREATE TABLE IF NOT EXISTS audit(
                    id TEXT PRIMARY KEY,operator_id TEXT,action TEXT NOT NULL,
                    subject_id TEXT,detail TEXT,created_at INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS sync_conflicts(
                    id TEXT PRIMARY KEY,event_id TEXT NOT NULL UNIQUE,event_type TEXT NOT NULL,
                    aggregate_id TEXT NOT NULL,reason TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open',detail TEXT NOT NULL,
                    created_at INTEGER NOT NULL,resolved_at INTEGER);
                CREATE INDEX IF NOT EXISTS sync_conflicts_status ON sync_conflicts(status,created_at);
                CREATE TABLE IF NOT EXISTS print_settings(
                    kind TEXT PRIMARY KEY,driver TEXT NOT NULL,target TEXT NOT NULL,format TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS print_jobs(
                    id TEXT PRIMARY KEY,kind TEXT NOT NULL,payload BLOB NOT NULL,
                    driver TEXT NOT NULL,target TEXT NOT NULL,status TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,error TEXT,created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL);
            """)
            if self._meta(conn, "device_id") is None:
                self._set_meta(conn, "device_id", _uuid())
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(products)")}
            if "cost_cents" not in columns:
                conn.execute("ALTER TABLE products ADD COLUMN cost_cents INTEGER NOT NULL DEFAULT 0")
            if "scale_code" not in columns:
                conn.execute("ALTER TABLE products ADD COLUMN scale_code TEXT")
            if "image_url" not in columns:
                conn.execute("ALTER TABLE products ADD COLUMN image_url TEXT NOT NULL DEFAULT ''")
            if "ncm" not in columns:
                conn.execute("ALTER TABLE products ADD COLUMN ncm TEXT")
            if "stock_controlled" not in columns:
                conn.execute("ALTER TABLE products ADD COLUMN stock_controlled INTEGER NOT NULL DEFAULT 1")
            if "group_id" not in columns:
                conn.execute("ALTER TABLE products ADD COLUMN group_id TEXT")
            if "group_name" not in columns:
                conn.execute("ALTER TABLE products ADD COLUMN group_name TEXT")
            if "variant" not in columns:
                conn.execute("ALTER TABLE products ADD COLUMN variant TEXT")
            sales_columns = {row["name"] for row in conn.execute("PRAGMA table_info(sales)")}
            if "customer_document" not in sales_columns:
                conn.execute("ALTER TABLE sales ADD COLUMN customer_document TEXT")
            conn.commit()
        try:
            self.path.chmod(0o600)
        except OSError:
            pass
        # A previous process may have sent bytes before crashing. Never retry it silently.
        with self.transaction() as conn:
            conn.execute("UPDATE print_jobs SET status='uncertain',error='Processo interrompido durante impressão' WHERE status='sending'")

    @contextmanager
    def connection(self):
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=10000")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=FULL")
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def transaction(self):
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
                conn.commit()
            except BaseException:
                conn.rollback()
                raise

    @staticmethod
    def _meta(conn, key):
        row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    @staticmethod
    def _set_meta(conn, key, value):
        conn.execute("INSERT INTO meta VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))

    def meta(self):
        with self.connection() as conn:
            return {row["key"]: row["value"] for row in conn.execute("SELECT key,value FROM meta")}

    def identity(self):
        meta = self.meta()
        return {key: meta.get(key) for key in ("tenant_id", "shop_id", "terminal_id", "device_id")}

    def connection_info(self):
        """Human-readable identity of the company this local device is serving."""
        meta = self.meta()
        return {
            "paired": bool(meta.get("terminal_id") and meta.get("token")),
            "tenant_id": meta.get("tenant_id"),
            "tenant_name": meta.get("tenant_name"),
            "shop_id": meta.get("shop_id"),
            "shop_name": meta.get("shop_name"),
            "terminal_id": meta.get("terminal_id"),
            "terminal_name": meta.get("terminal_name"),
            "issuer": json.loads(meta["issuer_profile"]) if meta.get("issuer_profile") else {},
            "license_hint": meta.get("license_hint"),
            "device_id": meta.get("device_id"),
            "cloud_url": meta.get("cloud_url"),
        }

    def _audit(self, conn, operator_id, action, subject_id=None, detail=None):
        conn.execute("INSERT INTO audit VALUES (?,?,?,?,?,?)",
                     (uuid.uuid4().hex, operator_id, action, subject_id, detail, _now_ms()))

    def _has_business_history(self, conn):
        # An old company's sales, receipts or audit trail must never become
        # visible to the next company's operators through this SQLite file.
        if any((self.path.parent / "backups").glob("terminal-*.sqlite3")):
            return True
        for table in ("sales", "cash_sessions", "cash_movements", "suspended_carts",
                      "outbox", "print_jobs"):
            if conn.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone():
                return True
        return conn.execute("""SELECT 1 FROM audit
            WHERE action NOT IN ('terminal.paired', 'terminal.bootstrap',
                                 'terminal.unpaired') LIMIT 1""").fetchone() is not None

    @staticmethod
    def _clear_company_cache(conn):
        # These are downloaded/setup data, not locally created business records.
        # Clearing them also makes exports after a new pairing tenant-isolated.
        for table in ("products", "operators", "customers", "login_attempts", "audit", "print_settings", "sync_conflicts"):
            conn.execute(f"DELETE FROM {table}")

    @staticmethod
    def _clear_connection_meta(conn):
        for key in ("token", "tenant_id", "tenant_name", "shop_id", "shop_name",
                    "terminal_id", "terminal_name", "license_hint", "lease", "public_key", "cloud_url",
                    "discount_max_bps", "issuer_profile", "business_hours", "bootstrap_at", "last_seen", "last_sync_at", "last_sync_error",
                    "release_info", "release_error", "release_checked_at", "last_backup_at",
                    "last_heartbeat_at", "last_heartbeat_error"):
            conn.execute("DELETE FROM meta WHERE key=?", (key,))

    def ensure_pairable(self):
        with self.connection() as conn:
            if self._meta(conn, "terminal_id"):
                raise TerminalError("Este dispositivo já está pareado; acione o suporte para transferência", 409)
            if self._has_business_history(conn):
                raise TerminalError("Este computador contém histórico de outra empresa. Clique em 'Criar novo perfil para outra empresa' ou use um diretório de dados separado; o banco atual será preservado.", 409)

    def _event(self, conn, event_type, aggregate_id, data):
        event_id = uuid.uuid4().hex
        identity = {key: self._meta(conn, key) for key in ("tenant_id", "shop_id", "terminal_id")}
        if not all(identity.values()):
            raise TerminalError("Terminal ainda não pareado", 409)
        event = {"event_id": event_id, "type": event_type, "aggregate_id": aggregate_id,
                 "identity": identity, "data": data}
        conn.execute("INSERT INTO outbox VALUES (?,?,?,?,?,NULL)",
                     (event_id, event_type, aggregate_id, _json(event), _now_ms()))
        return event_id

    def _receipt_context(self, conn, snapshot):
        """Keep issuer identity on new sales; enrich receipts from older versions."""
        result = dict(snapshot)
        if "issuer" not in result:
            raw = self._meta(conn, "issuer_profile")
            result["issuer"] = json.loads(raw) if raw else {"legal_name": self._meta(conn, "tenant_name") or ""}
        for key in ("terminal_name", "shop_name"):
            if key not in result:
                result[key] = self._meta(conn, key) or ""
        if "operator_name" not in result:
            operator = conn.execute("SELECT name FROM operators WHERE id=?", (snapshot.get("operator_id"),)).fetchone()
            result["operator_name"] = operator["name"] if operator else ""
        return result

    def _receipt_data(self, conn, snapshot):
        result = self._receipt_context(conn, snapshot)
        setting = conn.execute("SELECT format FROM print_settings WHERE kind='receipt'").fetchone()
        width = 58 if setting and setting["format"] == "escpos58" else 80
        result["receipt"] = receipt_layout(result, width)
        return result

    def pending_events(self, limit=50):
        with self.connection() as conn:
            return [json.loads(row["payload"]) for row in conn.execute(
                "SELECT payload FROM outbox WHERE acked_at IS NULL ORDER BY created_at,rowid LIMIT ?", (limit,))]

    def ack_events(self, ids):
        with self.transaction() as conn:
            for event_id in ids:
                conn.execute("UPDATE outbox SET acked_at=COALESCE(acked_at,?) WHERE id=?", (_now_ms(), event_id))

    def sync_status(self):
        with self.connection() as conn:
            release = self._meta(conn, "release_info")
            return {"pending": conn.execute("SELECT COUNT(*) FROM outbox WHERE acked_at IS NULL").fetchone()[0],
                    "conflicts": conn.execute("SELECT COUNT(*) FROM sync_conflicts WHERE status IN ('open','acknowledged')").fetchone()[0],
                    "last_sync_at": self._meta(conn, "last_sync_at"),
                    "last_sync_error": self._meta(conn, "last_sync_error"),
                    "last_heartbeat_at": self._meta(conn, "last_heartbeat_at"),
                    "last_heartbeat_error": self._meta(conn, "last_heartbeat_error"),
                    "bootstrap_at": self._meta(conn, "bootstrap_at"),
                    "release": json.loads(release) if release else None,
                    "release_error": self._meta(conn, "release_error")}

    def set_sync_result(self, error=None):
        with self.transaction() as conn:
            self._set_meta(conn, "last_sync_error", error or "")
            if error is None:
                self._set_meta(conn, "last_sync_at", str(_now_ms()))

    def set_heartbeat_result(self, error=None):
        with self.transaction() as conn:
            self._set_meta(conn, "last_heartbeat_error", error or "")
            if error is None:
                self._set_meta(conn, "last_heartbeat_at", str(_now_ms()))

    def record_sync_conflicts(self, conflicts):
        """Persist server conflict responses before acknowledging the outbox."""
        if not conflicts:
            return
        now = _now_ms()
        with self.transaction() as conn:
            for conflict in conflicts:
                if not isinstance(conflict, dict) or not conflict.get("event_id"):
                    continue
                conn.execute(
                    """INSERT INTO sync_conflicts
                       (id,event_id,event_type,aggregate_id,reason,status,detail,created_at,resolved_at)
                       VALUES (?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(event_id) DO UPDATE SET
                         event_type=excluded.event_type, aggregate_id=excluded.aggregate_id,
                         reason=excluded.reason, detail=excluded.detail""",
                    (str(conflict.get("id") or _uuid()), str(conflict["event_id"]),
                     str(conflict.get("event_type") or ""), str(conflict.get("aggregate_id") or ""),
                     str(conflict.get("reason") or "unknown"), str(conflict.get("status") or "open"),
                     _json(conflict.get("detail") or {}), now, None))

    def sync_conflicts(self, status=None):
        allowed = {"open", "acknowledged", "resolved", "ignored"}
        if status and status not in allowed:
            raise TerminalError("Estado de conflito inválido")
        with self.connection() as conn:
            query = "SELECT * FROM sync_conflicts"
            params = []
            if status:
                query += " WHERE status=?"
                params.append(status)
            query += " ORDER BY created_at DESC LIMIT 200"
            rows = []
            for row in conn.execute(query, params):
                item = dict(row)
                try:
                    item["detail"] = json.loads(item["detail"])
                except (TypeError, ValueError):
                    item["detail"] = {}
                rows.append(item)
            return rows

    def resolve_sync_conflict(self, conflict_id, status, resolution=""):
        if status not in {"acknowledged", "resolved", "ignored"}:
            raise TerminalError("Estado de resolução inválido")
        if not isinstance(resolution, str) or len(resolution) > 1000:
            raise TerminalError("Resolução inválida")
        with self.transaction() as conn:
            row = conn.execute("SELECT id FROM sync_conflicts WHERE id=?", (str(conflict_id),)).fetchone()
            if not row:
                raise TerminalError("Conflito não encontrado", 404)
            conn.execute("UPDATE sync_conflicts SET status=?,resolution=?,resolved_at=? WHERE id=?",
                         (status, resolution.strip(), _now_ms(), str(conflict_id)))
        return self.sync_conflicts()

    def set_release_result(self, payload=None, error=None):
        with self.transaction() as conn:
            self._set_meta(conn, "release_info", _json(payload) if payload is not None else "")
            self._set_meta(conn, "release_error", error or "")
            self._set_meta(conn, "release_checked_at", str(_now_ms()))

    def lease_status(self, now=None):
        """Verify signed lease and block new sales after its offline expiry."""
        from shared.licensing import LicenseError, verify_lease

        now = int(time.time()) if now is None else int(now)
        with self.transaction() as conn:
            envelope_raw = self._meta(conn, "lease")
            key = self._meta(conn, "public_key")
            if not envelope_raw or not key:
                return {"state": "unpaired", "can_sell": False, "reason": "Terminal não pareado"}
            last_seen = int(self._meta(conn, "last_seen") or 0)
            if last_seen and now + 300 < last_seen:
                return {"state": "clock_rollback", "can_sell": False,
                        "reason": "Relógio do computador voltou; ajuste a hora"}
            envelope = json.loads(envelope_raw)
            claims = envelope.get("payload", {})
            expires = claims.get("expires_at")
            if type(expires) is not int:
                return {"state": "invalid", "can_sell": False, "reason": "Licença inválida"}
            expected = {field: self._meta(conn, field) for field in
                        ("tenant_id", "shop_id", "terminal_id", "device_id")}
            try:
                verify_lease(envelope, key, expected=expected, now=now, last_seen=last_seen or None)
            except LicenseError:
                state = "expired" if now >= expires else "invalid"
                return {"state": state, "can_sell": False,
                        "reason": "Prazo offline expirado; conecte para renovar" if state == "expired" else "Licença inválida",
                        "expires_at": expires}
            if now > last_seen:
                self._set_meta(conn, "last_seen", str(now))
            return {"state": "active", "can_sell": True, "reason": "Licença ativa",
                    "expires_at": expires, "offline_seconds_max": OFFLINE_LEASE_SECONDS,
                    "plan_status": claims.get("status"), "features": claims.get("features", []),
                    "business_hours": claims.get("business_hours", {})}

    def business_hours_status(self, now=None):
        """Evaluate the last signed company schedule using the terminal clock."""
        now_dt = datetime.now() if now is None else now
        with self.connection() as conn:
            raw = self._meta(conn, "lease")
        try:
            hours = (json.loads(raw).get("payload", {}).get("business_hours") or {}) if raw else {}
        except (TypeError, ValueError):
            hours = {}
        if not isinstance(hours, dict) or not any(isinstance(row, dict) and row.get("enabled") for row in hours.values()):
            return {"configured": False, "open": True, "message": "Horário de funcionamento não configurado."}
        minute = now_dt.hour * 60 + now_dt.minute
        day_index = now_dt.weekday()
        for index, current_minute in ((day_index, minute), ((day_index - 1) % 7, minute)):
            row = hours.get(BUSINESS_DAYS[index]) or {}
            if not row.get("enabled"):
                continue
            try:
                opening = int(str(row.get("open", "")).split(":")[0]) * 60 + int(str(row.get("open", "")).split(":")[1])
                closing = int(str(row.get("close", "")).split(":")[0]) * 60 + int(str(row.get("close", "")).split(":")[1])
            except (ValueError, IndexError, TypeError):
                continue
            if closing > opening and index == day_index and opening <= minute < closing:
                return {"configured": True, "open": True, "message": "Dentro do horário de funcionamento.", "day": BUSINESS_DAYS[index]}
            if closing <= opening:
                if index == day_index and minute >= opening:
                    return {"configured": True, "open": True, "message": "Dentro do horário de funcionamento.", "day": BUSINESS_DAYS[index]}
                if index != day_index and current_minute < closing:
                    return {"configured": True, "open": True, "message": "Dentro do horário de funcionamento.", "day": BUSINESS_DAYS[index]}
        return {"configured": True, "open": False, "message": "Fora do horário de funcionamento da empresa."}

    def ensure_sale_allowed(self):
        status = self.lease_status()
        if not status["can_sell"]:
            raise TerminalError(status["reason"], 403)

    def set_pairing(self, response, cloud_url):
        from shared.licensing import LicenseError, verify_lease

        for key in ("token", "tenant_id", "shop_id", "terminal_id", "lease", "public_key"):
            if key not in response:
                raise TerminalError(f"Pareamento incompleto: {key}")
        with self.transaction() as conn:
            if self._meta(conn, "terminal_id"):
                raise TerminalError("Este dispositivo já está pareado; acione o suporte para transferência", 409)
            if self._has_business_history(conn):
                raise TerminalError("Este computador contém histórico de outra empresa. Clique em 'Criar novo perfil para outra empresa' ou use um diretório de dados separado; o banco atual será preservado.", 409)
            old_key = self._meta(conn, "public_key")
            if old_key and old_key != response["public_key"]:
                raise TerminalError("Chave pública mudou; suporte precisa verificar o servidor", 409)
            expected = {key: response[key] for key in ("tenant_id", "shop_id", "terminal_id")}
            expected["device_id"] = self._meta(conn, "device_id")
            try:
                verify_lease(response["lease"], response["public_key"], expected=expected)
            except LicenseError as exc:
                raise TerminalError("Licença do pareamento inválida", 403) from exc
            self._clear_company_cache(conn)
            self._clear_connection_meta(conn)
            for key in ("token", "tenant_id", "shop_id", "terminal_id", "lease", "public_key"):
                self._set_meta(conn, key, _json(response[key]) if key == "lease" else response[key])
            for key, parent in (("tenant_name", "tenant"), ("shop_name", "shop"), ("terminal_name", "terminal")):
                value = response.get(parent, {}).get("name") if isinstance(response.get(parent), dict) else None
                if value:
                    self._set_meta(conn, key, _str(value, key))
            if response.get("license_hint"):
                self._set_meta(conn, "license_hint", _str(response["license_hint"], "Licença", 16))
            if response.get("issuer"):
                self._set_meta(conn, "issuer_profile", _json(response["issuer"]))
            self._set_meta(conn, "cloud_url", cloud_url)
            self._audit(conn, None, "terminal.paired", response["terminal_id"])

    @staticmethod
    def _pending_stock_deltas(conn):
        """Return local stock effects not yet reflected by the cloud balance."""
        deltas = defaultdict(int)
        events = [json.loads(row[0]) for row in conn.execute(
            "SELECT payload FROM outbox WHERE acked_at IS NULL ORDER BY created_at,rowid")]
        for event in events:
            event_type = event.get("type")
            data = event.get("data") or {}
            if event_type == "sale.completed":
                for item in data.get("items") or []:
                    deltas[str(item.get("product_id"))] -= int(item.get("quantity_milli") or 0)
            elif event_type == "stock.adjusted":
                deltas[str(data.get("product_id"))] += int(data.get("delta_milli") or 0)
            elif event_type == "sale.voided":
                sale = conn.execute("SELECT snapshot FROM sales WHERE id=?", (event.get("aggregate_id"),)).fetchone()
                if sale:
                    for item in (json.loads(sale[0]).get("items") or []):
                        deltas[str(item.get("product_id"))] += int(item.get("quantity_milli") or 0)
            elif event_type == "sale.returned":
                for item in data.get("items") or []:
                    deltas[str(item.get("product_id"))] += int(item.get("quantity_milli") or 0)
        return deltas

    def apply_bootstrap(self, response):
        from shared.licensing import LicenseError, payload_digest, verify_lease

        with self.transaction() as conn:
            key = self._meta(conn, "public_key")
            if not key or response.get("public_key") != key:
                raise TerminalError("Chave pública não corresponde à chave fixada", 409)
            expected = {field: self._meta(conn, field) for field in
                        ("tenant_id", "shop_id", "terminal_id", "device_id")}
            lease = response.get("lease")
            try:
                claims = verify_lease(lease, key, expected=expected)
            except (LicenseError, TypeError) as exc:
                raise TerminalError("Licença do catálogo inválida", 403) from exc
            products = response.get("products")
            operators = response.get("operators")
            if not isinstance(products, list) or not isinstance(operators, list):
                raise TerminalError("Catálogo ou operadores inválidos")
            if claims.get("operators_hash") != payload_digest(operators):
                raise TerminalError("Lista de operadores não corresponde à licença assinada", 403)
            timestamp = _now_ms()
            pending_deltas = self._pending_stock_deltas(conn)
            # The cloud permits several products without barcodes and may move a
            # barcode between products. Clear old assignments before upserting.
            # Missing catalog entries remain on disk for sale history, inactive.
            conn.execute("UPDATE products SET barcode=NULL,active=0")
            for product in products:
                product_id = _str(product.get("id"), "ID do produto")
                unit = product.get("unit")
                if unit not in ("UN", "KG", "L"):
                    raise TerminalError("Unidade de produto inválida")
                price = _positive_int(product.get("price_cents"), "Preço", zero=True)
                stock = product.get("quantity_milli", 0)
                if type(stock) is not int:
                    raise TerminalError("Estoque recebido inválido")
                # Cloud is authoritative for acknowledged movements. Preserve
                # only effects still waiting in this terminal's outbox, so a
                # purchase or adjustment made in the web panel reaches the
                # cashier without overwriting an offline sale.
                stock = max(0, stock + pending_deltas.get(product_id, 0))
                category = product.get("category")
                if isinstance(category, dict):
                    category = category.get("name")
                image_url = product.get("image_url") or ""
                if not isinstance(image_url, str) or len(image_url) > 500:
                    raise TerminalError("Imagem do produto inválida")
                stock_controlled = product.get("stock_controlled", True)
                if type(stock_controlled) is not bool:
                    raise TerminalError("Configuração de estoque inválida")
                conn.execute("""INSERT INTO products
                    (id,sku,barcode,ncm,scale_code,name,unit,category,price_cents,cost_cents,image_url,stock_controlled,stock_milli,active,updated_at,
                     group_id,group_name,variant)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(id) DO UPDATE SET sku=excluded.sku,barcode=excluded.barcode,
                    ncm=excluded.ncm,
                    scale_code=excluded.scale_code,
                    name=excluded.name,unit=excluded.unit,category=excluded.category,
                    price_cents=excluded.price_cents,cost_cents=excluded.cost_cents,
                    image_url=excluded.image_url,stock_controlled=excluded.stock_controlled,stock_milli=excluded.stock_milli,
                    active=excluded.active,updated_at=excluded.updated_at,group_id=excluded.group_id,
                    group_name=excluded.group_name,variant=excluded.variant""",
                    (product_id, product.get("sku"), product.get("barcode") or None,
                     product.get("ncm") or None, product.get("scale_code") or None,
                     _str(product.get("name"), "Nome do produto"), unit, category or "Geral",
                     price, _positive_int(product.get("cost_cents", 0), "Custo", zero=True),
                     image_url,
                     int(stock_controlled),
                     stock,
                     int(bool(product.get("active", True))), timestamp,
                     product.get("group_id") or None, str(product.get("group_name") or "")[:140] or None,
                     str(product.get("variant") or "")[:120] or None))
            operator_ids = []
            for operator in operators:
                op_id = _str(operator.get("id"), "Operador")
                pin_hash = _str(operator.get("pin_hash"), "Hash do PIN", 400)
                operator_ids.append(op_id)
                previous = conn.execute("SELECT pin_hash FROM operators WHERE id=?", (op_id,)).fetchone()
                conn.execute("""INSERT INTO operators VALUES (?,?,?,?,1)
                    ON CONFLICT(id) DO UPDATE SET name=excluded.name,role=excluded.role,
                    pin_hash=excluded.pin_hash,active=1""",
                    (op_id, _str(operator.get("name"), "Nome do operador"),
                     _str(operator.get("role"), "Papel"), pin_hash))
                # A new or changed PIN must be usable immediately, even when this
                # device had accumulated failed attempts for the same operator.
                if previous is None or previous["pin_hash"] != pin_hash:
                    conn.execute("DELETE FROM login_attempts WHERE operator_id=?", (op_id,))
            if operator_ids:
                conn.execute("UPDATE operators SET active=0 WHERE id NOT IN (" +
                             ",".join("?" for _ in operator_ids) + ")", operator_ids)
            else:
                conn.execute("UPDATE operators SET active=0")
            if operator_ids:
                conn.execute("DELETE FROM login_attempts WHERE operator_id NOT IN (" +
                             ",".join("?" for _ in operator_ids) + ")", operator_ids)
            else:
                conn.execute("DELETE FROM login_attempts")
            customers = response.get("customers")
            if isinstance(customers, list):
                self._apply_customers(conn, customers)
            if isinstance(response.get("payment_methods"), list):
                self._set_meta(conn, "payment_methods", _json(self._validated_payment_methods(response["payment_methods"])))
            for key, parent in (("tenant_name", "tenant"), ("shop_name", "shop"), ("terminal_name", "terminal")):
                value = response.get(parent, {}).get("name") if isinstance(response.get(parent), dict) else None
                if value:
                    self._set_meta(conn, key, _str(value, key))
            if response.get("license_hint"):
                self._set_meta(conn, "license_hint", _str(response["license_hint"], "Licença", 16))
            if response.get("issuer"):
                self._set_meta(conn, "issuer_profile", _json(response["issuer"]))
            self._set_meta(conn, "lease", _json(lease))
            max_discount = claims.get("max_discount_bps", 10000)
            if type(max_discount) is not int or not 0 <= max_discount <= 10000:
                raise TerminalError("Limite de desconto da licença inválido", 403)
            self._set_meta(conn, "discount_max_bps", str(max_discount))
            self._set_meta(conn, "business_hours", _json(claims.get("business_hours") or {}))
            self._set_meta(conn, "bootstrap_at", str(timestamp))
            self._audit(conn, None, "terminal.bootstrap", None,
                        f"{len(products)} produtos; {len(operators)} operadores")
            return {"products": len(products), "operators": len(operators)}

    @staticmethod
    def _pending_credit_effects(conn):
        """Fiado and store-credit effects of events the cloud has not acknowledged."""
        effects = defaultdict(lambda: [0, 0])  # customer_id -> [open fiado, store credit]
        for (payload,) in conn.execute("SELECT payload FROM outbox WHERE acked_at IS NULL ORDER BY created_at,rowid"):
            event = json.loads(payload)
            data = event.get("data") or {}
            kind = event.get("type")
            if kind == "sale.completed" and data.get("customer_id"):
                for payment in data.get("payments") or []:
                    if payment.get("method") == "credit_account":
                        effects[data["customer_id"]][0] += int(payment["amount_cents"])
                    elif payment.get("method") == "store_credit":
                        effects[data["customer_id"]][1] -= int(payment["amount_cents"])
            elif kind == "sale.voided":
                row = conn.execute("SELECT snapshot FROM sales WHERE id=?", (data.get("sale_id"),)).fetchone()
                sale = json.loads(row["snapshot"]) if row else {}
                for payment in (sale.get("payments") or []) if sale.get("customer_id") else []:
                    if payment.get("method") == "credit_account":
                        effects[sale["customer_id"]][0] -= int(payment["amount_cents"])
                    elif payment.get("method") == "store_credit":
                        effects[sale["customer_id"]][1] += int(payment["amount_cents"])
            elif kind == "sale.returned" and data.get("customer_id"):
                if data.get("refund_method") == "account":
                    effects[data["customer_id"]][0] -= int(data["total_cents"])
                elif data.get("refund_method") == "store_credit":
                    effects[data["customer_id"]][1] += int(data["total_cents"])
        return effects

    def _apply_customers(self, conn, customers):
        pending = self._pending_credit_effects(conn)
        ids = []
        for customer in customers:
            customer_id = _str(customer.get("id"), "Cliente")
            values = [customer.get(key, 0) for key in ("credit_limit_cents", "open_credit_cents", "store_credit_cents")]
            if any(type(value) is not int for value in values) or values[0] < 0:
                raise TerminalError("Saldo de cliente inválido")
            open_credit = max(0, values[1] + pending[customer_id][0])
            store_credit = values[2] + pending[customer_id][1]
            conn.execute("""INSERT INTO customers VALUES (?,?,?,?,?,?,1)
                ON CONFLICT(id) DO UPDATE SET name=excluded.name,document=excluded.document,
                credit_limit_cents=excluded.credit_limit_cents,open_credit_cents=excluded.open_credit_cents,
                store_credit_cents=excluded.store_credit_cents,active=1""",
                (customer_id, _str(customer.get("name"), "Nome do cliente", 180),
                 str(customer.get("document") or "")[:30], values[0], open_credit, store_credit))
            ids.append(customer_id)
        conn.execute("UPDATE customers SET active=0" + (" WHERE id NOT IN (" + ",".join("?" for _ in ids) + ")" if ids else ""), ids)

    @staticmethod
    def _validated_payment_methods(rows):
        result = []
        for row in rows:
            if not isinstance(row, dict) or row.get("kind") not in BUILTIN_PAYMENTS + ("other",):
                raise TerminalError("Forma de pagamento recebida inválida")
            result.append({"id": _str(row.get("id"), "Forma de pagamento"), "kind": row["kind"],
                           "name": _str(row.get("name"), "Nome da forma de pagamento", 60),
                           "active": bool(row.get("active", True))})
        return result

    def _payment_config(self, conn):
        raw = self._meta(conn, "payment_methods")
        if raw:
            return json.loads(raw)
        # Terminals paired before configurable methods: every built-in stays enabled.
        return [{"id": kind, "kind": kind, "name": name, "active": True} for kind, name in BUILTIN_NAMES.items()]

    def payment_methods(self):
        with self.connection() as conn:
            return self._payment_config(conn)

    def customers(self, query=""):
        query = f"%{str(query or '').strip()[:60]}%"
        with self.connection() as conn:
            return [dict(row) for row in conn.execute(
                """SELECT id,name,document,credit_limit_cents,open_credit_cents,store_credit_cents,
                   MAX(credit_limit_cents-open_credit_cents,0) available_credit_cents
                   FROM customers WHERE active=1 AND (name LIKE ? OR document LIKE ?) ORDER BY name LIMIT 50""",
                (query, query))]

    def unpair(self):
        """Undo a clean setup; a database with business history stays bound."""
        with self.transaction() as conn:
            pending = conn.execute("SELECT COUNT(*) FROM outbox WHERE acked_at IS NULL").fetchone()[0]
            if pending:
                raise TerminalError("Sincronize as vendas pendentes antes de trocar de empresa", 409)
            if conn.execute("SELECT 1 FROM cash_sessions WHERE closed_at IS NULL").fetchone():
                raise TerminalError("Feche o caixa antes de trocar de empresa", 409)
            if self._has_business_history(conn):
                raise TerminalError("Este computador contém histórico desta empresa. Use uma nova instalação ou diretório de dados para conectar outra empresa; preserve o banco atual.", 409)
            self._clear_company_cache(conn)
            self._clear_connection_meta(conn)
            self._audit(conn, None, "terminal.unpaired")
        return {"paired": False}

    def operators(self):
        with self.connection() as conn:
            return [dict(row) for row in conn.execute(
                "SELECT id,name,role FROM operators WHERE active=1 ORDER BY name")]

    def operator(self, operator_id):
        with self.connection() as conn:
            row = conn.execute("SELECT id,name,role FROM operators WHERE id=? AND active=1", (operator_id,)).fetchone()
            if not row:
                raise TerminalError("Sessão do operador inválida", 401)
            return dict(row)

    def operator_pin_hash(self, operator_id):
        with self.connection() as conn:
            row = conn.execute("SELECT pin_hash FROM operators WHERE id=? AND active=1", (operator_id,)).fetchone()
            if not row:
                raise TerminalError("Sessão do operador inválida", 401)
            return row["pin_hash"]

    def login(self, operator_id, pin):
        from shared.security import verify_pin

        operator_id = _str(operator_id, "Operador")
        now = int(time.time())
        with self.transaction() as conn:
            attempts = conn.execute("SELECT failures,locked_until FROM login_attempts WHERE operator_id=?",
                                    (operator_id,)).fetchone()
            if attempts and attempts["locked_until"] > now:
                raise TerminalError("Muitas tentativas; aguarde cinco minutos", 429)
            row = conn.execute("SELECT * FROM operators WHERE id=? AND active=1", (operator_id,)).fetchone()
            valid = bool(row and isinstance(pin, str) and verify_pin(pin, row["pin_hash"]))
            if not valid:
                failures = (attempts["failures"] if attempts else 0) + 1
                conn.execute("""INSERT INTO login_attempts VALUES (?,?,?)
                    ON CONFLICT(operator_id) DO UPDATE SET failures=excluded.failures,locked_until=excluded.locked_until""",
                    (operator_id, failures, now + 300 if failures >= 5 else 0))
                self._audit(conn, operator_id, "operator.login_failed")
            else:
                conn.execute("DELETE FROM login_attempts WHERE operator_id=?", (operator_id,))
                self._audit(conn, operator_id, "operator.login")
                result = {"id": row["id"], "name": row["name"], "role": row["role"]}
        if not valid:
            raise TerminalError("Operador ou PIN inválido", 401)
        return result

    def manager_override(self, pin):
        from shared.security import verify_pin

        if not isinstance(pin, str) or not pin:
            raise TerminalError("PIN de gerente necessário", 403)
        now = int(time.time())
        with self.transaction() as conn:
            attempts = conn.execute("SELECT failures,locked_until FROM login_attempts WHERE operator_id='manager_override'").fetchone()
            if attempts and attempts["locked_until"] > now:
                raise TerminalError("Muitas tentativas de autorização; aguarde", 429)
            for row in conn.execute("SELECT * FROM operators WHERE active=1 AND role IN ('owner','admin','manager')"):
                if verify_pin(pin, row["pin_hash"]):
                    conn.execute("DELETE FROM login_attempts WHERE operator_id='manager_override'")
                    return row["id"]
            failures = (attempts["failures"] if attempts else 0) + 1
            conn.execute("""INSERT INTO login_attempts VALUES ('manager_override',?,?)
                ON CONFLICT(operator_id) DO UPDATE SET failures=excluded.failures,locked_until=excluded.locked_until""",
                (failures, now + 300 if failures >= 5 else 0))
        raise TerminalError("PIN de gerente inválido", 403)

    def authorize_discount(self, pin, gross_cents, discount_cents):
        """Validate a manager PIN and the company discount ceiling before checkout."""
        self.ensure_sale_allowed()
        gross = _positive_int(gross_cents, "Total da venda")
        discount = _positive_int(discount_cents, "Desconto", zero=True)
        if gross <= 0 or discount <= 0 or discount >= gross:
            raise TerminalError("Desconto ou total inválido")
        with self.connection() as conn:
            configured_limit = int(self._meta(conn, "discount_max_bps") or 10000)
        if not 0 <= configured_limit <= 10000:
            configured_limit = 10000
        if discount * 10000 > gross * configured_limit:
            raise TerminalError("Desconto acima do limite configurado pela administração", 403)
        self.manager_override(pin)
        return {"authorized": True}

    def products(self, query="", category=""):
        query = str(query)[:100].strip()
        with self.connection() as conn:
            if query:
                exact = conn.execute("SELECT * FROM products WHERE active=1 AND (id=? OR barcode=? OR sku=?)",
                                     (query, query, query)).fetchone()
                if exact:
                    return [dict(exact)]
            sql = "SELECT * FROM products WHERE active=1"
            params = []
            if query:
                sql += " AND (name LIKE ? OR sku LIKE ?)"
                params.extend((f"%{query}%", f"%{query}%"))
            if category:
                sql += " AND category=?"
                params.append(category)
            return [dict(row) for row in conn.execute(sql + " ORDER BY name LIMIT 150", params)]

    def categories(self):
        with self.connection() as conn:
            return [row[0] for row in conn.execute("SELECT DISTINCT category FROM products WHERE active=1 ORDER BY category")]

    def cash_state(self):
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM cash_sessions WHERE closed_at IS NULL").fetchone()
            if not row:
                return None
            return dict(row)

    def _open_cash(self, conn):
        row = conn.execute("SELECT * FROM cash_sessions WHERE closed_at IS NULL").fetchone()
        if not row:
            raise TerminalError("Abra o caixa antes da operação", 409)
        return row

    @staticmethod
    def _cash_expected(conn, cash):
        return Store._cash_summary(conn, cash)["expected_cents"]

    @staticmethod
    def _cash_summary(conn, cash):
        categories = {"cash_sales_net_cents": 0, "pix_cents": 0,
                      "card_credit_cents": 0, "card_debit_cents": 0,
                      "card_legacy_cents": 0, "supplied_cents": 0,
                      "withdrawn_cents": 0}
        sales_total = received_total = changes = 0
        sales_count = 0
        for row in conn.execute("SELECT snapshot FROM sales WHERE session_id=? AND status='completed'",
                                (cash["id"],)):
            sale = json.loads(row["snapshot"])
            sales_count += 1
            sales_total += int(sale.get("total_cents") or 0)
            changes += int(sale.get("change_cents") or 0)
            payment_total = 0
            for payment in sale.get("payments", []):
                method = payment.get("method")
                amount = int(payment.get("amount_cents") or 0)
                payment_total += amount
                if method == "cash":
                    categories["cash_sales_net_cents"] += amount
                elif method == "pix":
                    categories["pix_cents"] += amount
                elif method == "card_credit":
                    categories["card_credit_cents"] += amount
                elif method == "card_debit":
                    categories["card_debit_cents"] += amount
                elif method == "card":
                    categories["card_legacy_cents"] += amount
            received_total += payment_total
        categories["cash_sales_net_cents"] -= changes
        categories["cash_refunds_cents"] = conn.execute(
            "SELECT COALESCE(SUM(total_cents),0) FROM sale_returns WHERE session_id=? AND refund_method='cash'",
            (cash["id"],)).fetchone()[0]
        movements = conn.execute(
            "SELECT kind,COALESCE(SUM(amount_cents),0) amount FROM cash_movements "
            "WHERE session_id=? GROUP BY kind", (cash["id"],))
        for row in movements:
            if row["kind"] == "supplied":
                categories["supplied_cents"] = row["amount"]
            elif row["kind"] == "withdrawn":
                categories["withdrawn_cents"] = row["amount"]
        expected = (cash["opening_cents"] + categories["cash_sales_net_cents"] +
                    categories["supplied_cents"] - categories["withdrawn_cents"] - categories["cash_refunds_cents"])
        return {
            "id": cash["id"], "opened_at": cash["opened_at"],
            "opening_cents": cash["opening_cents"], "sales_count": sales_count,
            "sales_total_cents": sales_total,
            "received_total_cents": received_total - changes,
            "categories": categories, "expected_cents": expected,
            "total_channels_cents": (categories["cash_sales_net_cents"] +
                                      categories["pix_cents"] + categories["card_credit_cents"] +
                                      categories["card_debit_cents"] + categories["card_legacy_cents"]),
        }

    def cash_summary(self):
        with self.connection() as conn:
            return self._cash_summary(conn, self._open_cash(conn))

    def open_cash(self, operator_id, opening_cents):
        self.ensure_sale_allowed()
        schedule = self.business_hours_status()
        if schedule["configured"] and not schedule["open"]:
            raise TerminalError("Fora do horário de funcionamento da empresa; o caixa não pode ser aberto agora", 403)
        opening_cents = _positive_int(opening_cents, "Valor de abertura", zero=True)
        with self.transaction() as conn:
            if conn.execute("SELECT 1 FROM cash_sessions WHERE closed_at IS NULL").fetchone():
                raise TerminalError("Já existe caixa aberto", 409)
            session_id, timestamp = _uuid(), _now_ms()
            conn.execute("INSERT INTO cash_sessions VALUES (?,?,?, ?,NULL,NULL,NULL)",
                         (session_id, operator_id, opening_cents, timestamp))
            self._event(conn, "cash.opened", session_id,
                        {"opening_cents": opening_cents, "operator_id": operator_id, "created_at": timestamp})
            self._audit(conn, operator_id, "cash.opened", session_id)
            return {"id": session_id, "opening_cents": opening_cents, "opened_at": timestamp}

    def cash_movement(self, operator_id, kind, amount_cents, reason):
        self.ensure_sale_allowed()
        if kind not in ("withdrawn", "supplied"):
            raise TerminalError("Tipo de movimento inválido")
        amount_cents = _positive_int(amount_cents, "Valor")
        reason = _str(reason, "Motivo", 240)
        with self.transaction() as conn:
            cash = self._open_cash(conn)
            if kind == "withdrawn" and amount_cents > self._cash_expected(conn, cash):
                raise TerminalError("Sangria excede o dinheiro esperado no caixa", 409)
            movement_id, timestamp = _uuid(), _now_ms()
            conn.execute("INSERT INTO cash_movements VALUES (?,?,?,?,?,?,?)",
                         (movement_id, cash["id"], kind, amount_cents, reason, operator_id, timestamp))
            self._event(conn, "cash." + kind, movement_id,
                        {"cash_session_id": cash["id"], "amount_cents": amount_cents,
                         "reason": reason, "operator_id": operator_id, "created_at": timestamp})
            self._audit(conn, operator_id, "cash." + kind, movement_id, reason)
            return {"id": movement_id, "kind": kind, "amount_cents": amount_cents}

    def close_cash(self, operator_id, counted_cents, manager_pin):
        # Fechar o caixa é uma operação de supervisão. A validação acontece
        # antes de alterar a sessão para que um PIN inválido nunca deixe o
        # operador em um estado parcialmente fechado.
        supervisor_id = self.manager_override(manager_pin)
        counted_cents = _positive_int(counted_cents, "Valor contado", zero=True)
        with self.transaction() as conn:
            cash = self._open_cash(conn)
            summary = self._cash_summary(conn, cash)
            expected = summary["expected_cents"]
            timestamp = _now_ms()
            conn.execute("UPDATE cash_sessions SET closed_at=?,counted_cents=?,expected_cents=? WHERE id=?",
                         (timestamp, counted_cents, expected, cash["id"]))
            self._event(conn, "cash.closed", cash["id"],
                        {"counted_cents": counted_cents, "expected_cents": expected,
                         "difference_cents": counted_cents - expected, "created_at": timestamp,
                         "operator_id": operator_id, "supervisor_id": supervisor_id})
            self._audit(conn, operator_id, "cash.closed", cash["id"])
            summary.update({"closed_at": timestamp, "expected_cents": expected,
                            "counted_cents": counted_cents,
                            "difference_cents": counted_cents - expected})
            return summary

    def authorize_item_removal(self, operator, manager_pin=None):
        """Require a supervisor PIN when a cashier removes a line from a cart."""
        authorizer = operator["id"] if operator.get("role") in MANAGER_ROLES else self.manager_override(manager_pin)
        with self.transaction() as conn:
            self._audit(conn, operator["id"], "cart.item_removed_authorized", None, f"authorizer={authorizer}")
        return {"authorized": True, "authorizer_id": authorizer}

    def suspend_cart(self, operator_id, label, cart):
        if not isinstance(cart, dict) or not isinstance(cart.get("items"), list) or not cart["items"]:
            raise TerminalError("Carrinho vazio")
        label = _str(label, "Nome do carrinho", 80)
        with self.transaction() as conn:
            cart_id = _uuid()
            conn.execute("INSERT INTO suspended_carts VALUES (?,?,?,?,?)",
                         (cart_id, label, _json(cart), operator_id, _now_ms()))
            self._audit(conn, operator_id, "cart.suspended", cart_id)
            return {"id": cart_id, "label": label}

    def suspended_carts(self):
        with self.connection() as conn:
            return [{"id": row["id"], "label": row["label"], "cart": json.loads(row["cart"]),
                     "created_at": row["created_at"]} for row in conn.execute(
                "SELECT * FROM suspended_carts ORDER BY created_at DESC")]

    def resume_cart(self, operator_id, cart_id):
        with self.transaction() as conn:
            row = conn.execute("SELECT * FROM suspended_carts WHERE id=?", (cart_id,)).fetchone()
            if not row:
                raise TerminalError("Carrinho não encontrado", 404)
            conn.execute("DELETE FROM suspended_carts WHERE id=?", (cart_id,))
            self._audit(conn, operator_id, "cart.resumed", cart_id)
            return json.loads(row["cart"])

    def sale(self, operator, request):
        self.ensure_sale_allowed()
        if operator["role"] not in SELL_ROLES:
            raise TerminalError("Papel sem permissão de venda", 403)
        request_id = _str(request.get("request_id"), "Identificador da venda")
        item_requests = request.get("items")
        payments = request.get("payments")
        if not isinstance(item_requests, list) or not item_requests or not isinstance(payments, list) or not payments:
            raise TerminalError("Itens e pagamentos obrigatórios")
        discount = _positive_int(request.get("discount_cents", 0), "Desconto", zero=True)
        customer_name = str(request.get("customer_name") or "").strip()[:160]
        customer_document = str(request.get("customer_document") or "").strip()[:30]
        customer_id = request.get("customer_id") or None
        if customer_id is not None:
            customer_id = _str(customer_id, "Cliente")
        seller_id = request.get("seller_id") or None
        if seller_id is not None:
            seller_id = _str(seller_id, "Vendedor")
        override_id = self.manager_override(request.get("manager_pin")) if request.get("manager_pin") else None
        with self.transaction() as conn:
            old = conn.execute("SELECT snapshot FROM sales WHERE request_id=?", (request_id,)).fetchone()
            if old:
                previous = json.loads(old["snapshot"])
                comparable = {"items": [{"product_id": x["product_id"], "quantity_milli": x["quantity_milli"]}
                                        for x in previous["items"]],
                              "payments": previous["payments"],
                              "discount_cents": previous["discount_cents"],
                              "customer_name": previous.get("requested_customer", [previous["customer_name"]])[0],
                              "customer_document": previous.get("requested_customer", [None, previous.get("customer_document", "")])[1],
                              "customer_id": previous["customer_id"],
                              "seller_id": previous.get("seller_id")}
                received = {"items": item_requests, "payments": payments, "discount_cents": discount,
                            "customer_name": customer_name, "customer_document": customer_document,
                            "customer_id": customer_id, "seller_id": seller_id}
                if _json(comparable) != _json(received):
                    raise TerminalError("Identificador já usado para outra venda", 409)
                return self._receipt_data(conn, previous)
            cash = self._open_cash(conn)
            seller_name = ""
            if seller_id is not None:
                seller = conn.execute("SELECT name FROM operators WHERE id=? AND active=1", (seller_id,)).fetchone()
                if not seller:
                    raise TerminalError("Vendedor não encontrado", 404)
                seller_name = seller["name"]
            lines, seen = [], set()
            for item in item_requests:
                if not isinstance(item, dict):
                    raise TerminalError("Item inválido")
                product_id = _str(item.get("product_id"), "Produto")
                qty = _positive_int(item.get("quantity_milli"), "Quantidade")
                if product_id in seen:
                    raise TerminalError("Produto repetido; some as quantidades")
                seen.add(product_id)
                row = conn.execute("SELECT * FROM products WHERE id=? AND active=1", (product_id,)).fetchone()
                if not row:
                    raise TerminalError("Produto não encontrado", 404)
                if row["unit"] == "UN" and qty % 1000:
                    raise TerminalError("Produto UN exige quantidade inteira")
                if row["stock_controlled"] and row["stock_milli"] < qty:
                    raise TerminalError(f"Estoque insuficiente: {row['name']}", 409)
                gross = _line_gross(row["price_cents"], qty)
                lines.append({"product_id": product_id, "name": row["name"], "sku": row["sku"] or "",
                              "ncm": row["ncm"] or "", "unit": row["unit"],
                              "stock_controlled": bool(row["stock_controlled"]),
                              "quantity_milli": qty, "unit_price_cents": row["price_cents"],
                              "gross_cents": gross})
            gross_total = sum(line["gross_cents"] for line in lines)
            if gross_total <= 0 or discount >= gross_total:
                raise TerminalError("Desconto ou total inválido")
            configured_limit = int(self._meta(conn, "discount_max_bps") or 10000)
            if not 0 <= configured_limit <= 10000:
                configured_limit = 10000
            if discount * 10000 > gross_total * configured_limit:
                raise TerminalError("Desconto acima do limite configurado pela administração", 403)
            role_limit = DISCOUNT_BPS.get(operator["role"], 0)
            if discount * 10000 > gross_total * role_limit and not override_id:
                raise TerminalError("PIN de gerente necessário para este desconto", 403)
            _allocate_discount(lines, discount, gross_total)
            total = gross_total - discount
            normalized_payments = []
            config = self._payment_config(conn)
            enabled = {row["kind"] for row in config if row["active"] and row["kind"] != "other"} | {"card"}
            others = {row["id"]: row["name"] for row in config if row["kind"] == "other" and row["active"]}
            payment_names = {}
            for payment in payments:
                if not isinstance(payment, dict) or payment.get("method") not in (
                        "cash", "pix", "card", "card_credit", "card_debit", "other", *CREDIT_METHODS):
                    raise TerminalError("Forma de pagamento inválida")
                line = {"method": payment["method"],
                        "amount_cents": _positive_int(payment.get("amount_cents"), "Pagamento")}
                if payment["method"] == "other":
                    configured_id = payment.get("payment_method_id")
                    if configured_id not in others:
                        raise TerminalError("Forma de pagamento desativada ou desconhecida", 409)
                    line["payment_method_id"] = configured_id
                    payment_names[configured_id] = others[configured_id]
                elif payment["method"] not in enabled:
                    raise TerminalError("Forma de pagamento desativada pela empresa", 409)
                normalized_payments.append(line)
            paid = sum(p["amount_cents"] for p in normalized_payments)
            cash_paid = sum(p["amount_cents"] for p in normalized_payments if p["method"] == "cash")
            change = paid - total
            if change < 0 or change > cash_paid:
                raise TerminalError("Pagamentos não fecham o total; troco exige dinheiro")
            requested_customer = None
            on_account = sum(p["amount_cents"] for p in normalized_payments if p["method"] == "credit_account")
            from_credit = sum(p["amount_cents"] for p in normalized_payments if p["method"] == "store_credit")
            if on_account or from_credit:
                customer = conn.execute("SELECT * FROM customers WHERE id=? AND active=1", (customer_id,)).fetchone() if customer_id else None
                if not customer:
                    raise TerminalError("Venda a prazo ou com crédito na loja exige cliente cadastrado", 409)
                if from_credit > customer["store_credit_cents"]:
                    raise TerminalError(f"Crédito na loja insuficiente: disponível {_brl(customer['store_credit_cents'])}", 409)
                available = customer["credit_limit_cents"] - customer["open_credit_cents"]
                if on_account > available and not override_id:
                    raise TerminalError(f"Limite de fiado excedido: disponível {_brl(max(0, available))}; PIN de gerente necessário", 403)
                conn.execute("UPDATE customers SET open_credit_cents=open_credit_cents+?,store_credit_cents=store_credit_cents-? WHERE id=?",
                             (on_account, from_credit, customer_id))
                requested_customer = [customer_name, customer_document]
                customer_name = customer_name or customer["name"]
                customer_document = customer_document or (customer["document"] or "")
            sale_id, timestamp = _uuid(), _now_ms()
            snapshot = {"id": sale_id, "request_id": request_id, "cash_session_id": cash["id"],
                        "operator_id": operator["id"], "seller_id": seller_id, "seller_name": seller_name,
                        "customer_name": customer_name,
                        "customer_document": customer_document,
                        "customer_id": customer_id, "gross_cents": gross_total,
                        "discount_cents": discount, "total_cents": total, "change_cents": change,
                        "items": lines, "payments": normalized_payments, "created_at": timestamp,
                        "status": "completed"}
            if payment_names:
                snapshot["payment_names"] = payment_names
            if requested_customer and requested_customer != [customer_name, customer_document]:
                snapshot["requested_customer"] = requested_customer  # for idempotent retries
            snapshot = self._receipt_context(conn, snapshot)
            for line in lines:
                if line.get("stock_controlled", True):
                    conn.execute("UPDATE products SET stock_milli=stock_milli-? WHERE id=?",
                                 (line["quantity_milli"], line["product_id"]))
            conn.execute("""INSERT INTO sales
                (id,request_id,session_id,operator_id,customer_name,customer_id,gross_cents,
                 discount_cents,total_cents,change_cents,snapshot,status,created_at,voided_at,void_reason,customer_document)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,NULL,?)""",
                         (sale_id, request_id, cash["id"], operator["id"], customer_name,
                          customer_id, gross_total, discount, total, change, _json(snapshot),
                          "completed", timestamp, customer_document))
            event_data = {key: snapshot[key] for key in ("request_id", "cash_session_id", "created_at",
                                                       "gross_cents", "discount_cents", "total_cents",
                                                       "change_cents", "customer_id", "customer_name",
                                                       "customer_document", "operator_id", "seller_id",
                                                       "items", "payments")}
            self._event(conn, "sale.completed", sale_id, event_data)
            self._audit(conn, operator["id"], "sale.completed", sale_id,
                        f"manager_override={override_id}" if override_id else None)
            self._queue_receipt(conn, snapshot)
            return self._receipt_data(conn, snapshot)

    def sales(self, limit=50):
        with self.connection() as conn:
            return [dict(row) for row in conn.execute("""SELECT id,request_id,customer_name,total_cents,status,
                created_at,void_reason FROM sales ORDER BY created_at DESC LIMIT ?""", (min(max(int(limit), 1), 100),))]

    def sale_detail(self, sale_id):
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM sales WHERE id=?", (sale_id,)).fetchone()
            if not row:
                raise TerminalError("Venda não encontrada", 404)
            result = json.loads(row["snapshot"])
            result["status"] = row["status"]
            result["void_reason"] = row["void_reason"]
            return self._receipt_data(conn, result)

    def void_sale(self, operator, sale_id, reason, manager_pin=None):
        reason = _str(reason, "Motivo do cancelamento", 240)
        self.ensure_sale_allowed()
        authorizer = operator["id"] if operator["role"] in MANAGER_ROLES else self.manager_override(manager_pin)
        with self.transaction() as conn:
            row = conn.execute("SELECT * FROM sales WHERE id=?", (sale_id,)).fetchone()
            if not row:
                raise TerminalError("Venda não encontrada", 404)
            if row["status"] == "voided":
                return self.sale_detail(sale_id)
            if conn.execute("SELECT 1 FROM sale_returns WHERE sale_id=?", (sale_id,)).fetchone():
                raise TerminalError("Venda com devolução não pode ser cancelada; devolva os itens restantes", 409)
            original_cash = conn.execute("SELECT closed_at FROM cash_sessions WHERE id=?", (row["session_id"],)).fetchone()
            if original_cash and original_cash["closed_at"] is not None:
                raise TerminalError("Caixa dessa venda já foi fechado; registre devolução pelo processo administrativo", 409)
            snapshot = json.loads(row["snapshot"])
            for item in snapshot["items"]:
                if item.get("stock_controlled", True):
                    conn.execute("UPDATE products SET stock_milli=stock_milli+? WHERE id=?",
                                 (item["quantity_milli"], item["product_id"]))
            if snapshot.get("customer_id"):
                for payment in snapshot.get("payments", []):
                    if payment["method"] == "credit_account":
                        conn.execute("UPDATE customers SET open_credit_cents=MAX(open_credit_cents-?,0) WHERE id=?",
                                     (payment["amount_cents"], snapshot["customer_id"]))
                    elif payment["method"] == "store_credit":
                        conn.execute("UPDATE customers SET store_credit_cents=store_credit_cents+? WHERE id=?",
                                     (payment["amount_cents"], snapshot["customer_id"]))
            timestamp = _now_ms()
            conn.execute("UPDATE sales SET status='voided',voided_at=?,void_reason=? WHERE id=?",
                         (timestamp, reason, sale_id))
            self._event(conn, "sale.voided", sale_id,
                        {"sale_id": sale_id, "reason": reason, "created_at": timestamp})
            self._audit(conn, operator["id"], "sale.voided", sale_id, f"authorizer={authorizer}; {reason}")
            snapshot["status"] = "voided"
            snapshot["void_reason"] = reason
            return self._receipt_data(conn, snapshot)

    def returnable(self, sale_id):
        """Lines of a sale with the quantity still available for return."""
        with self.connection() as conn:
            row = conn.execute("SELECT snapshot,status FROM sales WHERE id=?", (sale_id,)).fetchone()
            if not row:
                raise TerminalError("Venda não encontrada", 404)
            snapshot = json.loads(row["snapshot"])
            returned = self._returned_quantities(conn, sale_id)
            return {"sale_id": sale_id, "status": row["status"], "customer_id": snapshot.get("customer_id"),
                    "customer_name": snapshot.get("customer_name", ""),
                    "on_account": any(p["method"] == "credit_account" for p in snapshot.get("payments", [])),
                    "items": [{"product_id": item["product_id"], "name": item["name"], "unit": item["unit"],
                               "quantity_milli": item["quantity_milli"], "line_total_cents": item["line_total_cents"],
                               "available_milli": item["quantity_milli"] - returned.get(item["product_id"], 0)}
                              for item in snapshot["items"]]}

    @staticmethod
    def _returned_quantities(conn, sale_id):
        totals = defaultdict(int)
        for (items,) in conn.execute("SELECT items FROM sale_returns WHERE sale_id=?", (sale_id,)):
            for item in json.loads(items):
                totals[item["product_id"]] += item["quantity_milli"]
        return totals

    def return_sale(self, operator, sale_id, request):
        """Partial or total return; refund in cash, store credit or abated from fiado."""
        self.ensure_sale_allowed()
        if operator["role"] not in SELL_ROLES:
            raise TerminalError("Papel sem permissão de devolução", 403)
        return_id = _str(request.get("return_id"), "Identificador da devolução")
        refund = request.get("refund_method")
        if refund not in REFUND_METHODS:
            raise TerminalError("Forma de devolução inválida")
        reason = _str(request.get("reason"), "Motivo da devolução", 240)
        wanted = request.get("items")
        if not isinstance(wanted, list) or not wanted:
            raise TerminalError("Informe os itens devolvidos")
        supervisor = operator["id"] if operator["role"] in MANAGER_ROLES else self.manager_override(request.get("manager_pin"))
        with self.transaction() as conn:
            old = conn.execute("SELECT * FROM sale_returns WHERE id=?", (return_id,)).fetchone()
            if old:
                if old["sale_id"] != sale_id or old["refund_method"] != refund:
                    raise TerminalError("Identificador já usado para outra devolução", 409)
                return {"id": old["id"], "sale_id": sale_id, "refund_method": refund,
                        "total_cents": old["total_cents"], "items": json.loads(old["items"])}
            row = conn.execute("SELECT snapshot,status FROM sales WHERE id=?", (sale_id,)).fetchone()
            if not row:
                raise TerminalError("Venda não encontrada", 404)
            if row["status"] != "completed":
                raise TerminalError("Venda cancelada não aceita devolução", 409)
            snapshot = json.loads(row["snapshot"])
            lines = {item["product_id"]: item for item in snapshot["items"]}
            returned = self._returned_quantities(conn, sale_id)
            items, total, seen = [], 0, set()
            for item in wanted:
                if not isinstance(item, dict):
                    raise TerminalError("Item inválido")
                product_id = _str(item.get("product_id"), "Produto")
                qty = _positive_int(item.get("quantity_milli"), "Quantidade")
                line = lines.get(product_id)
                if line is None or product_id in seen:
                    raise TerminalError("Produto não pertence à venda ou está repetido")
                seen.add(product_id)
                if line["unit"] == "UN" and qty % 1000:
                    raise TerminalError("Produto UN exige quantidade inteira")
                if returned.get(product_id, 0) + qty > line["quantity_milli"]:
                    raise TerminalError(f"Quantidade maior que a disponível para devolução: {line['name']}", 409)
                amount = _return_amount(line["line_total_cents"], line["quantity_milli"], qty)
                if amount <= 0:
                    raise TerminalError("Valor devolvido inválido")
                items.append({"product_id": product_id, "quantity_milli": qty, "amount_cents": amount})
                total += amount
            customer_id = request.get("customer_id") or snapshot.get("customer_id")
            session_id = None
            if refund == "cash":
                cash = self._open_cash(conn)
                session_id = cash["id"]
                if total > self._cash_expected(conn, cash):
                    raise TerminalError("Dinheiro no caixa insuficiente para a devolução", 409)
            elif refund == "store_credit":
                if not customer_id or not conn.execute("SELECT 1 FROM customers WHERE id=? AND active=1", (customer_id,)).fetchone():
                    raise TerminalError("Crédito na loja exige cliente cadastrado", 409)
                conn.execute("UPDATE customers SET store_credit_cents=store_credit_cents+? WHERE id=?", (total, customer_id))
            else:
                if not any(p["method"] == "credit_account" for p in snapshot.get("payments", [])):
                    raise TerminalError("Abatimento no fiado exige venda a prazo", 409)
                customer = conn.execute("SELECT open_credit_cents FROM customers WHERE id=?", (customer_id,)).fetchone()
                open_credit = customer["open_credit_cents"] if customer else 0
                conn.execute("UPDATE customers SET open_credit_cents=?,store_credit_cents=store_credit_cents+? WHERE id=?",
                             (max(0, open_credit - total), max(0, total - open_credit), customer_id))
            for item in items:
                if lines[item["product_id"]].get("stock_controlled", True):
                    conn.execute("UPDATE products SET stock_milli=stock_milli+? WHERE id=?",
                                 (item["quantity_milli"], item["product_id"]))
            timestamp = _now_ms()
            conn.execute("INSERT INTO sale_returns VALUES (?,?,?,?,?,?,?,?,?)",
                         (return_id, sale_id, session_id, operator["id"], refund, total, _json(items), reason, timestamp))
            data = {"sale_id": sale_id, "items": items, "total_cents": total, "refund_method": refund,
                    "reason": reason, "operator_id": operator["id"], "supervisor_id": supervisor,
                    "created_at": timestamp}
            if customer_id:
                data["customer_id"] = customer_id
            if session_id:
                data["cash_session_id"] = session_id
            self._event(conn, "sale.returned", return_id, data)
            self._audit(conn, operator["id"], "sale.returned", sale_id, f"{refund}; {total}; supervisor={supervisor}")
            return {"id": return_id, "sale_id": sale_id, "refund_method": refund, "total_cents": total, "items": items}

    def adjust_stock(self, operator, product_id, delta_milli, reason):
        self.ensure_sale_allowed()
        if operator["role"] not in ("owner", "admin", "manager", "inventory"):
            raise TerminalError("Sem permissão para ajustar estoque", 403)
        if type(delta_milli) is not int or delta_milli == 0 or abs(delta_milli) > 10**12:
            raise TerminalError("Quantidade inválida")
        reason = _str(reason, "Motivo", 240)
        with self.transaction() as conn:
            row = conn.execute("SELECT stock_milli,unit,stock_controlled FROM products WHERE id=?", (product_id,)).fetchone()
            if not row:
                raise TerminalError("Produto não encontrado", 404)
            if not row["stock_controlled"]:
                raise TerminalError("Produto de produção própria não usa controle de estoque")
            if row["unit"] == "UN" and delta_milli % 1000:
                raise TerminalError("Produto UN exige ajuste em unidades inteiras")
            if row["stock_milli"] + delta_milli < 0:
                raise TerminalError("Estoque ficaria negativo")
            adjustment_id, timestamp = _uuid(), _now_ms()
            conn.execute("UPDATE products SET stock_milli=stock_milli+? WHERE id=?", (delta_milli, product_id))
            self._event(conn, "stock.adjusted", adjustment_id,
                        {"product_id": product_id, "delta_milli": delta_milli,
                         "reason": reason, "created_at": timestamp})
            self._audit(conn, operator["id"], "stock.adjusted", product_id, reason)
            return {"id": adjustment_id, "stock_milli": row["stock_milli"] + delta_milli}

    def audit(self):
        with self.connection() as conn:
            return [dict(row) for row in conn.execute("SELECT * FROM audit ORDER BY created_at DESC LIMIT 100")]

    def export_data(self):
        """Full operator-visible export, including unacknowledged events.

        Excludes terminal token, PIN hashes, signing key and session secret.
        """
        with self.connection() as conn:
            sales = []
            for row in conn.execute("SELECT * FROM sales ORDER BY created_at,id"):
                sale = json.loads(row["snapshot"])
                sale["status"] = row["status"]
                sale["void_reason"] = row["void_reason"]
                sales.append(sale)
            return {"identity": {key: self._meta(conn, key) for key in
                                  ("tenant_id", "shop_id", "terminal_id", "device_id")},
                    "products": [dict(row) for row in conn.execute("SELECT * FROM products ORDER BY name")],
                    "cash_sessions": [dict(row) for row in conn.execute("SELECT * FROM cash_sessions ORDER BY opened_at")],
                    "cash_movements": [dict(row) for row in conn.execute("SELECT * FROM cash_movements ORDER BY created_at")],
                    "sales": sales,
                    "audit": [dict(row) for row in conn.execute("SELECT * FROM audit ORDER BY created_at")],
                    "pending_events": [json.loads(row["payload"]) for row in conn.execute(
                        "SELECT payload FROM outbox WHERE acked_at IS NULL ORDER BY created_at,rowid")]}

    def backup_status(self):
        directory = self.path.parent / "backups"
        copies = sorted(directory.glob("terminal-*.sqlite3"), reverse=True) if directory.exists() else []
        return {"directory": str(directory), "last_backup_at": self.meta().get("last_backup_at"),
                "copies": [{"name": path.name, "size_bytes": path.stat().st_size}
                           for path in copies[:7]]}

    def backup_now(self):
        """Create a transaction-consistent SQLite snapshot without exposing it over HTTP."""
        directory = self.path.parent / "backups"
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            directory.chmod(0o700)
        except OSError:
            pass
        backup_id = f"terminal-{_now_ms()}-{secrets.token_hex(3)}.sqlite3"
        destination = directory / backup_id
        staging = directory / f".{backup_id}.tmp"
        descriptor = os.open(staging, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(descriptor)
        try:
            with self.connection() as source:
                with closing(sqlite3.connect(staging)) as target:
                    source.backup(target)
                    if target.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                        raise TerminalError("Backup local não passou na verificação")
                    target.commit()
            with staging.open("rb+") as copied:
                os.fsync(copied.fileno())
            os.replace(staging, destination)
            with self.transaction() as conn:
                self._set_meta(conn, "last_backup_at", str(_now_ms()))
                self._audit(conn, None, "terminal.backup", backup_id)
            for older in sorted(directory.glob("terminal-*.sqlite3"), reverse=True)[7:]:
                older.unlink()
            return {"name": backup_id, "directory": str(directory),
                    "size_bytes": destination.stat().st_size}
        finally:
            staging.unlink(missing_ok=True)

    def printer_settings(self):
        with self.connection() as conn:
            return [dict(row) for row in conn.execute("SELECT * FROM print_settings ORDER BY kind")]

    def save_printer(self, kind, driver, target, fmt, operator_id=None):
        if kind not in ("receipt", "label") or driver not in ("tcp", "windows_raw", "cups", "file"):
            raise TerminalError("Configuração de impressora inválida")
        if kind == "label" and fmt == "escpos50x30":
            raise TerminalError(LABEL_PRINTER_REQUIRED, 409)
        if (kind == "receipt" and fmt not in ("escpos58", "escpos80")) or (
                kind == "label" and fmt not in ("zpl", "epl")):
            raise TerminalError("Formato de impressão inválido")
        target = _str(target, "Destino", 260)
        with self.transaction() as conn:
            conn.execute("INSERT INTO print_settings VALUES (?,?,?,?) ON CONFLICT(kind) DO UPDATE SET "
                         "driver=excluded.driver,target=excluded.target,format=excluded.format",
                         (kind, driver, target, fmt))
            self._audit(conn, operator_id, "print.settings_changed", kind, f"{driver}:{fmt}")
        return {"kind": kind, "driver": driver, "target": target, "format": fmt}

    def _queue_receipt(self, conn, snapshot):
        setting = conn.execute("SELECT * FROM print_settings WHERE kind='receipt'").fetchone()
        if not setting:
            return None
        snapshot = self._receipt_context(conn, snapshot)
        width = 58 if setting["format"] == "escpos58" else 80
        return self._queue_print(conn, "receipt", receipt_bytes(snapshot, width), setting)

    def _queue_print(self, conn, kind, payload, setting):
        job_id, timestamp = _uuid(), _now_ms()
        conn.execute("INSERT INTO print_jobs VALUES (?,?,?,?,?,'queued',0,NULL,?,?)",
                     (job_id, kind, payload, setting["driver"], setting["target"], timestamp, timestamp))
        return job_id

    def queue_label(self, product_id, quantity=1):
        quantity = _positive_int(quantity, "Quantidade de etiquetas")
        if quantity > 999:
            raise TerminalError("A quantidade de etiquetas deve ficar entre 1 e 999")
        with self.transaction() as conn:
            product = conn.execute("SELECT * FROM products WHERE id=?", (product_id,)).fetchone()
            setting = conn.execute("SELECT * FROM print_settings WHERE kind='label'").fetchone()
            if not product:
                raise TerminalError("Produto não encontrado", 404)
            if not setting or setting["format"] == "escpos50x30":
                raise TerminalError(LABEL_PRINTER_REQUIRED, 409)
            single_label = label_bytes(dict(product), setting["format"])
            job_id = self._queue_print(conn, "label", single_label * quantity, setting)
            return {"id": job_id, "status": "queued", "quantity": quantity}

    def reprint(self, sale_id, operator_id=None):
        snapshot = self.sale_detail(sale_id)
        with self.transaction() as conn:
            setting = conn.execute("SELECT * FROM print_settings WHERE kind='receipt'").fetchone()
            if not setting:
                raise TerminalError("Configure impressora de recibos antes", 409)
            job_id = self._queue_print(conn, "receipt", receipt_bytes(snapshot,
                58 if setting["format"] == "escpos58" else 80), setting)
            self._audit(conn, operator_id, "sale.reprint", sale_id)
            return {"id": job_id, "status": "queued"}

    def print_jobs(self):
        with self.connection() as conn:
            return [dict(row) for row in conn.execute("""SELECT id,kind,driver,target,status,attempts,error,created_at,
                updated_at FROM print_jobs ORDER BY created_at DESC LIMIT 100""")]

    def begin_print(self):
        with self.transaction() as conn:
            while True:
                row = conn.execute("SELECT * FROM print_jobs WHERE status='queued' ORDER BY created_at,rowid LIMIT 1").fetchone()
                if not row:
                    return None
                # Preserve old jobs for inspection, but never send the retired
                # ESC/POS label payload (which cuts through die-cut labels).
                if row["kind"] == "label" and row["payload"].startswith(b"\x1b@"):
                    conn.execute("UPDATE print_jobs SET status='blocked',error=?,updated_at=? WHERE id=?",
                                 (LABEL_PRINTER_REQUIRED, _now_ms(), row["id"]))
                    continue
                break
            conn.execute("UPDATE print_jobs SET status='sending',attempts=attempts+1,updated_at=? WHERE id=?",
                         (_now_ms(), row["id"]))
            return dict(row)

    def finish_print(self, job_id, error=None):
        with self.transaction() as conn:
            conn.execute("UPDATE print_jobs SET status=?,error=?,updated_at=? WHERE id=?",
                         ("uncertain" if error else "printed", str(error)[:300] if error else None,
                          _now_ms(), job_id))

    def retry_print(self, operator_id, job_id):
        with self.transaction() as conn:
            row = conn.execute("SELECT status,kind,payload FROM print_jobs WHERE id=?", (job_id,)).fetchone()
            if not row:
                raise TerminalError("Impressão não encontrada", 404)
            if row["kind"] == "label" and row["payload"].startswith(b"\x1b@"):
                raise TerminalError(LABEL_PRINTER_REQUIRED, 409)
            if row["status"] not in ("uncertain", "printed"):
                raise TerminalError("Impressão já está na fila", 409)
            conn.execute("UPDATE print_jobs SET status='queued',error=NULL,updated_at=? WHERE id=?",
                         (_now_ms(), job_id))
            self._audit(conn, operator_id, "print.retry.confirmed", job_id)
            return {"id": job_id, "status": "queued", "warning": "Pode gerar impressão duplicada"}
