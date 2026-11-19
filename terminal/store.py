"""SQLite state and business rules for a single offline terminal."""

import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import threading
import time
import unicodedata
import uuid
from datetime import datetime
from collections import defaultdict
from contextlib import closing, contextmanager
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

from . import paths
from .labels import SCALE_SCHEMA
from . import cash_reports
from . import nfce
from .cash_reports import METHOD_LABELS
from .printers import (LABEL_PRINTER_REQUIRED, PAYMENT_LABELS, label_bytes, layout_bytes, receipt_bytes,
                       receipt_layout, voucher_layout)


# Offline lease of a cloud that does not issue long leases (72h; past_due is 24h).
OFFLINE_LEASE_SECONDS = 72 * 60 * 60
# The operator is warned when the offline lease has this much time left or less...
LEASE_WARNING_SECONDS = 7 * 24 * 60 * 60
# ...but only once the lease is this old: an online terminal renews it all the time.
LEASE_WARNING_MIN_AGE_SECONDS = 6 * 60 * 60
# An expired lease never interrupts a cashier that is already open, up to this limit.
EXPIRED_SHIFT_GRACE_SECONDS = 24 * 60 * 60
# last_seen is persisted at most this often (it used to be written on every read).
LAST_SEEN_WRITE_SECONDS = 60
CORRUPTION_HINTS = ("malformed", "not a database")
SELL_ROLES = {"owner", "admin", "manager", "cashier"}
MANAGER_ROLES = {"owner", "admin", "manager"}
CREDIT_METHODS = ("credit_account", "store_credit")
BUILTIN_NAMES = {"cash": "Dinheiro", "pix": "Pix", "card_credit": "Crédito", "card_debit": "Débito",
                 "credit_account": "A prazo (fiado)", "store_credit": "Crédito na loja"}
BUILTIN_PAYMENTS = tuple(BUILTIN_NAMES)
REFUND_METHODS = ("cash", "store_credit", "account")
REFUND_LABELS = {"cash": "Dinheiro", "store_credit": "Crédito na loja", "account": "Abatido do fiado"}
# /api/products answers at most this many rows and reports the total found.
PRODUCT_PAGE_SIZE = 150


def _qty_text(milli, unit):
    if unit == "UN":
        return str(milli // 1000)
    return f"{milli / 1000:.3f}".rstrip("0").rstrip(".").replace(".", ",") + " " + unit


def _brl(cents):
    return "R$ " + f"{cents / 100:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _return_amount(line_total_cents, line_quantity_milli, returned_milli):
    """Same half-up rule as the cloud (credit_logic.return_line_amount)."""
    return (2 * line_total_cents * returned_milli + line_quantity_milli) // (2 * line_quantity_milli)


DISCOUNT_BPS = {"cashier": 1000, "manager": 5000, "admin": 10000, "owner": 10000}
# Same limits the cloud enforces on a sale event (cloud/core/sale_logic.py).
MAX_SALE_ITEMS = 200
MAX_SALE_PAYMENTS = 20
PRODUCT_NAME_MAX = 180
# Manager authorizations lock per requesting operator; the store-wide counter
# only stops a brute force spread over several operators.
MANAGER_OVERRIDE_KEY = "manager_override"
MANAGER_OVERRIDE_FAILURES = 5
MANAGER_OVERRIDE_GLOBAL_FAILURES = 30
MANAGER_OVERRIDE_GLOBAL_WINDOW_SECONDS = 15 * 60
MANAGER_OVERRIDE_LOCK_SECONDS = 300
BUSINESS_DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
# Recovery of the shop on a new computer (phase 2). Everything lives in `meta`.
RESTORE_META = ("restore_state", "restore_cursor", "restore_progress", "restore_pass", "restore_attempts",
                "restore_paused", "restore_error", "restore_started_at", "restore_finished_at",
                "restore_totals", "restore_import")
PHASE2_META = RESTORE_META + ("mirror_key", "cloud_capabilities", "transferred_at", "send_only_until",
                              "send_only_closed", "config_synced_hash")
RESTORE_BLOCKED = "Este caixa ainda está trazendo os dados da loja. Aguarde terminar para vender."
RESTORE_ABANDON_PENDING = ("Ainda há vendas da cópia de segurança esperando para subir ao servidor. "
                           "Ligue a internet e aguarde; se não resolver, chame o suporte.")
RESTORE_MAX_ATTEMPTS = 3
RESTORE_MISMATCH = ("Os dados trazidos não bateram com os do servidor. Toque em \"Tentar de novo\"; "
                    "se continuar, chame o suporte.")
TRANSFERRED_REASON = ("Este caixa foi transferido para outro computador. "
                      "As vendas que faltavam enviar continuam subindo.")
TRANSFERRED_CLOSED_REASON = ("Este caixa foi transferido para outro computador. "
                             "O prazo de envio deste computador terminou.")
TRANSFERRED_STRANDED_REASON = ("Este caixa foi transferido para outro computador. O prazo para este computador "
                               "enviar terminou e {count} registro(s) ficaram guardados só aqui. Não apague este "
                               "computador: use a cópia de segurança no computador novo ou chame o suporte.")
RESTORE_COUNTERS = ("position", "total", "sales", "sales_cents", "returns", "returns_cents", "credit_receipts",
                    "credit_receipts_cents", "cash_sessions", "cash_movements", "cash_movements_cents", "config")
# Same values the cloud accepts in the terminal.config event.
CONFIG_DRIVERS = ("tcp", "windows_raw", "cups", "file")
CONFIG_FORMATS = {"receipt": ("escpos58", "escpos80"), "label": ("zpl", "epl")}
# PPLA/TSPL só existem neste computador: a nuvem ainda não aceita esses formatos no terminal.config.
LABEL_FORMATS = ("zpl", "epl", "ppla", "tspl")
EVENT_ID = re.compile(r"[0-9a-f]{32}")
# Phase 3: additional cashiers ("estações") of the same shop. The computer that owns this
# SQLite file is always the station "server"; the others reach it through the LAN listener.
SERVER_STATION_ID = "server"
SERVER_STATION_NAME = "Caixa principal"
STATION_NAME_MAX = 40
STATION_ENROLL_TTL_SECONDS = 300
STATION_ENROLL_MAX_FAILURES = 5
STATION_ONLINE_SECONDS = 90
SERVER_ONLY_MESSAGE = "Esta operação só pode ser feita no caixa principal da loja."
STATION_TABLES = ("cash_sessions", "sales", "sale_returns", "credit_receipts", "print_jobs")
STATION_LIMIT_NONE = "O plano desta loja não inclui caixas adicionais. Fale com o suporte para ampliar o plano."
STATION_LIMIT_FULL = ("O plano desta loja inclui {n} caixa(s) adicional(is) e todos já estão em uso. Remova um "
                      "caixa em Sistema → Caixas da loja ou amplie o plano.")
STATION_NOT_HUB = ("Este computador não é o caixa principal desta loja. No painel, em Terminais, escolha "
                   "\"Tornar principal desta loja\" ou adicione os caixas no computador principal.")
STATION_NOT_HUB_STATION = ("O computador a que este caixa está ligado deixou de ser o caixa principal desta loja. "
                           "No painel, em Terminais, confira qual é o caixa principal.")
SUPERSEDED_REASON = ("Outro computador assumiu como caixa principal desta loja. Este computador não vende mais: "
                     "use o novo caixa principal e chame o suporte para trazer as vendas que ficaram aqui.")
STATION_CODE_INVALID = "Código inválido ou vencido. Peça um novo código no caixa principal."
PROMOTION_META = ("promotion_state", "promoted_at", "promotion_previous_station", "promotion_device_id",
                  "server_generation")


class TerminalError(Exception):
    def __init__(self, message, status=400, code=None):
        super().__init__(message)
        self.status = status
        # Stable machine-readable reason (station_limit, server_only...), when there is one.
        self.code = code


def _now_ms():
    return time.time_ns() // 1_000_000


def _mirror_call(name, *args):
    """Hook into the second-disk copy (terminal/mirror.py), when this build has it.

    Imported late and fully guarded: the copy must never break a sale.
    """
    try:
        from . import mirror
        return getattr(mirror, name)(*args)
    except Exception:  # ImportError included
        return None


def _int(value):
    return value if type(value) is int else 0


def _config_hash(data):
    """Identity of a terminal.config payload, ignoring when it was produced."""
    return hashlib.sha256(_json({key: value for key, value in data.items()
                                 if key != "updated_at"}).encode()).hexdigest()


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


def _optional_text(value, maximum):
    """Optional catalog text: anything unusable becomes None instead of an error."""
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()[:maximum]



def _product_out(row):
    """Product row for the browser: SQLite keeps stock_controlled as 0/1, the screen expects true/false."""
    product = dict(row)
    if "stock_controlled" in product:
        product["stock_controlled"] = bool(product["stock_controlled"])
    return product

def _fold(value):
    """Lowercase without accents: SQLite LIKE only folds ASCII letters."""
    text = unicodedata.normalize("NFKD", str(value or ""))
    return "".join(char for char in text if not unicodedata.combining(char)).casefold()


def _search_text(name, sku):
    return _fold(name) + "\x1f" + _fold(sku)


def _like(value):
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _catalog_fiscal(value):
    """Fiscal block of a catalog product -> (JSON text, ready flag). Absent or malformed: unknown."""
    if not isinstance(value, dict):
        return "", None
    ready = value.get("ready")
    text = _json({"profile_id": value.get("profile_id") if isinstance(value.get("profile_id"), str) else None,
                  "fields": value.get("fields") if isinstance(value.get("fields"), dict) else {},
                  "missing": [item for item in value.get("missing", []) if isinstance(item, str)][:5]
                  if isinstance(value.get("missing"), list) else []})
    if len(text) > 8000:
        return "", None
    return text, (int(ready) if type(ready) is bool else None)


def _line_gross(price_cents, quantity_milli):
    return int((Decimal(price_cents) * Decimal(quantity_milli) / Decimal(1000)).quantize(
        Decimal("1"), rounding=ROUND_HALF_UP))


def _allocate_discount(lines, discount, base_total):
    """Apportion the manual discount over the promotional price of each line,
    proportionally and without taking a line below zero."""
    net = [line["gross_cents"] - line.get("promo_discount_cents", 0) for line in lines]
    shares = [divmod(discount * value, base_total) for value in net]
    allocated = [share[0] for share in shares]
    remaining = discount - sum(allocated)
    order = sorted(range(len(lines)), key=lambda index: (-shares[index][1], index))
    for index in order:
        if not remaining:
            break
        if allocated[index] < net[index]:
            allocated[index] += 1
            remaining -= 1
    if remaining:
        raise TerminalError("Não foi possível distribuir o desconto")
    for line, value, portion in zip(lines, net, allocated):
        line["discount_cents"] = portion
        line["line_total_cents"] = value - portion


class Store:
    def __init__(self, path):
        self.path = Path(path)
        # Per thread: did the current transaction add something to the outbox?
        self._local = threading.local()
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
                CREATE TABLE IF NOT EXISTS credit_receipts(
                    id TEXT PRIMARY KEY,customer_id TEXT NOT NULL,session_id TEXT NOT NULL,method TEXT NOT NULL,
                    amount_cents INTEGER NOT NULL,operator_id TEXT NOT NULL,created_at INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS suspended_carts(
                    id TEXT PRIMARY KEY,label TEXT NOT NULL,cart TEXT NOT NULL,
                    operator_id TEXT NOT NULL,created_at INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS outbox(
                    id TEXT PRIMARY KEY,event_type TEXT NOT NULL,aggregate_id TEXT NOT NULL,
                    payload TEXT NOT NULL,created_at INTEGER NOT NULL,acked_at INTEGER,seq INTEGER);
                CREATE INDEX IF NOT EXISTS outbox_pending ON outbox(acked_at,created_at);
                CREATE INDEX IF NOT EXISTS outbox_pending_order ON outbox(acked_at);
                CREATE TABLE IF NOT EXISTS audit(
                    id TEXT PRIMARY KEY,operator_id TEXT,action TEXT NOT NULL,
                    subject_id TEXT,detail TEXT,created_at INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS sync_conflicts(
                    id TEXT PRIMARY KEY,event_id TEXT NOT NULL,event_type TEXT NOT NULL,
                    aggregate_id TEXT NOT NULL,reason TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open',detail TEXT NOT NULL,
                    created_at INTEGER NOT NULL,resolved_at INTEGER,
                    resolution TEXT NOT NULL DEFAULT '',UNIQUE(event_id,reason));
                CREATE INDEX IF NOT EXISTS sync_conflicts_status ON sync_conflicts(status,created_at);
                CREATE TABLE IF NOT EXISTS sync_quarantine(
                    event_id TEXT PRIMARY KEY,event_type TEXT NOT NULL,aggregate_id TEXT NOT NULL,
                    reason TEXT NOT NULL,message TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open',
                    created_at INTEGER NOT NULL,resolved_at INTEGER);
                CREATE TABLE IF NOT EXISTS print_settings(
                    kind TEXT PRIMARY KEY,driver TEXT NOT NULL,target TEXT NOT NULL,format TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS print_jobs(
                    id TEXT PRIMARY KEY,kind TEXT NOT NULL,payload BLOB NOT NULL,
                    driver TEXT NOT NULL,target TEXT NOT NULL,status TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,error TEXT,created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL);
            """)
            conn.executescript(SCALE_SCHEMA)  # balança: etiquetas impressas e validade por produto
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
            if "fiscal" not in columns:
                # Resolved tax profile from the cloud (JSON) and NFC-e readiness: 1, 0 or NULL (unknown,
                # catalog from a cloud without fiscal data). Never blocks a sale.
                conn.execute("ALTER TABLE products ADD COLUMN fiscal TEXT NOT NULL DEFAULT ''")
                conn.execute("ALTER TABLE products ADD COLUMN fiscal_ready INTEGER")
            if "search_text" not in columns:
                conn.execute("ALTER TABLE products ADD COLUMN search_text TEXT NOT NULL DEFAULT ''")
                for row in conn.execute("SELECT id,name,sku FROM products").fetchall():
                    conn.execute("UPDATE products SET search_text=? WHERE id=?",
                                 (_search_text(row["name"], row["sku"]), row["id"]))
            sales_columns = {row["name"] for row in conn.execute("PRAGMA table_info(sales)")}
            if "customer_document" not in sales_columns:
                conn.execute("ALTER TABLE sales ADD COLUMN customer_document TEXT")
            conflict_columns = {row["name"] for row in conn.execute("PRAGMA table_info(sync_conflicts)")}
            if "resolution" not in conflict_columns:
                conn.execute("ALTER TABLE sync_conflicts ADD COLUMN resolution TEXT NOT NULL DEFAULT ''")
            conn.commit()
            self._migrate_conflict_key(conn)
            self._migrate_outbox_seq(conn)
            self._migrate_stations(conn)
            self._migrate_shifts(conn)
            nfce.migrate(conn)  # NFC-e per sale and the contingency number block (terminal/nfce.py)
        try:
            self.path.chmod(0o600)
        except OSError:
            pass
        # A previous process may have sent bytes before crashing. Never retry it silently.
        # Only the queue of this computer: each additional cashier recovers its own (recover_print).
        with self.transaction() as conn:
            conn.execute("UPDATE print_jobs SET status='uncertain',error='Processo interrompido durante impressão' "
                         "WHERE status='sending' AND station_id=?", (SERVER_STATION_ID,))

    @staticmethod
    def is_corruption(exc):
        """True only for errors SQLite raises when the file itself is damaged."""
        if not isinstance(exc, sqlite3.DatabaseError) or isinstance(
                exc, (sqlite3.IntegrityError, sqlite3.ProgrammingError, sqlite3.InterfaceError)):
            return False
        return any(hint in str(exc).lower() for hint in CORRUPTION_HINTS)

    @staticmethod
    def quick_check(path):
        """None when the SQLite file is healthy (or absent); otherwise what is wrong with it."""
        path = Path(path)
        if not path.exists():
            return None
        try:
            with closing(sqlite3.connect(path, timeout=10)) as conn:
                rows = [str(row[0]) for row in conn.execute("PRAGMA quick_check(5)")]
        except sqlite3.DatabaseError as exc:
            # Locked, unreadable or I/O trouble is not damage: let the start fail instead.
            if not Store.is_corruption(exc):
                raise
            return str(exc) or type(exc).__name__
        return None if rows == ["ok"] else "; ".join(rows)[:300] or "quick_check sem resposta"

    @staticmethod
    def local_backups(path):
        """Backup copies next to a database, newest first, without opening the database."""
        directory = Path(path).parent / "backups"
        copies = sorted(directory.glob("terminal-*.sqlite3"), reverse=True) if directory.exists() else []
        result = []
        for copy in copies[:7]:
            try:
                stamp = int(copy.name.split("-")[1])
            except (IndexError, ValueError):
                stamp = None
            result.append({"name": copy.name, "path": str(copy), "size_bytes": copy.stat().st_size,
                           "created_at": stamp})
        return result

    @staticmethod
    def _migrate_outbox_seq(conn):
        """Delivery order is an explicit counter: the clock may run backwards."""
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(outbox)")}
        conn.execute("BEGIN IMMEDIATE")
        try:
            if "seq" not in columns:
                conn.execute("ALTER TABLE outbox ADD COLUMN seq INTEGER")
            if conn.execute("SELECT 1 FROM outbox WHERE seq IS NULL LIMIT 1").fetchone():
                # rowid is the insertion order every reader already used; created_at only breaks
                # ties that cannot exist. Rows numbered by a previous run keep their place.
                last = conn.execute("SELECT COALESCE(MAX(seq),0) FROM outbox").fetchone()[0]
                missing = [row[0] for row in conn.execute(
                    "SELECT rowid FROM outbox WHERE seq IS NULL ORDER BY rowid,created_at")]
                conn.executemany("UPDATE outbox SET seq=? WHERE rowid=?",
                                 [(last + offset, rowid) for offset, rowid in enumerate(missing, start=1)])
            conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS outbox_seq ON outbox(seq)")
            conn.execute("CREATE INDEX IF NOT EXISTS outbox_pending_seq ON outbox(acked_at,seq)")
            conn.commit()
        except BaseException:
            conn.rollback()
            raise

    @staticmethod
    def _migrate_stations(conn):
        """Phase 3: one cashier per station. Idempotent; running it twice changes nothing.

        station_id always arrives by ALTER TABLE (never in the CREATE TABLE text), so a new
        database and a migrated one keep the same column order. After this migration the
        previous executable can no longer open a cashier or sell (its positional INSERTs
        do not know the new column); the updater's rollback does not restore the database.
        """
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute("""CREATE TABLE IF NOT EXISTS stations(
                id TEXT PRIMARY KEY, name TEXT NOT NULL, token_hash TEXT UNIQUE,
                created_at INTEGER NOT NULL, last_seen_at INTEGER, revoked_at INTEGER,
                hostname TEXT NOT NULL DEFAULT '', app_version TEXT NOT NULL DEFAULT '',
                last_address TEXT NOT NULL DEFAULT '')""")
            conn.execute("""CREATE TABLE IF NOT EXISTS station_enroll_codes(
                code_hash TEXT PRIMARY KEY, created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL,
                created_by TEXT, station_name TEXT NOT NULL DEFAULT '',
                failures INTEGER NOT NULL DEFAULT 0, used_at INTEGER)""")
            conn.execute("INSERT OR IGNORE INTO stations(id,name,token_hash,created_at) VALUES(?,?,NULL,?)",
                         (SERVER_STATION_ID, SERVER_STATION_NAME, _now_ms()))
            for table in STATION_TABLES:
                if "station_id" not in {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN station_id TEXT NOT NULL DEFAULT 'server'")
            if "station_id" not in {row["name"] for row in conn.execute("PRAGMA table_info(print_settings)")}:
                # One printer setup per (station, kind): the primary key changes, so the table is rebuilt.
                conn.execute("ALTER TABLE print_settings RENAME TO print_settings_old")
                conn.execute("""CREATE TABLE print_settings(
                    kind TEXT NOT NULL, driver TEXT NOT NULL, target TEXT NOT NULL, format TEXT NOT NULL,
                    station_id TEXT NOT NULL DEFAULT 'server', PRIMARY KEY(station_id,kind))""")
                conn.execute("""INSERT INTO print_settings(kind,driver,target,format,station_id)
                                SELECT kind,driver,target,format,'server' FROM print_settings_old""")
                conn.execute("DROP TABLE print_settings_old")
            # The old index allowed a single open cashier in the whole file.
            conn.execute("DROP INDEX IF EXISTS cash_one_open")
            conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS cash_one_open_station "
                         "ON cash_sessions(station_id) WHERE closed_at IS NULL")
            conn.execute("CREATE INDEX IF NOT EXISTS print_jobs_station ON print_jobs(station_id,status,created_at)")
            conn.commit()
        except BaseException:
            conn.rollback()
            raise

    @staticmethod
    def _migrate_shifts(conn):
        """Cashier per operator shift: pauses and who closed it. Idempotent.

        A database from before this change keeps its open cashier: it already belongs to the
        operator that opened it (cash_sessions.operator_id), so it simply becomes that
        operator's shift, with no pause.
        """
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute("""CREATE TABLE IF NOT EXISTS cash_pauses(
                id TEXT PRIMARY KEY, session_id TEXT NOT NULL, operator_id TEXT NOT NULL,
                reason TEXT NOT NULL DEFAULT '', started_at INTEGER NOT NULL, ended_at INTEGER,
                resumed_by TEXT)""")
            conn.execute("CREATE INDEX IF NOT EXISTS cash_pauses_session ON cash_pauses(session_id,started_at)")
            conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS cash_pauses_one_open "
                         "ON cash_pauses(session_id) WHERE ended_at IS NULL")
            # Who closed and who authorized, apart: cash_sessions keeps the columns an older
            # executable knows (the updater's rollback does not restore the database).
            conn.execute("""CREATE TABLE IF NOT EXISTS cash_closings(
                session_id TEXT PRIMARY KEY, closed_by TEXT, supervisor_id TEXT)""")
            conn.commit()
        except BaseException:
            conn.rollback()
            raise

    # ---------- phase 3: which cashier is asking (kept per thread, never a method parameter) ----------

    def set_station(self, station_id):
        """Station of the request being served by this thread; None goes back to the server."""
        self._local.station_id = station_id if isinstance(station_id, str) and station_id else SERVER_STATION_ID

    def current_station(self):
        return getattr(self._local, "station_id", None) or SERVER_STATION_ID

    @contextmanager
    def station(self, station_id):
        previous = self.current_station()
        self.set_station(station_id)
        try:
            yield self
        finally:
            self.set_station(previous)

    @staticmethod
    def _station_name(conn, station_id):
        row = conn.execute("SELECT name FROM stations WHERE id=?", (station_id,)).fetchone()
        return row[0] if row else ""

    def station_name(self, station_id=None):
        with self.connection() as conn:
            return self._station_name(conn, station_id or self.current_station())

    def _print_setting(self, conn, kind):
        """Printer of the current cashier for receipts or labels, if it has one."""
        return conn.execute("SELECT * FROM print_settings WHERE kind=? AND station_id=?",
                            (kind, self.current_station())).fetchone()

    @staticmethod
    def _migrate_conflict_key(conn):
        """Older databases allowed one conflict per event; the cloud sends one per reason."""
        unique = [[column["name"] for column in conn.execute(f"PRAGMA index_info('{index['name']}')")]
                  for index in conn.execute("PRAGMA index_list(sync_conflicts)").fetchall() if index["unique"]]
        if ["event_id"] not in unique:
            return
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute("ALTER TABLE sync_conflicts RENAME TO sync_conflicts_old")
            conn.execute("""CREATE TABLE sync_conflicts(
                id TEXT PRIMARY KEY,event_id TEXT NOT NULL,event_type TEXT NOT NULL,
                aggregate_id TEXT NOT NULL,reason TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'open',detail TEXT NOT NULL,
                created_at INTEGER NOT NULL,resolved_at INTEGER,
                resolution TEXT NOT NULL DEFAULT '',UNIQUE(event_id,reason))""")
            conn.execute("""INSERT INTO sync_conflicts
                (id,event_id,event_type,aggregate_id,reason,status,detail,created_at,resolved_at,resolution)
                SELECT id,event_id,event_type,aggregate_id,reason,status,detail,created_at,resolved_at,resolution
                FROM sync_conflicts_old""")
            conn.execute("DROP TABLE sync_conflicts_old")
            conn.execute("CREATE INDEX IF NOT EXISTS sync_conflicts_status ON sync_conflicts(status,created_at)")
            conn.commit()
        except BaseException:
            conn.rollback()
            raise

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
            self._local.outbox_dirty = False
            try:
                yield conn
                conn.commit()
            except BaseException:
                self._local.outbox_dirty = False
                conn.rollback()
                raise
        if getattr(self._local, "outbox_dirty", False):
            self._local.outbox_dirty = False
            # Only after the commit: the copy reads the queue from the table.
            _mirror_call("on_event", self)

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
        station = self.current_station()
        if station != SERVER_STATION_ID:
            # Which additional cashier did it; the main one is the default and says nothing.
            mark = "caixa=" + (self._station_name(conn, station) or station)
            detail = f"{detail}; {mark}" if detail else mark
        conn.execute("INSERT INTO audit VALUES (?,?,?,?,?,?)",
                     (uuid.uuid4().hex, operator_id, action, subject_id, detail, _now_ms()))

    def _has_business_history(self, conn):
        # An old company's sales, receipts or audit trail must never become
        # visible to the next company's operators through this SQLite file.
        if any((self.path.parent / "backups").glob("terminal-*.sqlite3")):
            return True
        for table in ("sales", "cash_sessions", "cash_movements", "suspended_carts", "print_jobs"):
            if conn.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone():
                return True
        # terminal.config goes out by itself right after pairing: it is setup, not counter history.
        if conn.execute("SELECT 1 FROM outbox WHERE event_type != 'terminal.config' LIMIT 1").fetchone():
            return True
        return conn.execute("""SELECT 1 FROM audit
            WHERE action NOT IN ('terminal.paired', 'terminal.bootstrap',
                                 'terminal.unpaired', 'terminal.catalog_skipped',
                                 'terminal.clock_corrected', 'terminal.release_key_pinned') LIMIT 1""").fetchone() is not None

    @staticmethod
    def _clear_company_cache(conn):
        # These are downloaded/setup data, not locally created business records.
        # Clearing them also makes exports after a new pairing tenant-isolated.
        for table in ("products", "operators", "customers", "login_attempts", "audit", "print_settings",
                      "sync_conflicts", "sync_quarantine", "station_enroll_codes", "scale_labels",
                      "scale_product_settings", "nfce_reservation"):
            conn.execute(f"DELETE FROM {table}")
        # The additional cashiers belonged to the company that is leaving this file.
        conn.execute("DELETE FROM stations WHERE id != ?", (SERVER_STATION_ID,))

    @staticmethod
    def _clear_connection_meta(conn):
        for key in ("token", "tenant_id", "tenant_name", "shop_id", "shop_name",
                    "terminal_id", "terminal_name", "license_hint", "lease", "public_key", "cloud_url",
                    "discount_max_bps", "issuer_profile", "business_hours", "bootstrap_at", "last_seen", "last_sync_at", "last_sync_error",
                    "release_info", "release_error", "release_checked_at", "last_backup_at",
                    "last_heartbeat_at", "last_heartbeat_error", "bootstrap_summary", "catalog_skipped",
                    "release_public_key", "lease_renewed_at", "clock_skew", "clock_checked_at",
                    "superseded_at", "superseded_by", "nfce_config", *PHASE2_META,
                    *PROMOTION_META):
            conn.execute("DELETE FROM meta WHERE key=?", (key,))
        conn.execute("DELETE FROM meta WHERE key LIKE 'manager_override_window|%'")

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
        if "stations" in self._meta_json(self._meta(conn, "cloud_capabilities"), []):
            # Outside `data` on purpose: the cloud hashes `data`, and an older cloud refuses the key.
            station = self.current_station()
            event["station"] = {"id": station, "name": self._station_name(conn, station)}
        # Callers hold BEGIN IMMEDIATE, so the next number cannot be taken twice.
        conn.execute("""INSERT INTO outbox(id,event_type,aggregate_id,payload,created_at,acked_at,seq)
                        VALUES (?,?,?,?,?,NULL,(SELECT COALESCE(MAX(seq),0)+1 FROM outbox))""",
                     (event_id, event_type, aggregate_id, _json(event), _now_ms()))
        self._local.outbox_dirty = True
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
        setting = self._print_setting(conn, "receipt")
        width = 58 if setting and setting["format"] == "escpos58" else 80
        result["receipt"] = receipt_layout(result, width)
        return result

    def pending_events(self, limit=50):
        with self.connection() as conn:
            return [json.loads(row["payload"]) for row in conn.execute(
                "SELECT payload FROM outbox WHERE acked_at IS NULL ORDER BY seq LIMIT ?", (limit,))]

    def ack_events(self, ids):
        with self.transaction() as conn:
            for event_id in ids:
                conn.execute("UPDATE outbox SET acked_at=COALESCE(acked_at,?) WHERE id=?", (_now_ms(), event_id))

    def sync_status(self):
        with self.connection() as conn:
            release = self._meta(conn, "release_info")
            return {"pending": conn.execute("SELECT COUNT(*) FROM outbox WHERE acked_at IS NULL").fetchone()[0],
                    "conflicts": conn.execute("SELECT COUNT(*) FROM sync_conflicts WHERE status IN ('open','acknowledged')").fetchone()[0],
                    # Events the cloud kept for review instead of applying.
                    "quarantined": conn.execute("SELECT COUNT(*) FROM sync_quarantine WHERE status='open'").fetchone()[0],
                    "catalog_skipped": len(self._catalog_skipped(conn)),
                    "last_sync_at": self._meta(conn, "last_sync_at"),
                    "last_sync_error": self._meta(conn, "last_sync_error"),
                    "last_heartbeat_at": self._meta(conn, "last_heartbeat_at"),
                    "last_heartbeat_error": self._meta(conn, "last_heartbeat_error"),
                    "bootstrap_at": self._meta(conn, "bootstrap_at"),
                    "release": json.loads(release) if release else None,
                    "release_error": self._meta(conn, "release_error"),
                    # NFC-e waiting for the cloud/SEFAZ (contingency must be sent within 24 h).
                    "nfce": nfce.summary(conn)}

    def set_sync_result(self, error=None):
        with self.transaction() as conn:
            self._set_meta(conn, "last_sync_error", error or "")
            if error is None:
                self._set_meta(conn, "last_sync_at", str(_now_ms()))

    def clear_sync_error(self):
        """A successful cloud contact proves the terminal is online again.

        While events are still waiting, only their delivery clears the error.
        """
        with self.connection() as conn:
            if not self._meta(conn, "last_sync_error"):
                return
        with self.transaction() as conn:
            if not conn.execute("SELECT 1 FROM outbox WHERE acked_at IS NULL LIMIT 1").fetchone():
                self._set_meta(conn, "last_sync_error", "")

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
                # One event may come back with several conflicts, one per reason.
                event_id, reason = str(conflict["event_id"]), str(conflict.get("reason") or "unknown")
                values = (str(conflict.get("event_type") or ""), str(conflict.get("aggregate_id") or ""),
                          _json(conflict.get("detail") or {}))
                if conn.execute("UPDATE sync_conflicts SET event_type=?,aggregate_id=?,detail=? "
                                "WHERE event_id=? AND reason=?", (*values, event_id, reason)).rowcount:
                    continue
                conflict_id = str(conflict.get("id") or _uuid())
                if conn.execute("SELECT 1 FROM sync_conflicts WHERE id=?", (conflict_id,)).fetchone():
                    conflict_id = _uuid()
                conn.execute(
                    """INSERT INTO sync_conflicts
                       (id,event_id,event_type,aggregate_id,reason,status,detail,created_at,resolved_at)
                       VALUES (?,?,?,?,?,?,?,?,NULL)""",
                    (conflict_id, event_id, values[0], values[1], reason,
                     str(conflict.get("status") or "open"), values[2], now))

    def record_quarantined(self, entries, events):
        """Keep track of events the cloud accepted for review instead of applying.

        The cloud lists them in accepted_event_ids too, so the outbox moves on;
        this local record is what tells the operator that something needs review.
        """
        if not isinstance(entries, list):
            return []
        sent = {event["event_id"]: event for event in events}
        recorded, now = [], _now_ms()
        with self.transaction() as conn:
            for entry in entries:
                event = sent.get(entry.get("event_id")) if isinstance(entry, dict) else None
                if event is None:
                    continue
                reason = str(entry.get("reason") or "rejected_event")[:120]
                message = str(entry.get("message") or "")[:500]
                row = {"event_id": event["event_id"], "event_type": event["type"],
                       "aggregate_id": event["aggregate_id"], "reason": reason, "message": message}
                recorded.append(row)
                inserted = conn.execute(
                    "INSERT OR IGNORE INTO sync_quarantine VALUES (?,?,?,?,?,'open',?,NULL)",
                    (row["event_id"], row["event_type"], row["aggregate_id"], reason, message, now)).rowcount
                if not inserted:
                    continue
                # Also listed with the offline conflicts the manager already reviews.
                conn.execute(
                    """INSERT INTO sync_conflicts
                       (id,event_id,event_type,aggregate_id,reason,status,detail,created_at,resolved_at)
                       VALUES (?,?,?,?,'rejected_event','open',?,?,NULL)
                       ON CONFLICT(event_id,reason) DO NOTHING""",
                    (_uuid(), row["event_id"], row["event_type"], row["aggregate_id"],
                     _json({"reason": reason, "message": message}), now))
                self._audit(conn, None, "sync.event_quarantined", row["aggregate_id"],
                            f"{row['event_type']}; {reason}; {message}"[:300])
        return recorded

    def quarantined_events(self, status="open"):
        if status not in (None, "open", "resolved"):
            raise TerminalError("Estado de revisão inválido")
        with self.connection() as conn:
            query = "SELECT * FROM sync_quarantine" + (" WHERE status=?" if status else "")
            return [dict(row) for row in conn.execute(query + " ORDER BY created_at DESC,rowid DESC LIMIT 200",
                                                      (status,) if status else ())]

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
            row = conn.execute("SELECT id,event_id,reason FROM sync_conflicts WHERE id=?",
                               (str(conflict_id),)).fetchone()
            if not row:
                raise TerminalError("Conflito não encontrado", 404)
            conn.execute("UPDATE sync_conflicts SET status=?,resolution=?,resolved_at=? WHERE id=?",
                         (status, resolution.strip(), _now_ms(), str(conflict_id)))
            if status in ("resolved", "ignored") and row["reason"] == "rejected_event":
                conn.execute("UPDATE sync_quarantine SET status='resolved',resolved_at=? WHERE event_id=? AND status='open'",
                             (_now_ms(), row["event_id"]))
        return self.sync_conflicts()

    def set_release_result(self, payload=None, error=None):
        with self.transaction() as conn:
            self._set_meta(conn, "release_info", _json(payload) if payload is not None else "")
            self._set_meta(conn, "release_error", error or "")
            self._set_meta(conn, "release_checked_at", str(_now_ms()))

    def lease_status(self, now=None):
        """Verify the signed lease and say what this terminal may still do offline.

        can_sell: sales and the other cashier operations. An expired lease keeps
        them for the cashier that is already open (a shift is never cut in the middle).
        can_open_cash: a new cashier needs a lease that is still valid.
        """
        from shared.licensing import CLOCK_TOLERANCE_SECONDS, LicenseError, verify_lease

        now = int(time.time()) if now is None else int(now)
        with self.connection() as conn:
            envelope_raw = self._meta(conn, "lease")
            key = self._meta(conn, "public_key")
            if not envelope_raw or not key:
                return {"state": "unpaired", "can_sell": False, "can_open_cash": False,
                        "reason": "Terminal não pareado"}
            if self._meta(conn, "transferred_at"):
                # The shop moved to another computer: this one only delivers its queue,
                # and only while the cloud still takes it.
                closed = self._meta(conn, "send_only_closed") == "1"
                pending = conn.execute("SELECT COUNT(*) FROM outbox WHERE acked_at IS NULL").fetchone()[0]
                reason = TRANSFERRED_REASON
                if closed:
                    reason = (TRANSFERRED_STRANDED_REASON.format(count=pending) if pending
                              else TRANSFERRED_CLOSED_REASON)
                return {"state": "transferred", "can_sell": False, "can_open_cash": False,
                        "reason": reason, "send_only_closed": closed, "pending_events": pending,
                        "send_only_until": None if closed else self._meta(conn, "send_only_until") or None}
            if self._meta(conn, "superseded_at"):
                # A promoted computer with a newer generation of this same terminal answered on the
                # network: two main cashiers would split the sales of the shop.
                return {"state": "superseded", "can_sell": False, "can_open_cash": False,
                        "reason": SUPERSEDED_REASON,
                        "pending_events": conn.execute(
                            "SELECT COUNT(*) FROM outbox WHERE acked_at IS NULL").fetchone()[0]}
            last_seen = int(self._meta(conn, "last_seen") or 0)
            if last_seen and now + CLOCK_TOLERANCE_SECONDS < last_seen:
                return {"state": "clock_rollback", "can_sell": False, "can_open_cash": False,
                        "reason": "Relógio do computador voltou; ajuste a hora",
                        "clock": {"problem": "behind", "computer_time": now, "reference_time": last_seen}}
            envelope = json.loads(envelope_raw)
            claims = envelope.get("payload", {}) if isinstance(envelope, dict) else {}
            expires, issued = claims.get("expires_at"), claims.get("issued_at")
            invalid = {"state": "invalid", "can_sell": False, "can_open_cash": False, "reason": "Licença inválida"}
            if type(expires) is not int or type(issued) is not int:
                return invalid
            expected = {field: self._meta(conn, field) for field in
                        ("tenant_id", "shop_id", "terminal_id", "device_id")}
            try:
                verify_lease(envelope, key, expected=expected, now=now, last_seen=last_seen or None)
            except LicenseError:
                if now < expires:
                    return dict(invalid, expires_at=expires)
                try:
                    # Past its date, but is it this terminal's own signed lease?
                    verify_lease(envelope, key, expected=expected, now=expires - 1)
                except LicenseError:
                    return dict(invalid, expires_at=expires)
                cash = conn.execute("SELECT 1 FROM cash_sessions WHERE closed_at IS NULL AND station_id=?",
                                    (self.current_station(),)).fetchone()
                grace_until = expires + EXPIRED_SHIFT_GRACE_SECONDS
                shift = bool(cash) and now < grace_until
                result = {"state": "expired", "can_sell": shift, "can_open_cash": False, "expires_at": expires,
                          "reason": ("Licença offline vencida. Este caixa aberto continua vendendo; conecte à "
                                     "internet para renovar antes de abrir o próximo caixa") if shift
                          else "Prazo offline expirado; conecte para renovar"}
                if shift:
                    result["grace_until"] = grace_until
                # The cloud showed the clock was ahead and nobody fixed it since.
                skew = int(self._meta(conn, "clock_skew") or 0)
                checked = int(self._meta(conn, "clock_checked_at") or 0)
                if skew > CLOCK_TOLERANCE_SECONDS and now >= checked - CLOCK_TOLERANCE_SECONDS and now - skew < expires:
                    result["clock"] = {"problem": "ahead", "computer_time": now, "reference_time": now - skew}
                    result["reason"] = "Relógio do computador está adiantado; ajuste a hora"
                return result
            remaining = expires - now
            warning = remaining <= LEASE_WARNING_SECONDS and now - issued >= LEASE_WARNING_MIN_AGE_SECONDS
            days_left = max(0, remaining // 86400)
            if not warning:
                reason = "Licença ativa"
            elif days_left >= 1:
                reason = (f"Licença offline vence em {days_left} dia{'s' if days_left > 1 else ''}; "
                          "conecte este caixa à internet para renovar")
            else:
                reason = "Licença offline vence hoje; conecte este caixa à internet para renovar"
            status = {"state": "active", "can_sell": True, "can_open_cash": True, "reason": reason,
                      "expires_at": expires, "issued_at": issued, "seconds_left": remaining,
                      "days_left": days_left, "warning": warning,
                      "offline_seconds_max": expires - issued,
                      "plan_status": claims.get("status"), "features": claims.get("features", []),
                      "business_hours": claims.get("business_hours", {}),
                      "max_stations": self._claim_stations(claims)}
        if now - last_seen >= LAST_SEEN_WRITE_SECONDS:
            self.touch_last_seen(now)
        return status

    def mark_superseded(self, generation, fingerprint=""):
        """Another computer answered as this same terminal with a newer generation. Returns True
        when this is news. Only a new pairing, a recovery or a promotion clears it."""
        with self.transaction() as conn:
            if self._meta(conn, "superseded_at"):
                return False
            self._set_meta(conn, "superseded_at", str(_now_ms()))
            self._set_meta(conn, "superseded_by", _json({"generation": int(generation),
                                                         "fingerprint": str(fingerprint or "")[:64]}))
            self._audit(conn, None, "terminal.superseded", self._meta(conn, "terminal_id"),
                        f"generation={int(generation)}")
            return True

    def superseded(self):
        with self.connection() as conn:
            return bool(self._meta(conn, "superseded_at"))

    def server_generation(self):
        with self.connection() as conn:
            try:
                return max(1, int(self._meta(conn, "server_generation") or 1))
            except ValueError:
                return 1

    @staticmethod
    def _claim_stations(claims):
        value = claims.get("max_stations") if isinstance(claims, dict) else None
        return value if type(value) is int and value >= 0 else 0

    def touch_last_seen(self, now=None):
        """Advance the anti-rollback mark; it never moves backwards here."""
        now = int(time.time()) if now is None else int(now)
        try:
            with self.transaction() as conn:
                if now > int(self._meta(conn, "last_seen") or 0):
                    self._set_meta(conn, "last_seen", str(now))
        except sqlite3.OperationalError:
            pass  # a busy database only delays the mark; the next read writes it

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
        self.ensure_not_restoring()
        status = self.lease_status()
        if not status["can_sell"]:
            raise TerminalError(status["reason"], 403)

    def _pin_release_key(self, conn, response):
        """The release key is pinned by the first authenticated answer that brings it."""
        from shared.licensing import key_id

        value = response.get("release_public_key") if isinstance(response, dict) else None
        if not isinstance(value, str) or not value.strip() or self._meta(conn, "release_public_key"):
            return
        try:
            fingerprint = key_id(value)
        except ValueError:  # LicenseError included
            return
        self._set_meta(conn, "release_public_key", value)
        self._audit(conn, None, "terminal.release_key_pinned", None, fingerprint)

    def _accept_lease(self, conn, lease, key, server_time=None):
        """Verify a lease the cloud just sent and re-anchor the clock mark on it.

        A lease newer than the stored one is judged at its own signed issue time:
        a wrong computer clock must not stop the renewal, lease_status reports it.
        Anything else (a replay) faces the normal clock checks.
        """
        from shared.licensing import CLOCK_TOLERANCE_SECONDS, LicenseError, verify_lease

        expected = {field: self._meta(conn, field) for field in
                    ("tenant_id", "shop_id", "terminal_id", "device_id")}
        try:
            previous_issued = int(json.loads(self._meta(conn, "lease") or "{}")["payload"]["issued_at"])
        except (KeyError, TypeError, ValueError):
            previous_issued = 0
        try:
            issued = lease["payload"]["issued_at"]
        except (KeyError, TypeError):
            issued = None
        fresh = type(issued) is int and issued > previous_issued
        try:
            claims = verify_lease(lease, key, expected=expected)
        except LicenseError:
            if not fresh:
                raise
            claims = verify_lease(lease, key, expected=expected, now=issued)
        if not fresh:
            return claims
        now, last_seen = int(time.time()), int(self._meta(conn, "last_seen") or 0)
        anchor = issued
        if type(server_time) is int and abs(server_time - issued) <= CLOCK_TOLERANCE_SECONDS:
            anchor = max(anchor, server_time)
        clock_ok = abs(now - anchor) <= CLOCK_TOLERANCE_SECONDS
        if clock_ok and last_seen <= now + CLOCK_TOLERANCE_SECONDS:
            anchor = max(anchor, last_seen)
        elif clock_ok:
            # The clock was ahead and has been corrected: without this the
            # cashier stays blocked until the old mark passes.
            self._audit(conn, None, "terminal.clock_corrected", None, f"last_seen={last_seen}")
        self._set_meta(conn, "last_seen", str(anchor))
        self._set_meta(conn, "clock_skew", str(0 if clock_ok else now - anchor))
        self._set_meta(conn, "clock_checked_at", str(now))
        return claims

    def _store_lease(self, conn, lease, claims):
        max_discount = claims.get("max_discount_bps", 10000)
        if type(max_discount) is not int or not 0 <= max_discount <= 10000:
            raise TerminalError("Limite de desconto da licença inválido", 403)
        self._set_meta(conn, "lease", _json(lease))
        self._set_meta(conn, "discount_max_bps", str(max_discount))
        self._set_meta(conn, "business_hours", _json(claims.get("business_hours") or {}))

    def apply_lease(self, response):
        """Renewal that does not depend on the catalog (POST /api/v1/terminal/lease)."""
        from shared.licensing import LicenseError, payload_digest

        with self.transaction() as conn:
            key = self._meta(conn, "public_key")
            if not key or response.get("public_key") != key:
                raise TerminalError("Chave pública não corresponde à chave fixada", 409)
            lease = response.get("lease")
            try:
                claims = self._accept_lease(conn, lease, key, response.get("server_time"))
            except (LicenseError, TypeError) as exc:
                raise TerminalError("Licença renovada inválida", 403) from exc
            operators = response.get("operators")
            if isinstance(operators, list):
                if claims.get("operators_hash") != payload_digest(operators):
                    raise TerminalError("Lista de operadores não corresponde à licença assinada", 403)
                self._apply_operators(conn, operators, [])
            self._store_lease(conn, lease, claims)
            self._pin_release_key(conn, response)
            self._store_cloud_extras(conn, response, mirror_key=True)
            self._set_meta(conn, "lease_renewed_at", str(_now_ms()))
        return {"expires_at": claims["expires_at"]}

    def mark_lease_attempt(self):
        """Hourly throttle also for a cloud that has no lease route yet."""
        with self.transaction() as conn:
            self._set_meta(conn, "lease_renewed_at", str(_now_ms()))

    def record_audit(self, operator_id, action, subject_id=None, detail=None):
        with self.transaction() as conn:
            self._audit(conn, operator_id, action, subject_id, detail)

    def set_pairing(self, response, cloud_url):
        from shared.licensing import LicenseError

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
            self._clear_company_cache(conn)
            self._clear_connection_meta(conn)
            for key in ("token", "tenant_id", "shop_id", "terminal_id", "public_key"):
                self._set_meta(conn, key, response[key])
            try:
                # The cloud has already handed the terminal to this computer: a wrong clock
                # here must not undo that. Signature and identity are checked as always; the
                # license is judged at its own issue time and lease_status asks for the clock.
                self._accept_lease(conn, response["lease"], response["public_key"], response.get("server_time"))
            except (LicenseError, TypeError) as exc:
                raise TerminalError("Licença do pareamento inválida", 403) from exc
            self._set_meta(conn, "lease", _json(response["lease"]))
            for key, parent in (("tenant_name", "tenant"), ("shop_name", "shop"), ("terminal_name", "terminal")):
                value = response.get(parent, {}).get("name") if isinstance(response.get(parent), dict) else None
                if value:
                    self._set_meta(conn, key, _str(value, key))
            if response.get("license_hint"):
                self._set_meta(conn, "license_hint", _str(response["license_hint"], "Licença", 16))
            if response.get("issuer"):
                self._set_meta(conn, "issuer_profile", _json(response["issuer"]))
            self._set_meta(conn, "cloud_url", cloud_url)
            self._pin_release_key(conn, response)
            self._store_cloud_extras(conn, response, mirror_key=True)
            if response.get("recovery") is True:
                # This terminal already has history in the cloud: bring it back before selling.
                self._set_meta(conn, "restore_state", "pending")
                self._set_meta(conn, "restore_pass", "1")
                self._set_meta(conn, "restore_started_at", str(_now_ms()))
            self._audit(conn, None, "terminal.paired", response["terminal_id"])

    @staticmethod
    def _pending_stock_deltas(conn):
        """Return local stock effects not yet reflected by the cloud balance."""
        deltas = defaultdict(int)
        events = [json.loads(row[0]) for row in conn.execute(
            "SELECT payload FROM outbox WHERE acked_at IS NULL ORDER BY seq")]
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
        from shared.licensing import LicenseError, payload_digest

        products = response.get("products")
        operators = response.get("operators")
        skipped = []
        # The signed license and operators are committed on their own: a bad
        # catalog row must never stop the offline lease from being renewed.
        with self.transaction() as conn:
            key = self._meta(conn, "public_key")
            if not key or response.get("public_key") != key:
                raise TerminalError("Chave pública não corresponde à chave fixada", 409)
            lease = response.get("lease")
            try:
                claims = self._accept_lease(conn, lease, key, response.get("server_time"))
            except (LicenseError, TypeError) as exc:
                raise TerminalError("Licença do catálogo inválida", 403) from exc
            if not isinstance(operators, list):
                raise TerminalError("Catálogo ou operadores inválidos")
            if claims.get("operators_hash") != payload_digest(operators):
                raise TerminalError("Lista de operadores não corresponde à licença assinada", 403)
            self._apply_operators(conn, operators, skipped)
            self._store_lease(conn, lease, claims)
            self._pin_release_key(conn, response)
            self._store_cloud_extras(conn, response)
        if not isinstance(products, list):
            raise TerminalError("Catálogo ou operadores inválidos")
        with self.transaction() as conn:
            timestamp = _now_ms()
            pending_deltas = self._pending_stock_deltas(conn)
            # The cloud permits several products without barcodes and may move a
            # barcode between products. Clear old assignments before upserting.
            # Missing catalog entries remain on disk for sale history, inactive.
            conn.execute("UPDATE products SET barcode=NULL,active=0")
            for product in products:
                try:
                    row = self._catalog_row(product, pending_deltas, timestamp)
                    try:
                        self._upsert_product(conn, row)
                    except sqlite3.IntegrityError:
                        # Barcode still held by another row of this same load.
                        self._upsert_product(conn, {**row, "barcode": None})
                        skipped.append({"kind": "barcode", "id": row["id"],
                                        "error": "Código de barras repetido no catálogo"})
                except (TerminalError, sqlite3.IntegrityError) as exc:
                    skipped.append(self._skipped("product", product, exc))
            customers = response.get("customers")
            if isinstance(customers, list):
                self._apply_customers(conn, customers, skipped)
            if isinstance(response.get("promotions"), list):
                promotions = []
                for rule in response["promotions"]:
                    if (isinstance(rule, dict) and rule.get("kind") in ("price", "wholesale", "buy_x_pay_y")
                            and isinstance(rule.get("id"), str) and isinstance(rule.get("name"), str)):
                        promotions.append(rule)
                    else:
                        skipped.append(self._skipped("promotion", rule, "Promoção recebida inválida"))
                self._set_meta(conn, "promotions", _json(promotions))
            if isinstance(response.get("payment_methods"), list):
                self._set_meta(conn, "payment_methods",
                               _json(self._validated_payment_methods(response["payment_methods"], skipped)))
            for key, parent in (("tenant_name", "tenant"), ("shop_name", "shop"), ("terminal_name", "terminal")):
                value = response.get(parent, {}).get("name") if isinstance(response.get(parent), dict) else None
                if _optional_text(value, 160):
                    self._set_meta(conn, key, _optional_text(value, 160))
            if _optional_text(response.get("license_hint"), 16):
                self._set_meta(conn, "license_hint", _optional_text(response["license_hint"], 16))
            if response.get("issuer"):
                self._set_meta(conn, "issuer_profile", _json(response["issuer"]))
            if isinstance(response.get("nfce"), dict):
                # NFC-e switch, environment and printing rules (no secrets); old clouds send nothing.
                nfce.store_config(conn, self._set_meta, response["nfce"])
            self._set_meta(conn, "bootstrap_at", str(timestamp))
            # Bootstrap runs every 30 seconds: audit it only when something changed.
            summary = f"{len(products)} produtos; {len(operators)} operadores"
            if summary != self._meta(conn, "bootstrap_summary"):
                self._set_meta(conn, "bootstrap_summary", summary)
                self._audit(conn, None, "terminal.bootstrap", None, summary)
            skipped_raw = _json(skipped[:50])
            if skipped_raw != (self._meta(conn, "catalog_skipped") or "[]"):
                self._set_meta(conn, "catalog_skipped", skipped_raw)
                for item in skipped[:50]:
                    self._audit(conn, None, "terminal.catalog_skipped", item["id"] or None,
                                f"{item['kind']}: {item['error']}"[:300])
            result = {"products": len(products), "operators": len(operators)}
            if skipped:
                result["skipped"] = len(skipped)
            return result

    @staticmethod
    def _skipped(kind, item, error):
        item_id = item.get("id") if isinstance(item, dict) else None
        return {"kind": kind, "id": str(item_id)[:160] if item_id else "", "error": str(error)[:200]}

    def _catalog_skipped(self, conn):
        try:
            value = json.loads(self._meta(conn, "catalog_skipped") or "[]")
        except ValueError:
            return []
        return value if isinstance(value, list) else []

    def catalog_skipped(self):
        """Rows of the last bootstrap that this terminal could not use."""
        with self.connection() as conn:
            return self._catalog_skipped(conn)

    @staticmethod
    def _catalog_row(product, pending_deltas, timestamp):
        if not isinstance(product, dict):
            raise TerminalError("Produto recebido inválido")
        product_id = _str(product.get("id"), "ID do produto")
        unit = product.get("unit")
        if unit not in ("UN", "KG", "L"):
            raise TerminalError("Unidade de produto inválida")
        name = product.get("name")
        if not isinstance(name, str) or not name.strip():
            raise TerminalError("Nome do produto inválido")
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
            image_url = ""  # cosmetic: never lose the product over its picture
        stock_controlled = product.get("stock_controlled", True)
        if type(stock_controlled) is not bool:
            raise TerminalError("Configuração de estoque inválida")
        fiscal, fiscal_ready = _catalog_fiscal(product.get("fiscal"))
        return {"id": product_id, "sku": product.get("sku") if isinstance(product.get("sku"), str) else None,
                "barcode": _optional_text(product.get("barcode"), 80),
                "ncm": _optional_text(product.get("ncm"), 20),
                "scale_code": _optional_text(product.get("scale_code"), 20),
                # Same limit as the cloud; the sale event carries this name back.
                "name": name.strip()[:PRODUCT_NAME_MAX], "unit": unit,
                "category": _optional_text(category, 160) or "Geral", "price_cents": price,
                "cost_cents": _positive_int(product.get("cost_cents", 0), "Custo", zero=True),
                "image_url": image_url, "stock_controlled": int(stock_controlled), "stock_milli": stock,
                "active": int(bool(product.get("active", True))), "updated_at": timestamp,
                "group_id": _optional_text(product.get("group_id"), 160),
                "group_name": _optional_text(product.get("group_name"), 140),
                "variant": _optional_text(product.get("variant"), 120),
                "fiscal": fiscal, "fiscal_ready": fiscal_ready}

    @staticmethod
    def _upsert_product(conn, row):
        conn.execute("""INSERT INTO products
            (id,sku,barcode,ncm,scale_code,name,unit,category,price_cents,cost_cents,image_url,stock_controlled,stock_milli,active,updated_at,
             group_id,group_name,variant,search_text,fiscal,fiscal_ready)
            VALUES (:id,:sku,:barcode,:ncm,:scale_code,:name,:unit,:category,:price_cents,:cost_cents,:image_url,
                    :stock_controlled,:stock_milli,:active,:updated_at,:group_id,:group_name,:variant,:search_text,
                    :fiscal,:fiscal_ready)
            ON CONFLICT(id) DO UPDATE SET sku=excluded.sku,barcode=excluded.barcode,
            ncm=excluded.ncm,
            scale_code=excluded.scale_code,
            name=excluded.name,unit=excluded.unit,category=excluded.category,
            price_cents=excluded.price_cents,cost_cents=excluded.cost_cents,
            image_url=excluded.image_url,stock_controlled=excluded.stock_controlled,stock_milli=excluded.stock_milli,
            active=excluded.active,updated_at=excluded.updated_at,group_id=excluded.group_id,
            group_name=excluded.group_name,variant=excluded.variant,search_text=excluded.search_text,
            fiscal=excluded.fiscal,fiscal_ready=excluded.fiscal_ready""",
                     {"fiscal": "", "fiscal_ready": None, **row, "search_text": _search_text(row["name"], row["sku"])})

    def _apply_operators(self, conn, operators, skipped):
        operator_ids = []
        for operator in operators:
            try:
                if not isinstance(operator, dict):
                    raise TerminalError("Operador inválido")
                op_id = _str(operator.get("id"), "Operador")
                pin_hash = _str(operator.get("pin_hash"), "Hash do PIN", 400)
                name = _str(operator.get("name"), "Nome do operador")
                role = _str(operator.get("role"), "Papel")
            except TerminalError as exc:
                skipped.append(self._skipped("operator", operator, exc))
                continue
            operator_ids.append(op_id)
            previous = conn.execute("SELECT pin_hash FROM operators WHERE id=?", (op_id,)).fetchone()
            conn.execute("""INSERT INTO operators VALUES (?,?,?,?,1)
                ON CONFLICT(id) DO UPDATE SET name=excluded.name,role=excluded.role,
                pin_hash=excluded.pin_hash,active=1""", (op_id, name, role, pin_hash))
            # A new or changed PIN must be usable immediately, even when this
            # device had accumulated failed attempts for the same operator.
            if previous is None or previous["pin_hash"] != pin_hash:
                # Also the attempts counted on each additional cashier ("<operator>|<station>").
                conn.execute("DELETE FROM login_attempts WHERE operator_id=? OR operator_id LIKE ? ESCAPE '\\'",
                             (op_id, _like(op_id) + "|%"))
        marks = ",".join("?" for _ in operator_ids)
        conn.execute("UPDATE operators SET active=0" + (f" WHERE id NOT IN ({marks})" if operator_ids else ""),
                     operator_ids)
        # The manager-PIN lockouts are kept under their own keys: a bootstrap
        # (every 30 seconds, or on demand) must never give back the attempts.
        active = set(operator_ids)
        orphans = [(row[0],) for row in conn.execute("SELECT operator_id FROM login_attempts").fetchall()
                   if not row[0].startswith(MANAGER_OVERRIDE_KEY) and row[0].split("|", 1)[0] not in active]
        conn.executemany("DELETE FROM login_attempts WHERE operator_id=?", orphans)

    @staticmethod
    def _pending_credit_effects(conn):
        """Fiado and store-credit effects of events the cloud has not acknowledged."""
        effects = defaultdict(lambda: [0, 0])  # customer_id -> [open fiado, store credit]
        for (payload,) in conn.execute("SELECT payload FROM outbox WHERE acked_at IS NULL ORDER BY seq"):
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
            elif kind == "credit.received":
                effects[data["customer_id"]][0] -= int(data["amount_cents"])
            elif kind == "sale.returned" and data.get("customer_id"):
                if data.get("refund_method") == "account":
                    effects[data["customer_id"]][0] -= int(data["total_cents"])
                elif data.get("refund_method") == "store_credit":
                    effects[data["customer_id"]][1] += int(data["total_cents"])
        return effects

    def _apply_customers(self, conn, customers, skipped):
        pending = self._pending_credit_effects(conn)
        ids = []
        for customer in customers:
            try:
                if not isinstance(customer, dict):
                    raise TerminalError("Cliente recebido inválido")
                customer_id = _str(customer.get("id"), "Cliente")
                name = _str(customer.get("name"), "Nome do cliente", 180)
                values = [customer.get(key, 0) for key in ("credit_limit_cents", "open_credit_cents", "store_credit_cents")]
                if any(type(value) is not int for value in values) or values[0] < 0:
                    raise TerminalError("Saldo de cliente inválido")
            except TerminalError as exc:
                skipped.append(self._skipped("customer", customer, exc))
                continue
            open_credit = max(0, values[1] + pending[customer_id][0])
            store_credit = values[2] + pending[customer_id][1]
            conn.execute("""INSERT INTO customers VALUES (?,?,?,?,?,?,1)
                ON CONFLICT(id) DO UPDATE SET name=excluded.name,document=excluded.document,
                credit_limit_cents=excluded.credit_limit_cents,open_credit_cents=excluded.open_credit_cents,
                store_credit_cents=excluded.store_credit_cents,active=1""",
                (customer_id, name, str(customer.get("document") or "")[:30], values[0], open_credit, store_credit))
            ids.append(customer_id)
        conn.execute("UPDATE customers SET active=0" + (" WHERE id NOT IN (" + ",".join("?" for _ in ids) + ")" if ids else ""), ids)

    @classmethod
    def _validated_payment_methods(cls, rows, skipped):
        result = []
        for row in rows:
            try:
                if not isinstance(row, dict) or row.get("kind") not in BUILTIN_PAYMENTS + ("other",):
                    raise TerminalError("Forma de pagamento recebida inválida")
                method = {"id": _str(row.get("id"), "Forma de pagamento"), "kind": row["kind"],
                          "name": _str(row.get("name"), "Nome da forma de pagamento", 60),
                          "active": bool(row.get("active", True))}
                if row.get("print_rule") in ("always", "ask", "never"):
                    method["print_rule"] = row["print_rule"]  # NFC-e printing after a sale (docs/NFCE.md)
                result.append(method)
            except TerminalError as exc:
                skipped.append(cls._skipped("payment_method", row, exc))
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

    def _promotions(self, conn):
        raw = self._meta(conn, "promotions")
        return json.loads(raw) if raw else []

    def _line_promotion(self, conn, row, quantity_milli, now_ms, label=False):
        from shared.promotions import best_promotion, describe

        product = {"id": row["id"], "category": row["category"], "unit": row["unit"], "price_cents": row["price_cents"]}
        discount, promotion = best_promotion(product, quantity_milli, self._promotions(conn), now_ms)
        if not promotion:
            return {"promo_discount_cents": 0, "promotion_id": None, "promotion_name": ""}
        result = {"promo_discount_cents": discount, "promotion_id": promotion["id"],
                  "promotion_name": promotion["name"][:80]}
        if label:  # screen only: the sale event carries exactly the fields the cloud accepts
            result["promotion_label"] = describe(promotion)
        return result

    def quote(self, item_requests):
        """Price a cart with the same engine the sale uses (works offline)."""
        if not isinstance(item_requests, list):
            raise TerminalError("Itens inválidos")
        now, lines = _now_ms(), []
        with self.connection() as conn:
            for item in item_requests[:200]:
                if not isinstance(item, dict):
                    raise TerminalError("Item inválido")
                row = conn.execute("SELECT * FROM products WHERE id=? AND active=1", (str(item.get("product_id")),)).fetchone()
                qty = item.get("quantity_milli")
                if not row or type(qty) is not int or qty <= 0:
                    continue
                promo = self._line_promotion(conn, row, qty, now, label=True)
                gross = _line_gross(row["price_cents"], qty)
                lines.append({"product_id": row["id"], "gross_cents": gross, **promo,
                              "net_cents": gross - promo["promo_discount_cents"]})
        return {"lines": lines, "gross_cents": sum(l["gross_cents"] for l in lines),
                "promo_discount_cents": sum(l["promo_discount_cents"] for l in lines)}

    def customers(self, query=""):
        wanted = _fold(str(query or "").strip()[:60])
        with self.connection() as conn:
            rows = conn.execute(
                """SELECT id,name,document,credit_limit_cents,open_credit_cents,store_credit_cents,
                   MAX(credit_limit_cents-open_credit_cents,0) available_credit_cents
                   FROM customers WHERE active=1 ORDER BY name""")
            # Filtered here because SQLite LIKE does not ignore accents.
            found = (dict(row) for row in rows
                     if wanted in _fold(row["name"]) or wanted in _fold(row["document"]))
            return [row for row, _ in zip(found, range(50))]

    def unpair(self):
        """Undo a clean setup; a database with business history stays bound."""
        with self.transaction() as conn:
            pending = conn.execute("""SELECT COUNT(*) FROM outbox
                WHERE acked_at IS NULL AND event_type != 'terminal.config'""").fetchone()[0]
            if pending:
                raise TerminalError("Sincronize as vendas pendentes antes de trocar de empresa", 409)
            if conn.execute("SELECT 1 FROM cash_sessions WHERE closed_at IS NULL").fetchone():
                raise TerminalError("Feche o caixa antes de trocar de empresa", 409)
            if self._has_business_history(conn):
                raise TerminalError("Este computador contém histórico desta empresa. Use uma nova instalação ou diretório de dados para conectar outra empresa; preserve o banco atual.", 409)
            self._clear_company_cache(conn)
            # Setup notes of the pairing being undone must not travel with the next one.
            conn.execute("DELETE FROM outbox WHERE event_type = 'terminal.config'")
            self._clear_connection_meta(conn)
            self._audit(conn, None, "terminal.unpaired")
        return {"paired": False}

    def operators(self):
        with self.connection() as conn:
            return [dict(row) for row in conn.execute(
                "SELECT id,name,role FROM operators WHERE active=1 ORDER BY name")]

    def has_manager(self):
        """Whether anyone on this terminal can authorize maintenance with a PIN."""
        with self.connection() as conn:
            return conn.execute("SELECT 1 FROM operators WHERE active=1 AND role IN ('owner','admin','manager') "
                                "LIMIT 1").fetchone() is not None

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
        # Wrong PINs lock this operator on this cashier only.
        attempts_key = operator_id + self._station_suffix()
        # PBKDF2 is slow on purpose: run it without holding the write lock,
        # which the sale path and the background worker also need.
        with self.connection() as conn:
            attempts = conn.execute("SELECT locked_until FROM login_attempts WHERE operator_id=?",
                                    (attempts_key,)).fetchone()
            if attempts and attempts["locked_until"] > now:
                raise TerminalError("Muitas tentativas; aguarde cinco minutos", 429)
            row = conn.execute("SELECT * FROM operators WHERE id=? AND active=1", (operator_id,)).fetchone()
        valid = bool(row and isinstance(pin, str) and verify_pin(pin, row["pin_hash"]))
        with self.transaction() as conn:
            attempts = conn.execute("SELECT failures,locked_until FROM login_attempts WHERE operator_id=?",
                                    (attempts_key,)).fetchone()
            if attempts and attempts["locked_until"] > now:
                raise TerminalError("Muitas tentativas; aguarde cinco minutos", 429)
            if valid:  # the PIN may have been rotated or the operator removed meanwhile
                valid = conn.execute("SELECT 1 FROM operators WHERE id=? AND active=1 AND pin_hash=?",
                                     (operator_id, row["pin_hash"])).fetchone() is not None
            if not valid:
                failures = (attempts["failures"] if attempts else 0) + 1
                conn.execute("""INSERT INTO login_attempts VALUES (?,?,?)
                    ON CONFLICT(operator_id) DO UPDATE SET failures=excluded.failures,locked_until=excluded.locked_until""",
                    (attempts_key, failures, now + 300 if failures >= 5 else 0))
                self._audit(conn, operator_id, "operator.login_failed")
            else:
                conn.execute("DELETE FROM login_attempts WHERE operator_id=?", (attempts_key,))
                self._audit(conn, operator_id, "operator.login")
                result = {"id": row["id"], "name": row["name"], "role": row["role"]}
        if not valid:
            raise TerminalError("Operador ou PIN inválido", 401)
        return result

    def _station_suffix(self):
        """Suffix of the lockout keys: nothing on the main cashier, "|<station>" on the others."""
        station = self.current_station()
        return "" if station == SERVER_STATION_ID else "|" + station

    def manager_override(self, pin, requester_id=None):
        """Check a manager PIN typed on behalf of `requester_id` (the logged operator).

        Wrong PINs lock only that requester. Without one (maintenance on the
        login screen) the attempts are counted apart and never lock the cashier.
        """
        from shared.security import verify_pin

        if not isinstance(pin, str) or not pin:
            raise TerminalError("PIN de gerente necessário", 403)
        now = int(time.time())
        # Per cashier: typos on one computer never lock the others, and the ceiling is per cashier too.
        suffix = self._station_suffix()
        own_key = f"{MANAGER_OVERRIDE_KEY}:{requester_id or 'maintenance'}{suffix}"
        global_key, window_key = MANAGER_OVERRIDE_KEY + suffix, "manager_override_window" + suffix
        keys = (own_key, global_key) if requester_id else (own_key,)
        lock_query = "SELECT failures,locked_until FROM login_attempts WHERE operator_id=?"

        def ensure_unlocked(conn):
            for key in keys:
                attempts = conn.execute(lock_query, (key,)).fetchone()
                if attempts and attempts["locked_until"] > now:
                    raise TerminalError("Muitas tentativas de autorização; aguarde", 429)

        with self.connection() as conn:
            ensure_unlocked(conn)
            managers = conn.execute(
                "SELECT id,pin_hash FROM operators WHERE active=1 AND role IN ('owner','admin','manager')").fetchall()
        # Same reason as in login(): no PBKDF2 while holding the write lock.
        matched = next((row for row in managers if verify_pin(pin, row["pin_hash"])), None)
        with self.transaction() as conn:
            ensure_unlocked(conn)
            if matched and conn.execute(
                    "SELECT 1 FROM operators WHERE id=? AND active=1 AND pin_hash=? AND role IN ('owner','admin','manager')",
                    (matched["id"], matched["pin_hash"])).fetchone():
                conn.execute("DELETE FROM login_attempts WHERE operator_id=?", (own_key,))
                return matched["id"]
            upsert = """INSERT INTO login_attempts VALUES (?,?,?)
                ON CONFLICT(operator_id) DO UPDATE SET failures=excluded.failures,locked_until=excluded.locked_until"""
            attempts = conn.execute(lock_query, (own_key,)).fetchone()
            failures = (attempts["failures"] if attempts else 0) + 1
            conn.execute(upsert, (own_key, failures,
                                  now + MANAGER_OVERRIDE_LOCK_SECONDS if failures >= MANAGER_OVERRIDE_FAILURES else 0))
            if requester_id:
                # Store-wide ceiling, counted inside a time window so honest typos never add up.
                window = int(self._meta(conn, window_key) or 0)
                attempts = conn.execute(lock_query, (global_key,)).fetchone()
                total = attempts["failures"] if attempts else 0
                if now - window >= MANAGER_OVERRIDE_GLOBAL_WINDOW_SECONDS or now < window:
                    total = 0
                    self._set_meta(conn, window_key, str(now))
                total += 1
                locked = total >= MANAGER_OVERRIDE_GLOBAL_FAILURES
                conn.execute(upsert, (global_key, 0 if locked else total,
                                      now + MANAGER_OVERRIDE_LOCK_SECONDS if locked else 0))
                if locked:
                    self._audit(conn, requester_id, "manager_override.store_locked")
        raise TerminalError("PIN de gerente inválido", 403)

    def authorize_discount(self, pin, gross_cents, discount_cents, operator_id=None):
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
        self.manager_override(pin, operator_id)
        return {"authorized": True}

    def products(self, query="", category=""):
        return self.product_search(query, category)["products"]

    def product_search(self, query="", category=""):
        """One page of the catalog plus how many products matched in total.

        An exact barcode, SKU or id wins; otherwise every typed word must be
        part of the name or SKU, ignoring accents and case.
        """
        query = str(query)[:100].strip()
        with self.connection() as conn:
            if query:
                exact = conn.execute("SELECT * FROM products WHERE active=1 AND (id=? OR barcode=? OR sku=?)",
                                     (query, query, query)).fetchone()
                if exact:
                    return {"products": [_product_out(exact)], "total": 1, "limit": PRODUCT_PAGE_SIZE}
            where, params = "active=1", []
            for word in _fold(query).split():
                where += " AND search_text LIKE ? ESCAPE '\\'"
                params.append(f"%{_like(word)}%")
            if category:
                where += " AND category=?"
                params.append(category)
            total = conn.execute(f"SELECT COUNT(*) FROM products WHERE {where}", params).fetchone()[0]
            rows = conn.execute(f"SELECT * FROM products WHERE {where} ORDER BY name LIMIT ?",
                                (*params, PRODUCT_PAGE_SIZE))
            return {"products": [_product_out(row) for row in rows], "total": total, "limit": PRODUCT_PAGE_SIZE}

    def product_by_scale_code(self, scale_code):
        """Weighed product of a scale label, searched over the whole catalog."""
        code = str(scale_code)[:20]
        with self.connection() as conn:
            row = conn.execute(
                "SELECT * FROM products WHERE active=1 AND unit='KG' AND scale_code IN (?,?) ORDER BY name LIMIT 1",
                (code, code.lstrip("0") or code)).fetchone()
        if not row:
            raise TerminalError("Nenhum produto por quilo com este código de balança", 404)
        return _product_out(row)

    def products_by_ids(self, ids):
        """Current price and stock of specific products (cart resume, catalog refresh)."""
        ids = [str(value) for value in ids if value][:100]
        if not ids:
            return []
        with self.connection() as conn:
            return [_product_out(row) for row in conn.execute(
                "SELECT * FROM products WHERE active=1 AND id IN (" + ",".join("?" for _ in ids) + ")", ids)]

    def categories(self):
        with self.connection() as conn:
            return [row[0] for row in conn.execute("SELECT DISTINCT category FROM products WHERE active=1 ORDER BY category")]

    def cash_state(self):
        """Open cashier (operator shift) of the station that is asking (each computer has its own)."""
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM cash_sessions WHERE closed_at IS NULL AND station_id=?",
                               (self.current_station(),)).fetchone()
            if not row:
                return None
            return self._shift_state(conn, row)

    @staticmethod
    def _shift_state(conn, row):
        """Open shift as the screen needs it: whose it is and whether it is paused."""
        result = dict(row)
        owner = conn.execute("SELECT name FROM operators WHERE id=?", (row["operator_id"],)).fetchone()
        result["operator_name"] = owner["name"] if owner else ""
        pause = conn.execute("SELECT * FROM cash_pauses WHERE session_id=? AND ended_at IS NULL",
                             (row["id"],)).fetchone()
        result["paused"] = None
        if pause:
            by = conn.execute("SELECT name FROM operators WHERE id=?", (pause["operator_id"],)).fetchone()
            result["paused"] = {"id": pause["id"], "since": pause["started_at"], "reason": pause["reason"],
                                "operator_id": pause["operator_id"], "operator_name": by["name"] if by else ""}
        return result

    def _own_shift(self, conn, operator_id, manager=False):
        """Open shift of this station, only if it belongs to `operator_id` and is not paused.

        Rule: one active shift per station, and the shift belongs to the operator who opened it.
        Another operator does not sell, take money in or out of that drawer: the shift is closed
        first (manager PIN) and the next operator starts their own. `manager=True` (a manager doing
        a refund at the counter) may act on the shift of someone else, never on a paused one.
        """
        cash = self._open_cash(conn)
        if cash["operator_id"] and cash["operator_id"] != operator_id and not manager:
            owner = conn.execute("SELECT name FROM operators WHERE id=?", (cash["operator_id"],)).fetchone()
            raise TerminalError(f"O caixa está no turno de {owner['name'] if owner else 'outro operador'}. "
                                "Peça para ele encerrar o turno, ou um gerente encerra (F12).", 409,
                                code="cash_other_operator")
        if conn.execute("SELECT 1 FROM cash_pauses WHERE session_id=? AND ended_at IS NULL",
                        (cash["id"],)).fetchone():
            raise TerminalError("Caixa pausado. Retome o caixa com o seu PIN para continuar.", 409,
                                code="cash_paused")
        return cash

    def _open_cash(self, conn):
        row = conn.execute("SELECT * FROM cash_sessions WHERE closed_at IS NULL AND station_id=?",
                           (self.current_station(),)).fetchone()
        if not row:
            raise TerminalError("Abra o caixa antes da operação", 409)
        return row

    def any_cash_open(self):
        """True while any computer of the shop has its cashier open."""
        with self.connection() as conn:
            return conn.execute("SELECT 1 FROM cash_sessions WHERE closed_at IS NULL LIMIT 1").fetchone() is not None

    def open_cash_sessions(self):
        with self.connection() as conn:
            return [dict(row) for row in conn.execute(
                """SELECT c.id,c.station_id,COALESCE(s.name,'') station_name,c.operator_id,c.opened_at
                   FROM cash_sessions c LEFT JOIN stations s ON s.id=c.station_id
                   WHERE c.closed_at IS NULL ORDER BY c.opened_at,c.id""")]

    @staticmethod
    def _cash_expected(conn, cash):
        return Store._cash_summary(conn, cash)["expected_cents"]

    @staticmethod
    def _cash_summary(conn, cash):
        categories = {"cash_sales_net_cents": 0, "pix_cents": 0,
                      "card_credit_cents": 0, "card_debit_cents": 0,
                      "card_legacy_cents": 0, "supplied_cents": 0,
                      "withdrawn_cents": 0, "credit_account_cents": 0,
                      "store_credit_cents": 0, "other_cents": 0}
        sales_total = received_total = changes = 0
        sales_count = 0
        # Every form of payment, the company's own ones by name (closing screen and reports).
        methods = {key: 0 for key in ("cash", "pix", "card_debit", "card_credit")}
        method_names = {}
        raw_config = Store._meta(conn, "payment_methods")
        configured = {row.get("id"): row.get("name") for row in (json.loads(raw_config) if raw_config else [])
                      if isinstance(row, dict) and row.get("kind") == "other"}
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
                key = str(method)
                if method == "other":
                    configured_id = str(payment.get("payment_method_id") or "")
                    key = "other:" + configured_id
                    method_names[key] = ((sale.get("payment_names") or {}).get(configured_id)
                                         or configured.get(configured_id) or "Outra forma")
                    categories["other_cents"] += amount
                elif method in ("credit_account", "store_credit"):
                    categories[method + "_cents"] += amount
                methods[key] = methods.get(key, 0) + amount
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
        methods["cash"] -= changes
        categories["cash_refunds_cents"] = conn.execute(
            "SELECT COALESCE(SUM(total_cents),0) FROM sale_returns WHERE session_id=? AND refund_method='cash'",
            (cash["id"],)).fetchone()[0]
        categories["credit_received_cash_cents"] = categories["credit_received_other_cents"] = 0
        for method, amount in conn.execute(
                "SELECT method,COALESCE(SUM(amount_cents),0) FROM credit_receipts WHERE session_id=? GROUP BY method",
                (cash["id"],)):
            categories["credit_received_cash_cents" if method == "cash" else "credit_received_other_cents"] += amount
        movements = conn.execute(
            "SELECT kind,COALESCE(SUM(amount_cents),0) amount FROM cash_movements "
            "WHERE session_id=? GROUP BY kind", (cash["id"],))
        for row in movements:
            if row["kind"] == "supplied":
                categories["supplied_cents"] = row["amount"]
            elif row["kind"] == "withdrawn":
                categories["withdrawn_cents"] = row["amount"]
        expected = (cash["opening_cents"] + categories["cash_sales_net_cents"] +
                    categories["supplied_cents"] - categories["withdrawn_cents"] - categories["cash_refunds_cents"]
                    + categories["credit_received_cash_cents"])
        return {
            "id": cash["id"], "opened_at": cash["opened_at"],
            "opening_cents": cash["opening_cents"], "sales_count": sales_count,
            "sales_total_cents": sales_total,
            "received_total_cents": received_total - changes,
            "categories": categories, "expected_cents": expected,
            "methods": [{"key": key, "label": method_names.get(key) or METHOD_LABELS.get(key, key),
                         "amount_cents": amount} for key, amount in methods.items()],
            "total_channels_cents": (categories["cash_sales_net_cents"] +
                                      categories["pix_cents"] + categories["card_credit_cents"] +
                                      categories["card_debit_cents"] + categories["card_legacy_cents"]),
        }

    def cash_summary(self):
        with self.connection() as conn:
            return self._cash_summary(conn, self._open_cash(conn))

    def open_cash(self, operator_id, opening_cents):
        self.ensure_not_restoring()
        status = self.lease_status()
        if not status["can_open_cash"]:
            raise TerminalError("Licença offline vencida; conecte este caixa à internet para renovar antes "
                                "de abrir um novo caixa" if status["state"] == "expired" else status["reason"], 403)
        schedule = self.business_hours_status()
        if schedule["configured"] and not schedule["open"]:
            raise TerminalError("Fora do horário de funcionamento da empresa; o caixa não pode ser aberto agora", 403)
        opening_cents = _positive_int(opening_cents, "Valor de abertura", zero=True)
        with self.transaction() as conn:
            station = self.current_station()
            if conn.execute("SELECT 1 FROM cash_sessions WHERE closed_at IS NULL AND station_id=?",
                            (station,)).fetchone():
                raise TerminalError("Já existe caixa aberto", 409)
            session_id, timestamp = _uuid(), _now_ms()
            conn.execute("""INSERT INTO cash_sessions
                (id,operator_id,opening_cents,opened_at,closed_at,counted_cents,expected_cents,station_id)
                VALUES (?,?,?,?,NULL,NULL,NULL,?)""", (session_id, operator_id, opening_cents, timestamp, station))
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
            cash = self._own_shift(conn, operator_id)
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

    def close_cash(self, operator_id, counted_cents, manager_pin, station_id=None):
        # Fechar o caixa é uma operação de supervisão. A validação acontece
        # antes de alterar a sessão para que um PIN inválido nunca deixe o
        # operador em um estado parcialmente fechado.
        # Allowed on a transferred terminal: the closing still goes up through the send-only token.
        self.ensure_not_restoring()
        current = self.current_station()
        target = station_id if isinstance(station_id, str) and station_id else current
        # Only the main cashier closes the cashier of another computer (one that broke or was removed).
        if target != current and current != SERVER_STATION_ID:
            raise TerminalError(SERVER_ONLY_MESSAGE, 403, code="server_only")
        # The PIN is checked where it was typed; the closing is recorded on the cashier it belongs to.
        supervisor_id = self.manager_override(manager_pin, operator_id)
        counted_cents = _positive_int(counted_cents, "Valor contado", zero=True)
        with self.station(target):
            summary = self._close_cash(operator_id, counted_cents, supervisor_id)
        # The operator report comes out where the closing was typed (the manager may be closing
        # the shift of another computer). A printing problem never undoes a closing.
        try:
            summary["report"] = self._print_reports([summary["id"]], operator_id, "cash.report_printed")
        except (TerminalError, sqlite3.Error, ValueError, KeyError, TypeError) as exc:
            summary["report"] = {"layout": None, "jobs": [], "error": str(exc)}
        _mirror_call("on_cash_closed", self)
        return summary

    def _close_cash(self, operator_id, counted_cents, supervisor_id):
        with self.transaction() as conn:
            cash = self._open_cash(conn)
            summary = self._cash_summary(conn, cash)
            expected = summary["expected_cents"]
            timestamp = _now_ms()
            # A paused shift may be closed (by a manager): the pause ends with it.
            conn.execute("UPDATE cash_pauses SET ended_at=? WHERE session_id=? AND ended_at IS NULL",
                         (timestamp, cash["id"]))
            conn.execute("UPDATE cash_sessions SET closed_at=?,counted_cents=?,expected_cents=? WHERE id=?",
                         (timestamp, counted_cents, expected, cash["id"]))
            conn.execute("INSERT OR REPLACE INTO cash_closings(session_id,closed_by,supervisor_id) VALUES (?,?,?)",
                         (cash["id"], operator_id, supervisor_id))
            data = {"counted_cents": counted_cents, "expected_cents": expected,
                    "difference_cents": counted_cents - expected, "created_at": timestamp,
                    "operator_id": operator_id, "supervisor_id": supervisor_id}
            if self._shift_events(conn):
                # Only a cloud that announced "cash_shifts" knows these keys (an older one refuses them).
                closed = self._shift_data(conn, conn.execute("SELECT * FROM cash_sessions WHERE id=?",
                                                             (cash["id"],)).fetchone())
                data["shift_operator_id"] = cash["operator_id"] or None
                data["paused_ms"] = closed["paused_ms"]
                data["totals"] = {"sales_count": closed["sales"]["count"],
                                  "gross_cents": closed["sales"]["gross_cents"],
                                  "discount_cents": closed["sales"]["discount_cents"],
                                  "net_cents": closed["sales"]["net_cents"],
                                  "returns_cents": closed["sales"]["returns_cents"],
                                  "voided_count": closed["sales"]["voided_count"],
                                  "supplied_cents": closed["supplied_cents"],
                                  "withdrawn_cents": closed["withdrawn_cents"],
                                  "by_method": {row["key"]: row["amount_cents"] for row in closed["methods"]
                                                if row["amount_cents"]}}
            self._event(conn, "cash.closed", cash["id"], data)
            self._audit(conn, operator_id, "cash.closed", cash["id"])
            summary.update({"closed_at": timestamp, "expected_cents": expected,
                            "counted_cents": counted_cents,
                            "difference_cents": counted_cents - expected})
            return summary

    # ---------- operator shifts: pause, reports and the closing of the day ----------

    def _shift_events(self, conn):
        return "cash_shifts" in self._meta_json(self._meta(conn, "cloud_capabilities"), [])

    def pause_cash(self, operator, reason=""):
        """Pausar: blocks sales until the shift owner (or a manager) types the PIN again."""
        reason = _optional_text(reason, 120) or ""
        with self.transaction() as conn:
            cash = self._open_cash(conn)
            if cash["operator_id"] and cash["operator_id"] != operator["id"] and operator["role"] not in MANAGER_ROLES:
                raise TerminalError("Só o operador do turno ou um gerente pode pausar este caixa", 403)
            if conn.execute("SELECT 1 FROM cash_pauses WHERE session_id=? AND ended_at IS NULL",
                            (cash["id"],)).fetchone():
                raise TerminalError("O caixa já está pausado", 409, code="cash_paused")
            pause_id, timestamp = _uuid(), _now_ms()
            conn.execute("INSERT INTO cash_pauses(id,session_id,operator_id,reason,started_at,ended_at,resumed_by) "
                         "VALUES (?,?,?,?,?,NULL,NULL)", (pause_id, cash["id"], operator["id"], reason, timestamp))
            if self._shift_events(conn):
                self._event(conn, "cash.paused", pause_id,
                            {"cash_session_id": cash["id"], "operator_id": operator["id"], "reason": reason,
                             "created_at": timestamp})
            self._audit(conn, operator["id"], "cash.paused", cash["id"], reason or None)
            return self._shift_state(conn, cash)

    def resume_cash(self, operator, pin):
        """Retomar: PIN of the shift owner or of a manager. Wrong PINs lock this screen for 5 minutes."""
        from shared.security import verify_pin

        if not isinstance(pin, str) or not pin.strip():
            raise TerminalError("Digite o PIN para retomar o caixa")
        now = int(time.time())
        key = f"cash_resume:{operator['id']}{self._station_suffix()}"
        lock_query = "SELECT failures,locked_until FROM login_attempts WHERE operator_id=?"
        with self.connection() as conn:
            attempts = conn.execute(lock_query, (key,)).fetchone()
            if attempts and attempts["locked_until"] > now:
                raise TerminalError("Muitas tentativas; aguarde cinco minutos", 429)
            cash = self._open_cash(conn)
            pause = conn.execute("SELECT * FROM cash_pauses WHERE session_id=? AND ended_at IS NULL",
                                 (cash["id"],)).fetchone()
            if not pause:
                return self._shift_state(conn, cash)
            candidates = conn.execute(
                "SELECT id,pin_hash FROM operators WHERE active=1 AND (id=? OR role IN ('owner','admin','manager')) "
                "ORDER BY id<>?", (cash["operator_id"], cash["operator_id"])).fetchall()
        # PBKDF2 outside the write lock, as in login().
        matched = next((row for row in candidates if verify_pin(pin.strip(), row["pin_hash"])), None)
        with self.transaction() as conn:
            attempts = conn.execute(lock_query, (key,)).fetchone()
            if attempts and attempts["locked_until"] > now:
                raise TerminalError("Muitas tentativas; aguarde cinco minutos", 429)
            if not matched:
                failures = (attempts["failures"] if attempts else 0) + 1
                conn.execute("""INSERT INTO login_attempts VALUES (?,?,?)
                    ON CONFLICT(operator_id) DO UPDATE SET failures=excluded.failures,locked_until=excluded.locked_until""",
                             (key, failures, now + 300 if failures >= 5 else 0))
                self._audit(conn, operator["id"], "cash.resume_failed", cash["id"])
                failure = TerminalError("PIN inválido. Use o PIN do operador do turno ou de um gerente.", 403)
            else:
                failure = None
                conn.execute("DELETE FROM login_attempts WHERE operator_id=?", (key,))
                timestamp = _now_ms()
                if conn.execute("UPDATE cash_pauses SET ended_at=?,resumed_by=? WHERE id=? AND ended_at IS NULL",
                                (timestamp, matched["id"], pause["id"])).rowcount:
                    if self._shift_events(conn):
                        self._event(conn, "cash.resumed", pause["id"],
                                    {"cash_session_id": cash["id"], "operator_id": matched["id"],
                                     "created_at": timestamp})
                    self._audit(conn, matched["id"], "cash.resumed", cash["id"])
                state = self._shift_state(conn, conn.execute("SELECT * FROM cash_sessions WHERE id=?",
                                                             (cash["id"],)).fetchone())
        if failure:
            raise failure
        return state

    def _shift_data(self, conn, cash):
        """Everything the reports print about one shift (open or closed)."""
        cash = dict(cash)
        closing = conn.execute("SELECT closed_by,supervisor_id FROM cash_closings WHERE session_id=?",
                               (cash["id"],)).fetchone()
        cash["closed_by"], cash["supervisor_id"] = (closing["closed_by"], closing["supervisor_id"]) if closing else ("", "")
        summary = self._cash_summary(conn, cash)
        names = {row["id"]: row["name"] for row in conn.execute("SELECT id,name FROM operators")}
        end = cash["closed_at"] or _now_ms()
        pauses, paused_ms = [], 0
        for row in conn.execute("SELECT * FROM cash_pauses WHERE session_id=? ORDER BY started_at,id", (cash["id"],)):
            length = max(0, (row["ended_at"] or end) - row["started_at"])
            paused_ms += length
            pauses.append({"started_at": row["started_at"], "ended_at": row["ended_at"], "duration_ms": length,
                           "reason": row["reason"], "operator_name": names.get(row["operator_id"], ""),
                           "resumed_by_name": names.get(row["resumed_by"] or "", "")})
        sales = {"count": 0, "gross_cents": 0, "discount_cents": 0, "returns_count": 0, "returns_cents": 0,
                 "voided_count": 0, "voided_cents": 0, "net_cents": 0}
        for row in conn.execute("SELECT status,snapshot,total_cents FROM sales WHERE session_id=?", (cash["id"],)):
            if row["status"] == "completed":
                snapshot = json.loads(row["snapshot"])
                sales["count"] += 1
                sales["gross_cents"] += int(snapshot.get("gross_cents") or 0)
                sales["discount_cents"] += (int(snapshot.get("promo_discount_cents") or 0)
                                            + int(snapshot.get("discount_cents") or 0))
                sales["net_cents"] += int(row["total_cents"] or 0)
            elif row["status"] == "voided":
                sales["voided_count"] += 1
                sales["voided_cents"] += int(row["total_cents"] or 0)
        # A cash refund is tied to the shift; one in store credit or fiado only to who did it, and when.
        for row in conn.execute(
                """SELECT total_cents FROM sale_returns WHERE session_id=? OR (session_id IS NULL AND station_id=?
                   AND operator_id=? AND created_at>=? AND created_at<=?)""",
                (cash["id"], cash["station_id"], cash["operator_id"], cash["opened_at"], end)):
            sales["returns_count"] += 1
            sales["returns_cents"] += int(row["total_cents"] or 0)
        sales["net_cents"] -= sales["returns_cents"]
        movements = [{"created_at": row["created_at"], "kind": row["kind"], "amount_cents": row["amount_cents"],
                      "reason": row["reason"], "operator_name": names.get(row["operator_id"], "")}
                     for row in conn.execute("SELECT * FROM cash_movements WHERE session_id=? ORDER BY created_at,id",
                                             (cash["id"],))]
        credit = [{"key": row["method"], "label": METHOD_LABELS.get(row["method"], row["method"]),
                   "amount_cents": row["amount"]}
                  for row in conn.execute("SELECT method,COALESCE(SUM(amount_cents),0) amount FROM credit_receipts "
                                          "WHERE session_id=? GROUP BY method", (cash["id"],))]
        credit.sort(key=lambda row: cash_reports.method_key_order(row["key"]))
        methods = sorted(summary["methods"], key=lambda row: cash_reports.method_key_order(row["key"]))
        closed = cash["closed_at"] is not None
        expected = cash["expected_cents"] if closed and cash["expected_cents"] is not None else summary["expected_cents"]
        counted = cash["counted_cents"] if closed else None
        raw = self._meta(conn, "issuer_profile")
        station_id = cash["station_id"] or SERVER_STATION_ID
        return {"id": cash["id"], "issuer": json.loads(raw) if raw else {"legal_name": self._meta(conn, "tenant_name") or ""},
                "shop_name": self._meta(conn, "shop_name") or "", "station_id": station_id,
                "station_name": self._station_name(conn, station_id) or "Caixa",
                "operator_id": cash["operator_id"], "operator_name": names.get(cash["operator_id"], ""),
                "opened_at": cash["opened_at"], "closed_at": cash["closed_at"],
                "status": "closed" if closed else "open", "paused": any(p["ended_at"] is None for p in pauses),
                "paused_ms": paused_ms, "effective_ms": max(0, end - cash["opened_at"] - paused_ms),
                "opening_cents": cash["opening_cents"], "sales": sales, "methods": methods,
                "movements": movements, "supplied_cents": summary["categories"]["supplied_cents"],
                "withdrawn_cents": summary["categories"]["withdrawn_cents"],
                "cash_refunds_cents": summary["categories"]["cash_refunds_cents"], "credit_received": credit,
                "expected_cents": expected, "counted_cents": counted,
                "difference_cents": counted - expected if counted is not None else None,
                "pauses": pauses, "closed_by_name": names.get(cash.get("closed_by") or "", ""),
                "supervisor_name": names.get(cash.get("supervisor_id") or "", "")}

    @staticmethod
    def _day_bounds(day=None):
        zone = cash_reports._receipt_zone()
        if day in (None, ""):
            date = datetime.now(zone).date()
        else:
            try:
                date = datetime.strptime(str(day), "%Y-%m-%d").date()
            except ValueError:
                raise TerminalError("Data inválida; use AAAA-MM-DD") from None
        start = datetime(date.year, date.month, date.day, tzinfo=zone)
        end = datetime.fromordinal(date.toordinal() + 1).replace(tzinfo=zone)
        return date, int(start.timestamp() * 1000), int(end.timestamp() * 1000)

    def _visible_stations_clause(self):
        """The main cashier sees every register of the shop; an additional one only itself."""
        station = self.current_station()
        return ("", ()) if station == SERVER_STATION_ID else (" AND station_id=?", (station,))

    def _day_shift_rows(self, conn, start, end):
        clause, params = self._visible_stations_clause()
        return conn.execute(f"SELECT * FROM cash_sessions WHERE opened_at>=? AND opened_at<?{clause} "
                            "ORDER BY opened_at,id", (start, end, *params)).fetchall()

    def shifts_of_day(self, day=None):
        """Shifts of one day (default today) of the registers this computer can see."""
        date, start, end = self._day_bounds(day)
        with self.connection() as conn:
            shifts = [self._shift_data(conn, row) for row in self._day_shift_rows(conn, start, end)]
            open_elsewhere = [self._shift_data(conn, row) for row in conn.execute(
                f"SELECT * FROM cash_sessions WHERE closed_at IS NULL AND opened_at<?{self._visible_stations_clause()[0]}",
                (start, *self._visible_stations_clause()[1]))]
        keys = ("id", "station_id", "station_name", "operator_id", "operator_name", "opened_at", "closed_at",
                "status", "paused", "paused_ms", "effective_ms", "opening_cents", "expected_cents", "counted_cents",
                "difference_cents")
        brief = lambda shift: {**{key: shift[key] for key in keys}, "sales_count": shift["sales"]["count"],
                               "net_cents": shift["sales"]["net_cents"]}
        return {"day": date.isoformat(), "all_stations": self.current_station() == SERVER_STATION_ID,
                "shifts": [brief(shift) for shift in shifts],
                # Still open from an earlier day: the manager must see it before closing the day.
                "older_open": [brief(shift) for shift in open_elsewhere]}

    def _report_width(self, conn):
        setting = self._print_setting(conn, "receipt")
        return (58 if setting and setting["format"] == "escpos58" else 80), setting

    def _print_reports(self, session_ids, operator_id, action, reprint=False):
        """Operator report of each shift: one print job each (when a printer is set) and the layout."""
        with self.transaction() as conn:
            width, setting = self._report_width(conn)
            layouts, jobs = [], []
            for session_id in session_ids:
                row = conn.execute("SELECT * FROM cash_sessions WHERE id=?", (session_id,)).fetchone()
                if not row:
                    raise TerminalError("Turno não encontrado", 404)
                layout = cash_reports.operator_report(self._shift_data(conn, row), width, reprint=reprint)
                layouts.append(layout)
                if setting:
                    jobs.append(self._queue_print(conn, "receipt", layout_bytes(layout), setting))
                self._audit(conn, operator_id, action, session_id)
            return {"layout": cash_reports.combine(layouts), "jobs": jobs, "printer": bool(setting)}

    def print_shift_report(self, session_id, operator_id, own_only=False):
        """Reimprimir: the operator report of a shift this computer can see (a cashier: only their own)."""
        session_id = _str(session_id, "Turno", 64)
        with self.connection() as conn:
            clause, params = self._visible_stations_clause()
            row = conn.execute(f"SELECT operator_id FROM cash_sessions WHERE id=?{clause}",
                               (session_id, *params)).fetchone()
            if not row or (own_only and row["operator_id"] != operator_id):
                raise TerminalError("Turno não encontrado neste caixa", 404)
        return self._print_reports([session_id], operator_id, "cash.report_reprinted", reprint=True)

    def close_day(self, operator_id, manager_pin, day=None):
        """Fechamento do dia: one report per operator, one per register and the day's consolidated."""
        supervisor_id = self.manager_override(manager_pin, operator_id)
        date, start, end = self._day_bounds(day)
        day_label = date.strftime("%d/%m/%Y")
        with self.transaction() as conn:
            rows = self._day_shift_rows(conn, start, end)
            if not rows:
                raise TerminalError("Nenhum turno de caixa neste dia", 404)
            width, setting = self._report_width(conn)
            shifts = [self._shift_data(conn, row) for row in rows]
            issuer, shop_name = shifts[0]["issuer"], shifts[0]["shop_name"]
            layouts = [cash_reports.operator_report(shift, width) for shift in shifts]
            stations = {}
            for shift in shifts:
                stations.setdefault(shift["station_id"], []).append(shift)
            for station_shifts in stations.values():
                layouts.append(cash_reports.station_report(station_shifts[0]["station_name"], station_shifts, width,
                                                           day_label, issuer, shop_name))
            session_ids = [shift["id"] for shift in shifts]
            names = {row["id"]: row["name"] for row in conn.execute("SELECT id,name FROM operators")}
            marks = ",".join("?" * len(session_ids))
            products = {}
            for row in conn.execute(f"SELECT snapshot FROM sales WHERE status='completed' AND session_id IN ({marks})",
                                    session_ids):
                for item in json.loads(row["snapshot"]).get("items", []):
                    entry = products.setdefault(item["product_id"], {
                        "name": item.get("name") or "", "unit": item.get("unit") or "UN", "quantity_milli": 0,
                        "amount_cents": 0, "category": ""})
                    entry["quantity_milli"] += int(item.get("quantity_milli") or 0)
                    entry["amount_cents"] += int(item.get("line_total_cents", item.get("gross_cents")) or 0)
            for product_id, entry in products.items():
                found = conn.execute("SELECT category FROM products WHERE id=?", (product_id,)).fetchone()
                entry["category"] = (found["category"] if found else "") or ""
            cancellations = [{"id": row["id"], "created_at": row["created_at"], "voided_at": row["voided_at"],
                              "total_cents": row["total_cents"], "reason": row["void_reason"] or "",
                              "operator_name": names.get(row["operator_id"], "")}
                             for row in conn.execute(f"SELECT * FROM sales WHERE status='voided' AND session_id IN "
                                                     f"({marks}) ORDER BY voided_at,id", session_ids)]
            clause, params = self._visible_stations_clause()
            returns = [{"created_at": row["created_at"], "total_cents": row["total_cents"],
                        "refund_method": row["refund_method"], "reason": row["reason"],
                        "operator_name": names.get(row["operator_id"], "")}
                       for row in conn.execute(f"SELECT * FROM sale_returns WHERE created_at>=? AND created_at<?{clause} "
                                               "ORDER BY created_at,id", (start, end, *params))]
            layouts.append(cash_reports.day_report(day_label, shifts, list(products.values()), cancellations, returns,
                                                   width, issuer, shop_name))
            jobs = [self._queue_print(conn, "receipt", layout_bytes(layout), setting) for layout in layouts] if setting else []
            self._audit(conn, operator_id, "cash.day_closed", date.isoformat(),
                        f"supervisor={supervisor_id}; turnos={len(shifts)}")
            return {"day": date.isoformat(), "layouts": layouts, "layout": cash_reports.combine(layouts),
                    "jobs": jobs, "printer": bool(setting), "shifts": len(shifts),
                    "open_shifts": sum(1 for shift in shifts if shift["status"] == "open")}

    def authorize_item_removal(self, operator, manager_pin=None):
        """Require a supervisor PIN when a cashier removes a line from a cart."""
        authorizer = (operator["id"] if operator.get("role") in MANAGER_ROLES
                      else self.manager_override(manager_pin, operator["id"]))
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
        # The cloud refuses larger sales; stop here instead of selling what can never sync.
        if len(item_requests) > MAX_SALE_ITEMS:
            raise TerminalError(f"Uma venda aceita até {MAX_SALE_ITEMS} produtos diferentes; finalize esta e abra outra")
        if len(payments) > MAX_SALE_PAYMENTS:
            raise TerminalError(f"Uma venda aceita até {MAX_SALE_PAYMENTS} pagamentos")
        discount = _positive_int(request.get("discount_cents", 0), "Desconto", zero=True)
        customer_name = str(request.get("customer_name") or "").strip()[:160]
        customer_document = str(request.get("customer_document") or "").strip()[:30]
        customer_id = request.get("customer_id") or None
        if customer_id is not None:
            customer_id = _str(customer_id, "Cliente")
        seller_id = request.get("seller_id") or None
        if seller_id is not None:
            seller_id = _str(seller_id, "Vendedor")
        override_id = (self.manager_override(request.get("manager_pin"), operator["id"])
                       if request.get("manager_pin") else None)
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
                previous["fiscal"] = nfce.fiscal_for_receipt(conn, previous["id"])
                return self._with_nfce(conn, self._receipt_data(conn, previous), previous)
            cash = self._own_shift(conn, operator["id"])
            seller_name = ""
            if seller_id is not None:
                seller = conn.execute("SELECT name FROM operators WHERE id=? AND active=1", (seller_id,)).fetchone()
                if not seller:
                    raise TerminalError("Vendedor não encontrado", 404)
                seller_name = seller["name"]
            lines, seen, priced_at = [], set(), _now_ms()
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
                              "gross_cents": gross, **self._line_promotion(conn, row, qty, priced_at)})
            fiscal_warnings = self._fiscal_warnings(conn, [line["product_id"] for line in lines])
            gross_total = sum(line["gross_cents"] for line in lines)
            promo_total = sum(line["promo_discount_cents"] for line in lines)
            base_total = gross_total - promo_total  # manual discount and its limits apply to the promotional price
            if base_total <= 0 or discount >= base_total:
                raise TerminalError("Desconto ou total inválido")
            configured_limit = int(self._meta(conn, "discount_max_bps") or 10000)
            if not 0 <= configured_limit <= 10000:
                configured_limit = 10000
            if discount * 10000 > base_total * configured_limit:
                raise TerminalError("Desconto acima do limite configurado pela administração", 403)
            role_limit = DISCOUNT_BPS.get(operator["role"], 0)
            if discount * 10000 > base_total * role_limit and not override_id:
                raise TerminalError("PIN de gerente necessário para este desconto", 403)
            _allocate_discount(lines, discount, base_total)
            total = base_total - discount
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
                        "promo_discount_cents": promo_total, "discount_cents": discount, "total_cents": total, "change_cents": change,
                        "items": lines, "payments": normalized_payments, "created_at": timestamp,
                        "status": "completed",
                        # Receipt context only: the event data is built from a fixed list below.
                        "station_id": cash["station_id"],
                        "station_name": self._station_name(conn, cash["station_id"])}
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
                 discount_cents,total_cents,change_cents,snapshot,status,created_at,voided_at,void_reason,
                 customer_document,station_id)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,NULL,?,?)""",
                         (sale_id, request_id, cash["id"], operator["id"], customer_name,
                          customer_id, gross_total, discount, total, change, _json(snapshot),
                          "completed", timestamp, customer_document, cash["station_id"]))
            event_data = {key: snapshot[key] for key in ("request_id", "cash_session_id", "created_at",
                                                       "gross_cents", "promo_discount_cents", "discount_cents", "total_cents",
                                                       "change_cents", "customer_id", "customer_name",
                                                       "customer_document", "operator_id", "seller_id",
                                                       "items", "payments")}
            self._event(conn, "sale.completed", sale_id, event_data)
            self._audit(conn, operator["id"], "sale.completed", sale_id,
                        f"manager_override={override_id}" if override_id else None)
            cfg = nfce.config(conn, self._meta)
            if nfce.enabled(cfg):
                # The DANFE is printed after the NFC-e answer, following the payment rule.
                nfce.save(conn, sale_id, "pending")
            else:
                self._queue_receipt(conn, snapshot)
            result = self._with_nfce(conn, self._receipt_data(conn, snapshot), snapshot)
            if fiscal_warnings:
                # Advice for the cashier screen only: never part of the sale event or the receipt.
                result["fiscal_warnings"] = fiscal_warnings
            return result

    @staticmethod
    def _fiscal_warnings(conn, product_ids):
        """Products of this sale that the cloud marked as not ready for a NFC-e (fiscal_ready=0)."""
        warnings = []
        for product_id in product_ids:
            row = conn.execute("SELECT name,fiscal FROM products WHERE id=? AND fiscal_ready=0", (product_id,)).fetchone()
            if row:
                try:
                    missing = json.loads(row["fiscal"] or "{}").get("missing") or []
                except (ValueError, AttributeError):
                    missing = []
                warnings.append({"product_id": product_id, "name": row["name"],
                                 "missing": [str(item)[:200] for item in missing[:3] if isinstance(item, str)]})
        return warnings

    def sales(self, limit=50):
        with self.connection() as conn:
            # Every sale of the shop, whichever cashier made it.
            return [dict(row) for row in conn.execute("""SELECT s.id,s.request_id,s.customer_name,s.total_cents,
                s.status,s.created_at,s.void_reason,s.station_id,COALESCE(st.name,'') station_name
                FROM sales s LEFT JOIN stations st ON st.id=s.station_id
                ORDER BY s.created_at DESC LIMIT ?""", (min(max(int(limit), 1), 100),))]

    def sale_detail(self, sale_id):
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM sales WHERE id=?", (sale_id,)).fetchone()
            if not row:
                raise TerminalError("Venda não encontrada", 404)
            result = json.loads(row["snapshot"])
            result["status"] = row["status"]
            result["void_reason"] = row["void_reason"]
            fiscal = nfce.fiscal_for_receipt(conn, sale_id)
            if fiscal:
                result["fiscal"] = fiscal  # DANFE NFC-e (F9 reprints it)
            return self._with_nfce(conn, self._receipt_data(conn, result), result)

    def _with_nfce(self, conn, result, snapshot):
        """NFC-e status and print decision for the screen (never part of the sale event)."""
        cfg = nfce.config(conn, self._meta)
        found = nfce.row(conn, snapshot["id"])
        if nfce.enabled(cfg) or found:
            result["nfce"] = {"enabled": nfce.enabled(cfg), "status": found["status"] if found else None,
                              "print": nfce.decision(cfg, snapshot.get("payments") or []),
                              "contingency": bool(cfg.get("contingency")), "wait_ms": int(cfg.get("wait_ms") or 8000),
                              "error": found["error"] if found else ""}
        return result

    # ---------- NFC-e (terminal/nfce.py; docs/NFCE.md) ----------

    def nfce_config(self):
        with self.connection() as conn:
            return nfce.config(conn, self._meta)

    def nfce_record(self, sale_id, status, data=None, xml=None, error="", uploaded=None):
        with self.transaction() as conn:
            if not conn.execute("SELECT 1 FROM sales WHERE id=?", (sale_id,)).fetchone():
                raise TerminalError("Venda não encontrada", 404)
            nfce.save(conn, sale_id, status, data, xml, error, uploaded)

    def nfce_contingency(self, sale_id, now=None):
        """Offline NFC-e built here (tpEmis 9). Raises TerminalError when it cannot be issued."""
        from shared.nfce import NfceError
        with self.transaction() as conn:
            row = conn.execute("SELECT snapshot,status FROM sales WHERE id=?", (sale_id,)).fetchone()
            if not row:
                raise TerminalError("Venda não encontrada", 404)
            current = nfce.row(conn, sale_id)
            if current and current["status"] in ("authorized", "contingency"):
                return current["data"]
            sale = json.loads(row["snapshot"])
            try:
                data, xml = nfce.build_contingency(conn, sale, nfce.config(conn, self._meta), now)
            except (NfceError, ValueError, KeyError) as exc:
                raise TerminalError(str(exc), 409, code="nfce_contingency_unavailable") from exc
            nfce.save(conn, sale_id, "contingency", data, xml, uploaded=False)
            self._audit(conn, sale.get("operator_id"), "nfce.contingency", sale_id,
                        f"NFC-e {data['number']} série {data['series']} em contingência")
            return data

    def nfce_store_reservation(self, value):
        with self.transaction() as conn:
            nfce.store_reservation(conn, value)

    def nfce_needs_reservation(self):
        with self.connection() as conn:
            try:
                last = int(self._meta(conn, "nfce_reserve_attempt_at") or 0)
            except (TypeError, ValueError):
                last = 0
            return nfce.needs_reservation(conn, nfce.config(conn, self._meta), last_attempt_ms=last)

    def nfce_reservation_request(self):
        """Body of POST nfce/reserve; also records the attempt (minimum interval between asks)."""
        with self.transaction() as conn:
            self._set_meta(conn, "nfce_reserve_attempt_at", str(_now_ms()))
            used = (nfce.reservation(conn) or {}).get("used") or []
            return {"used": [n for n in used if isinstance(n, int)][-2000:]}

    def nfce_work(self):
        with self.connection() as conn:
            return {"uploads": nfce.pending_uploads(conn), "retry": nfce.to_retry(conn),
                    "refresh": nfce.to_refresh(conn)}

    def nfce_summary(self):
        with self.connection() as conn:
            return nfce.summary(conn)

    def print_sale(self, sale_id, operator_id=None):
        """Print the DANFE (or the receipt) of a sale on the thermal printer."""
        return self.reprint(sale_id, operator_id)

    def void_sale(self, operator, sale_id, reason, manager_pin=None):
        reason = _str(reason, "Motivo do cancelamento", 240)
        self.ensure_sale_allowed()
        with self.connection() as conn:
            fiscal_row = nfce.row(conn, sale_id)
            cfg = nfce.config(conn, self._meta)
        fiscal_issued = bool(fiscal_row and fiscal_row["status"] in ("authorized", "contingency"))
        if fiscal_issued:
            # Cancelling a NFC-e: manager PIN, reason of 15+ characters and the SEFAZ deadline.
            if len(reason.strip()) < 15:
                raise TerminalError("A venda tem NFC-e: o motivo do cancelamento precisa ter pelo menos 15 caracteres", 400)
            authorized_at = fiscal_row["data"].get("authorized_at")
            minutes = int(cfg.get("cancel_minutes") or 30)
            if fiscal_row["status"] == "authorized" and authorized_at and _now_ms() - int(authorized_at) > minutes * 60000:
                raise TerminalError(f"Prazo de cancelamento da NFC-e encerrado ({minutes} minutos). "
                                    "Registre uma devolução.", 409, code="nfce_cancel_expired")
        authorizer = (operator["id"] if operator["role"] in MANAGER_ROLES
                      else self.manager_override(manager_pin, operator["id"]))
        with self.transaction() as conn:
            row = conn.execute("SELECT * FROM sales WHERE id=?", (sale_id,)).fetchone()
            if not row:
                raise TerminalError("Venda não encontrada", 404)
            if row["status"] == "voided":
                return self.sale_detail(sale_id)
            if fiscal_issued:
                # The cloud cancels the NFC-e when the sale.voided event arrives (reason and manager already checked).
                nfce.save(conn, sale_id, fiscal_row["status"], {"cancel_requested": True, "cancel_reason": reason})
            if (row["station_id"] or SERVER_STATION_ID) != self.current_station():
                # Each cashier answers for its own drawer: the refund would leave another one.
                other = self._station_name(conn, row["station_id"] or SERVER_STATION_ID) or "outro caixa"
                raise TerminalError(f"Esta venda foi feita em outro caixa ({other}). Cancele nele ou registre "
                                    "uma devolução aqui.", 409, code="other_station")
            if conn.execute("SELECT 1 FROM sale_returns WHERE sale_id=?", (sale_id,)).fetchone():
                raise TerminalError("Venda com devolução não pode ser cancelada; devolva os itens restantes", 409)
            original_cash = conn.execute("SELECT closed_at FROM cash_sessions WHERE id=?", (row["session_id"],)).fetchone()
            # A session this computer does not have (restored history) counts as closed.
            if original_cash is None or original_cash["closed_at"] is not None:
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
        supervisor = (operator["id"] if operator["role"] in MANAGER_ROLES
                      else self.manager_override(request.get("manager_pin"), operator["id"]))
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
                cash = self._own_shift(conn, operator["id"], manager=operator["role"] in MANAGER_ROLES)
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
            conn.execute("""INSERT INTO sale_returns
                (id,sale_id,session_id,operator_id,refund_method,total_cents,items,reason,created_at,station_id)
                VALUES (?,?,?,?,?,?,?,?,?,?)""",
                         (return_id, sale_id, session_id, operator["id"], refund, total, _json(items), reason,
                          timestamp, self.current_station()))
            data = {"sale_id": sale_id, "items": items, "total_cents": total, "refund_method": refund,
                    "reason": reason, "operator_id": operator["id"], "supervisor_id": supervisor,
                    "created_at": timestamp}
            if customer_id:
                data["customer_id"] = customer_id
            if session_id:
                data["cash_session_id"] = session_id
            self._event(conn, "sale.returned", return_id, data)
            self._audit(conn, operator["id"], "sale.returned", sale_id, f"{refund}; {total}; supervisor={supervisor}")
            names = {row["id"]: row["name"] for row in conn.execute("SELECT id,name FROM operators")}
            rows = [["pair", "Venda original", sale_id[:8]], ["rule"]]
            for item in items:
                line = lines[item["product_id"]]
                rows.append(["text", line["name"]])
                rows.append(["pair", f"  {_qty_text(item['quantity_milli'], line['unit'])} x devolvido", _brl(item["amount_cents"])])
            rows += [["rule"], ["pair", "TOTAL DEVOLVIDO", _brl(total), True],
                     ["pair", "Devolvido em", REFUND_LABELS[refund]], ["text", f"Motivo: {reason}"]]
            customer = (conn.execute("SELECT name,store_credit_cents,open_credit_cents FROM customers WHERE id=?",
                                     (customer_id,)).fetchone() if customer_id else None)
            if customer and refund == "store_credit":
                rows.append(["pair", "Crédito na loja disponível", _brl(customer["store_credit_cents"])])
            if customer and refund == "account":
                rows.append(["pair", "Fiado em aberto", _brl(customer["open_credit_cents"])])
            rows += [["pair", "Operador", names.get(operator["id"], "")], ["pair", "Autorizado por", names.get(supervisor, "")]]
            layout, job = self._queue_voucher(conn, {"title": "DEVOLUÇÃO", "created_at": timestamp, "rows": rows,
                                                     "reference": f"Devolução: {return_id[:16]}",
                                                     "customer_name": customer["name"] if customer else snapshot.get("customer_name"),
                                                     "signature": refund == "cash"})
            return {"id": return_id, "sale_id": sale_id, "refund_method": refund, "total_cents": total, "items": items,
                    "receipt": layout, "print_job_id": job}

    def _queue_voucher(self, conn, doc):
        """Layout for the on-screen preview, plus a print job when a receipt printer is set."""
        raw = self._meta(conn, "issuer_profile")
        doc = {**doc, "issuer": json.loads(raw) if raw else {"legal_name": self._meta(conn, "tenant_name") or ""}}
        setting = self._print_setting(conn, "receipt")
        width = 58 if setting and setting["format"] == "escpos58" else 80
        layout = voucher_layout(doc, width)
        job = self._queue_print(conn, "receipt", layout_bytes(layout), setting) if setting else None
        return layout, job

    def receive_credit(self, operator, request):
        """Customer pays fiado at the counter; cash goes into the drawer."""
        self.ensure_sale_allowed()
        if operator["role"] not in SELL_ROLES:
            raise TerminalError("Papel sem permissão de recebimento", 403)
        receipt_id = _str(request.get("receipt_id"), "Identificador do recebimento")
        customer_id = _str(request.get("customer_id"), "Cliente")
        method = request.get("method")
        if method not in ("cash", "pix", "card_credit", "card_debit"):
            raise TerminalError("Forma de recebimento inválida")
        amount = _positive_int(request.get("amount_cents"), "Valor recebido")
        with self.transaction() as conn:
            old = conn.execute("SELECT * FROM credit_receipts WHERE id=?", (receipt_id,)).fetchone()
            if old:
                if (old["customer_id"], old["amount_cents"], old["method"]) != (customer_id, amount, method):
                    raise TerminalError("Identificador já usado para outro recebimento", 409)
                return {"id": receipt_id, "amount_cents": amount, "replayed": True}
            cash = self._own_shift(conn, operator["id"])
            customer = conn.execute("SELECT * FROM customers WHERE id=? AND active=1", (customer_id,)).fetchone()
            if not customer:
                raise TerminalError("Cliente não encontrado", 404)
            if amount > customer["open_credit_cents"]:
                raise TerminalError(f"Valor maior que o fiado em aberto ({_brl(customer['open_credit_cents'])})", 409)
            timestamp = _now_ms()
            conn.execute("""INSERT INTO credit_receipts
                (id,customer_id,session_id,method,amount_cents,operator_id,created_at,station_id)
                VALUES (?,?,?,?,?,?,?,?)""",
                         (receipt_id, customer_id, cash["id"], method, amount, operator["id"], timestamp,
                          cash["station_id"]))
            conn.execute("UPDATE customers SET open_credit_cents=open_credit_cents-? WHERE id=?", (amount, customer_id))
            self._event(conn, "credit.received", receipt_id,
                        {"customer_id": customer_id, "amount_cents": amount, "method": method,
                         "operator_id": operator["id"], "cash_session_id": cash["id"], "created_at": timestamp})
            self._audit(conn, operator["id"], "credit.received", customer_id, f"{method}; {amount}")
            remaining = customer["open_credit_cents"] - amount
            layout, job = self._queue_voucher(conn, {
                "title": "RECEBIMENTO DE FIADO", "created_at": timestamp, "reference": f"Recibo: {receipt_id[:16]}",
                "customer_name": customer["name"],
                "rows": [["pair", "Fiado anterior", _brl(customer["open_credit_cents"])],
                         ["pair", "VALOR RECEBIDO", _brl(amount), True],
                         ["pair", "Forma", PAYMENT_LABELS.get(method, method)],
                         ["pair", "Saldo em aberto", _brl(remaining)],
                         ["pair", "Operador", operator.get("name", "")]]})
            return {"id": receipt_id, "amount_cents": amount, "open_credit_cents": remaining,
                    "receipt": layout, "print_job_id": job}

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
                    "cash_pauses": [dict(row) for row in conn.execute("SELECT * FROM cash_pauses ORDER BY started_at")],
                    "sales": sales,
                    # Never the token of an additional cashier, not even its hash.
                    "stations": [dict(row) for row in conn.execute(
                        """SELECT id,name,created_at,last_seen_at,revoked_at,hostname,app_version,last_address
                           FROM stations ORDER BY created_at,id""")],
                    "audit": [dict(row) for row in conn.execute("SELECT * FROM audit ORDER BY created_at")],
                    "pending_events": [json.loads(row["payload"]) for row in conn.execute(
                        "SELECT payload FROM outbox WHERE acked_at IS NULL ORDER BY seq")],
                    # Received by the cloud but not applied there: kept for manual review.
                    "quarantined_events": [
                        {"reason": row["reason"], "message": row["message"], "status": row["status"],
                         "event": json.loads(row["payload"])} for row in conn.execute(
                            """SELECT q.reason,q.message,q.status,o.payload FROM sync_quarantine q
                               JOIN outbox o ON o.id=q.event_id ORDER BY o.seq""")]}

    def has_local_activity(self):
        """True once this terminal created something worth an automatic backup."""
        with self.connection() as conn:
            return (any(conn.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone()
                        for table in ("sales", "cash_sessions", "cash_movements"))
                    or conn.execute("SELECT 1 FROM outbox WHERE event_type != 'terminal.config' LIMIT 1"
                                    ).fetchone() is not None)

    def backup_status(self):
        return {"directory": str(self.path.parent / "backups"), "last_backup_at": self.meta().get("last_backup_at"),
                "copies": [{"name": copy["name"], "size_bytes": copy["size_bytes"]}
                           for copy in self.local_backups(self.path)]}

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
                    # The CSC never goes to backups (the copy asks the cloud again; docs/NFCE.md).
                    nfce.strip_secrets(target)
                    target.commit()
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
        """Printers of the cashier that is asking: each computer prints on its own."""
        with self.connection() as conn:
            return [dict(row) for row in conn.execute(
                "SELECT kind,driver,target,format FROM print_settings WHERE station_id=? ORDER BY kind",
                (self.current_station(),))]

    def save_printer(self, kind, driver, target, fmt, operator_id=None):
        if kind not in ("receipt", "label") or driver not in ("tcp", "windows_raw", "cups", "file"):
            raise TerminalError("Configuração de impressora inválida")
        if kind == "label" and fmt == "escpos50x30":
            raise TerminalError(LABEL_PRINTER_REQUIRED, 409)
        if (kind == "receipt" and fmt not in ("escpos58", "escpos80")) or (
                kind == "label" and fmt not in LABEL_FORMATS):
            raise TerminalError("Formato de impressão inválido")
        target = _str(target, "Destino", 260)
        with self.transaction() as conn:
            conn.execute("INSERT INTO print_settings(kind,driver,target,format,station_id) VALUES (?,?,?,?,?) "
                         "ON CONFLICT(station_id,kind) DO UPDATE SET "
                         "driver=excluded.driver,target=excluded.target,format=excluded.format",
                         (kind, driver, target, fmt, self.current_station()))
            self._audit(conn, operator_id, "print.settings_changed", kind, f"{driver}:{fmt}")
        try:
            self.ensure_config_event()
        except (TerminalError, sqlite3.Error):
            pass  # the worker tries again on its next pass
        return {"kind": kind, "driver": driver, "target": target, "format": fmt}

    # ---------- phase 2: recovery on a new computer, transfer and second-disk copy ----------

    def _store_cloud_extras(self, conn, response, mirror_key=False):
        """What the cloud announces besides the license: its capabilities and the backup key."""
        capabilities = response.get("capabilities") if isinstance(response, dict) else None
        if isinstance(capabilities, list) and all(isinstance(item, str) for item in capabilities):
            self._set_meta(conn, "cloud_capabilities", _json(capabilities[:50]))
        key = response.get("mirror_key") if mirror_key and isinstance(response, dict) else None
        if (isinstance(key, dict) and isinstance(key.get("key_id"), str) and key["key_id"]
                and isinstance(key.get("key"), str) and len(key["key"]) == 44):
            self._set_meta(conn, "mirror_key", _json({"key_id": key["key_id"], "key": key["key"]}))

    def _restore_active(self, conn):
        return (self._meta(conn, "restore_state") or "done") != "done"

    def ensure_not_restoring(self):
        with self.connection() as conn:
            if self._restore_active(conn):
                raise TerminalError(RESTORE_BLOCKED, 409)

    @staticmethod
    def _meta_json(raw, fallback):
        try:
            value = json.loads(raw) if raw else fallback
        except ValueError:
            return fallback
        return value if isinstance(value, type(fallback)) else fallback

    def _restore_progress(self, conn):
        stored = self._meta_json(self._meta(conn, "restore_progress"), {})
        return {key: _int(stored.get(key)) for key in RESTORE_COUNTERS}

    def _restore_status(self, conn):
        state = self._meta(conn, "restore_state") or "done"
        progress = self._restore_progress(conn)
        position, total = progress["position"], max(progress["total"], progress["position"])
        if state in ("done", "awaiting_backup"):
            percent = 100
        elif state == "importing":
            percent = 99
        else:
            percent = min(99, position * 100 // total) if total else 0
        imported = self._meta_json(self._meta(conn, "restore_import"), {})
        return {"state": state, "active": state != "done", "pass": int(self._meta(conn, "restore_pass") or 1),
                "applied": position, "total": total, "percent": percent,
                "counts": {key: progress[key] for key in RESTORE_COUNTERS if key not in ("position", "total")},
                "error": self._meta(conn, "restore_error") or "",
                "paused": self._meta(conn, "restore_paused") == "1",
                "started_at": int(self._meta(conn, "restore_started_at") or 0) or None,
                "finished_at": int(self._meta(conn, "restore_finished_at") or 0) or None,
                "import": imported or None}

    def restore_status(self):
        with self.connection() as conn:
            return self._restore_status(conn)

    def _restore_restart(self, conn):
        """Start the download over; after too many tries wait for the person to ask again."""
        attempts = int(self._meta(conn, "restore_attempts") or 0) + 1
        self._set_meta(conn, "restore_attempts", str(attempts))
        self._set_meta(conn, "restore_cursor", "")
        self._set_meta(conn, "restore_progress", "")
        if attempts >= RESTORE_MAX_ATTEMPTS:
            self._set_meta(conn, "restore_paused", "1")
            self._set_meta(conn, "restore_error", RESTORE_MISMATCH)

    def restore_restart(self):
        """The cloud refused the cursor: begin again from the first page, counting one attempt."""
        with self.transaction() as conn:
            if self._meta(conn, "restore_state") in ("pending", "restoring"):
                self._restore_restart(conn)
            return self._restore_status(conn)

    def restore_retry(self):
        """"Tentar de novo": forget the pause, the error, the attempts and the cursor."""
        with self.transaction() as conn:
            if not self._restore_active(conn):
                raise TerminalError("Restauração não está em andamento", 409)
            for key in ("restore_paused", "restore_error", "restore_attempts"):
                self._set_meta(conn, key, "")
            if self._meta(conn, "restore_state") in ("pending", "restoring"):
                self._set_meta(conn, "restore_cursor", "")
                self._set_meta(conn, "restore_progress", "")
            return self._restore_status(conn)

    def set_restore_error(self, error=""):
        with self.transaction() as conn:
            if self._restore_active(conn) and self._meta(conn, "restore_paused") != "1":
                self._set_meta(conn, "restore_error", str(error or "")[:300])

    def _finish_restore(self, conn, detail=None):
        self._set_meta(conn, "restore_state", "done")
        self._set_meta(conn, "restore_finished_at", str(_now_ms()))
        for key in ("restore_cursor", "restore_error", "restore_paused"):
            self._set_meta(conn, key, "")
        self._audit(conn, None, "terminal.restored", self._meta(conn, "terminal_id"), detail)

    def _ensure_abandonable(self, conn):
        if not self._restore_active(conn):
            raise TerminalError("Restauração não está em andamento", 409)
        if conn.execute("SELECT 1 FROM outbox WHERE acked_at IS NULL LIMIT 1").fetchone():
            raise TerminalError(RESTORE_ABANDON_PENDING, 409)

    def ensure_restore_abandonable(self):
        """Checked before the cloud is asked to give the terminal back to the old computer."""
        with self.connection() as conn:
            self._ensure_abandonable(conn)

    def restore_abandon(self):
        """"Desistir da recuperação": this computer goes back to the first screen.

        Only while the recovery is still running and nothing waits in the queue: the cashier
        was blocked the whole time, so every row here came from the cloud and stays there.
        """
        with self.transaction() as conn:
            self._ensure_abandonable(conn)
            self._clear_company_cache(conn)  # also the additional cashiers, their codes and printers
            for table in ("sale_returns", "credit_receipts", "sales", "cash_movements", "cash_pauses", "cash_closings",
                          "cash_sessions",
                          "suspended_carts", "print_jobs", "outbox"):
                conn.execute(f"DELETE FROM {table}")
            self._clear_connection_meta(conn)
            # The cloud may still list the old identity as the owner of the terminal that was
            # given up; a new one lets this computer recover another cashier of the same company.
            self._set_meta(conn, "device_id", _uuid())
            self._audit(conn, None, "terminal.unpaired", None, "Recuperação abandonada")
        # Copies made since the pairing hold only what was just removed; left behind they
        # would count as another company's history and refuse the next pairing.
        for copy in (self.path.parent / "backups").glob("terminal-*.sqlite3"):
            try:
                copy.unlink()
            except OSError:
                pass
        return {"paired": False}

    def restore_skip_backup(self):
        """"Não tenho, continuar": what came from the cloud is all there is."""
        with self.transaction() as conn:
            if self._meta(conn, "restore_state") != "awaiting_backup":
                raise TerminalError("A recuperação não está esperando a cópia de segurança", 409)
            self._finish_restore(conn, "sem cópia de segurança")
            return self._restore_status(conn)

    def restore_second_pass(self):
        """The queue read from the backup reached the cloud: download everything once more."""
        with self.transaction() as conn:
            if self._meta(conn, "restore_state") != "importing":
                return False
            if conn.execute("SELECT 1 FROM outbox WHERE acked_at IS NULL LIMIT 1").fetchone():
                return False
            self._set_meta(conn, "restore_state", "restoring")
            self._set_meta(conn, "restore_pass", "2")
            for key in ("restore_cursor", "restore_progress", "restore_attempts", "restore_error"):
                self._set_meta(conn, key, "")
            return True

    def apply_restore(self, page, sent_cursor=""):
        """Apply one page of GET /terminal/restore. Idempotent; creates no event.

        Nothing here touches the outbox, the print queue, product stock or customer
        balances: those come from the bootstrap. `sent_cursor` is the cursor the page
        was asked with; a page for any other position is ignored.
        """
        if not isinstance(page, dict) or not isinstance(page.get("records"), list):
            raise TerminalError("Página de restauração inválida", 502)
        with self.transaction() as conn:
            state = self._meta(conn, "restore_state")
            if state not in ("pending", "restoring"):
                raise TerminalError("Restauração não está em andamento", 409)
            if (self._meta(conn, "restore_cursor") or "") != (sent_cursor or ""):
                return {"skipped": True}
            progress = self._restore_progress(conn)
            names = {"operators": {row["id"]: row["name"] for row in conn.execute("SELECT id,name FROM operators")},
                     "payments": {row["id"]: row["name"] for row in self._payment_config(conn)
                                  if row.get("kind") == "other"}}
            applied = warnings = 0
            for record in page["records"]:
                progress["position"] += 1
                handler = {"cash_session": self._restore_cash_session, "sale": self._restore_sale,
                           "credit_receipt": self._restore_credit_receipt,
                           "config": self._restore_config}.get(record.get("kind") if isinstance(record, dict) else None)
                if handler is None:
                    continue
                try:
                    warnings += handler(conn, record, progress, names)
                    applied += 1
                except (KeyError, TypeError, ValueError, AttributeError, sqlite3.IntegrityError):
                    warnings += 1
            remote = page.get("progress") if isinstance(page.get("progress"), dict) else {}
            progress["total"] = max(_int(remote.get("total")), progress["position"])
            next_cursor = page.get("next_cursor") if isinstance(page.get("next_cursor"), str) else ""
            finished = page.get("done") is True
            if not finished and not next_cursor:
                raise TerminalError("Página de restauração sem continuação", 502)
            self._set_meta(conn, "restore_state", "restoring")
            self._set_meta(conn, "restore_error", "")
            self._set_meta(conn, "restore_cursor", "" if finished else next_cursor)
            self._set_meta(conn, "restore_progress", _json(progress))
            done = False
            if finished:
                totals = page.get("totals") if isinstance(page.get("totals"), dict) else {}
                if self._restore_totals_match(progress, totals):
                    done = True
                    self._set_meta(conn, "restore_totals", _json(totals))
                    self._set_meta(conn, "restore_attempts", "")
                    if (self._meta(conn, "restore_pass") or "1") == "2":
                        self._finish_restore(conn, f"{progress['sales']} vendas; {progress['cash_sessions']} caixas")
                    else:
                        self._set_meta(conn, "restore_state", "awaiting_backup")
                else:
                    self._restore_restart(conn)
            return {"applied": applied, "state": self._meta(conn, "restore_state"), "done": done,
                    "warnings": warnings}

    @staticmethod
    def _restore_totals_match(progress, totals):
        def remote(section, field):
            value = totals.get(section)
            return _int(value.get(field)) if isinstance(value, dict) else 0
        return all(progress[local] == remote(section, field) for local, section, field in (
            ("sales", "sales", "count"), ("sales_cents", "sales", "total_cents"),
            ("returns", "returns", "count"), ("returns_cents", "returns", "total_cents"),
            ("credit_receipts", "credit_receipts", "count"),
            ("credit_receipts_cents", "credit_receipts", "total_cents"),
            ("cash_sessions", "cash_sessions", "count"),
            ("cash_movements", "cash_movements", "count"),
            ("cash_movements_cents", "cash_movements", "total_cents"),
            ("config", "config", "count")))

    @staticmethod
    def _restore_cash_session(conn, record, progress, _names):
        movements = record.get("movements") if isinstance(record.get("movements"), list) else []
        progress["cash_sessions"] += 1
        progress["cash_movements"] += len(movements)
        progress["cash_movements_cents"] += sum(_int(row.get("amount_cents")) for row in movements
                                                if isinstance(row, dict))
        session_id, opened_at = str(record["id"]), int(record["opened_at"])
        station_id = str(record.get("station_id") or SERVER_STATION_ID)[:64]
        # A cashier known only from the history comes back as removed: the stations the shop still
        # has came first, in the config record. A removed one takes no place of the plan, still
        # shows its open cashier for the manager to close, and a real computer links again with
        # the same identity.
        created = _now_ms()
        conn.execute("INSERT OR IGNORE INTO stations(id,name,token_hash,created_at,revoked_at) VALUES (?,?,NULL,?,?)",
                     (station_id, str(record.get("station_name") or "Caixa")[:STATION_NAME_MAX], created,
                      None if station_id == SERVER_STATION_ID else created))
        values = [session_id, str(record.get("operator_id") or ""), int(record.get("opening_cents") or 0), opened_at,
                  record.get("closed_at"), record.get("counted_cents"), record.get("expected_cents"), station_id]
        upsert = """INSERT INTO cash_sessions
                    (id,operator_id,opening_cents,opened_at,closed_at,counted_cents,expected_cents,station_id)
                    VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
                    closed_at=excluded.closed_at,counted_cents=excluded.counted_cents,
                    expected_cents=excluded.expected_cents"""
        warnings = 0
        try:
            conn.execute(upsert, values)
        except sqlite3.IntegrityError:
            # Each station admits a single open cashier: keep the record, as closed.
            values[4] = opened_at
            conn.execute(upsert, values)
            warnings = 1
        for row in movements:
            conn.execute("INSERT OR IGNORE INTO cash_movements VALUES (?,?,?,?,?,?,?)",
                         (str(row["id"]), session_id, str(row["kind"]), int(row["amount_cents"]),
                          str(row.get("reason") or ""), str(row.get("operator_id") or ""), int(row["created_at"])))
        return warnings

    @staticmethod
    def _restore_sale(conn, record, progress, names):
        data = record["data"]
        returns = record.get("returns") if isinstance(record.get("returns"), list) else []
        progress["sales"] += 1
        progress["sales_cents"] += _int(data.get("total_cents"))
        progress["returns"] += len(returns)
        progress["returns_cents"] += sum(_int(row.get("total_cents")) for row in returns if isinstance(row, dict))
        sale_id = str(record["id"])
        station_id = str(record.get("station_id") or SERVER_STATION_ID)[:64]
        voided = record.get("status") == "voided"
        snapshot = {"id": sale_id, **data, "seller_name": names["operators"].get(data.get("seller_id"), ""),
                    "status": "voided" if voided else "completed"}
        payment_names = {payment["payment_method_id"]: names["payments"][payment["payment_method_id"]]
                         for payment in data.get("payments") or []
                         if isinstance(payment, dict) and payment.get("payment_method_id") in names["payments"]}
        if payment_names:
            snapshot["payment_names"] = payment_names
        if voided:
            snapshot["void_reason"] = record.get("void_reason") or ""
        warnings = 0
        try:
            conn.execute(
                """INSERT INTO sales (id,request_id,session_id,operator_id,customer_name,customer_id,gross_cents,
                       discount_cents,total_cents,change_cents,snapshot,status,created_at,voided_at,void_reason,
                       customer_document,station_id)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET status=excluded.status,voided_at=excluded.voided_at,
                       void_reason=excluded.void_reason,snapshot=excluded.snapshot""",
                (sale_id, str(data.get("request_id") or sale_id), str(data.get("cash_session_id") or ""),
                 str(data.get("operator_id") or ""), data.get("customer_name"), data.get("customer_id"),
                 int(data.get("gross_cents") or 0), int(data.get("discount_cents") or 0),
                 int(data.get("total_cents") or 0), int(data.get("change_cents") or 0), _json(snapshot),
                 snapshot["status"], int(data.get("created_at") or 0),
                 record.get("voided_at") if voided else None,
                 (record.get("void_reason") or "") if voided else None, data.get("customer_document"),
                 station_id))
        except sqlite3.IntegrityError:
            warnings = 1  # same request_id under another sale: the local record stays
        for row in returns:
            conn.execute("""INSERT OR IGNORE INTO sale_returns
                            (id,sale_id,session_id,operator_id,refund_method,total_cents,items,reason,created_at,
                             station_id) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                         (str(row["id"]), sale_id, row.get("cash_session_id") or None,
                          str(row.get("operator_id") or ""), str(row["refund_method"]), int(row["total_cents"]),
                          _json(row.get("items") or []), str(row.get("reason") or ""), int(row["created_at"]),
                          str(row.get("station_id") or SERVER_STATION_ID)[:64]))
        return warnings

    @staticmethod
    def _restore_credit_receipt(conn, record, progress, _names):
        progress["credit_receipts"] += 1
        progress["credit_receipts_cents"] += _int(record.get("amount_cents"))
        conn.execute("""INSERT OR IGNORE INTO credit_receipts
                        (id,customer_id,session_id,method,amount_cents,operator_id,created_at,station_id)
                        VALUES (?,?,?,?,?,?,?,?)""",
                     (str(record["id"]), str(record["customer_id"]), str(record.get("cash_session_id") or ""),
                      str(record["method"]), int(record["amount_cents"]), str(record.get("operator_id") or ""),
                      int(record["created_at"]), str(record.get("station_id") or SERVER_STATION_ID)[:64]))
        return 0

    def _restore_config(self, conn, record, progress, _names):
        progress["config"] += 1
        data = record["data"]
        for row in data.get("print_settings") or []:
            # Only kinds this computer has not configured yet: a printer chosen here wins.
            if (isinstance(row, dict) and row.get("driver") in CONFIG_DRIVERS
                    and row.get("format") in CONFIG_FORMATS.get(row.get("kind"), ())
                    and isinstance(row.get("target"), str) and 0 < len(row["target"].strip()) <= 260):
                conn.execute("INSERT OR IGNORE INTO print_settings(kind,driver,target,format,station_id) "
                             "VALUES (?,?,?,?,?)",
                             (row["kind"], row["driver"], row["target"].strip(), row["format"], SERVER_STATION_ID))
        stations = data.get("stations") if isinstance(data.get("stations"), list) else []
        for row in stations[:64]:
            # Identity only: the token stayed on the old computer, so each one is linked again.
            if (isinstance(row, dict) and isinstance(row.get("id"), str) and 0 < len(row["id"]) <= 64
                    and row["id"] != SERVER_STATION_ID and isinstance(row.get("name"), str)):
                now = _now_ms()
                conn.execute("INSERT OR IGNORE INTO stations(id,name,token_hash,created_at,revoked_at) "
                             "VALUES (?,?,NULL,?,?)",
                             (row["id"], row["name"].strip()[:STATION_NAME_MAX] or "Caixa", now,
                              now if row.get("revoked") is True else None))
        self._set_meta(conn, "config_synced_hash", _config_hash(data))
        return 0

    def import_outbox_from_mirror(self, events):
        """Put back in the queue what the old computer had not delivered (read from the backup copy).

        The events go up untouched and the cloud deduplicates them by event_id. No local
        effect is applied here: the local state comes from the second restore pass.
        """
        if not isinstance(events, list):
            raise TerminalError("Cópia de segurança inválida")
        result = {"imported": 0, "already_present": 0, "foreign": 0, "invalid": 0, "first_seq": None, "last_seq": None}
        with self.transaction() as conn:
            state = self._meta(conn, "restore_state")
            # Another copy may still be read while the first one goes up or is being checked
            # (second pass): the queue never takes the same event twice.
            again = state == "restoring" and (self._meta(conn, "restore_pass") or "1") == "2"
            if state not in ("awaiting_backup", "importing") and not again:
                raise TerminalError("A recuperação não está esperando a cópia de segurança", 409)
            identity = {key: self._meta(conn, key) for key in ("tenant_id", "shop_id", "terminal_id")}
            for item in events:
                event = item.get("event") if isinstance(item, dict) else None
                if (not isinstance(event, dict) or not isinstance(event.get("event_id"), str)
                        or not EVENT_ID.fullmatch(event["event_id"]) or event["event_id"] != item.get("event_id")
                        or not isinstance(event.get("type"), str) or not event["type"]
                        or not isinstance(event.get("aggregate_id"), str) or not isinstance(event.get("data"), dict)
                        or not isinstance(event.get("identity"), dict)):
                    result["invalid"] += 1
                    continue
                if {key: event["identity"].get(key) for key in identity} != identity:
                    result["foreign"] += 1
                    continue
                if conn.execute("SELECT 1 FROM outbox WHERE id=?", (event["event_id"],)).fetchone():
                    result["already_present"] += 1
                    continue
                seq = conn.execute("SELECT COALESCE(MAX(seq),0)+1 FROM outbox").fetchone()[0]
                created_at = item.get("created_at") if type(item.get("created_at")) is int else _now_ms()
                conn.execute("INSERT INTO outbox(id,event_type,aggregate_id,payload,created_at,acked_at,seq) "
                             "VALUES (?,?,?,?,?,NULL,?)",
                             (event["event_id"], event["type"], event["aggregate_id"], _json(event), created_at, seq))
                result["imported"] += 1
                result["first_seq"] = result["first_seq"] or seq
                result["last_seq"] = seq
            if result["imported"]:
                self._local.outbox_dirty = True
                self._set_meta(conn, "restore_state", "importing")
                # What the screen and the support see is the sum of every copy read.
                before = self._meta_json(self._meta(conn, "restore_import"), {})
                total = dict(result)
                for key in ("imported", "already_present", "foreign", "invalid"):
                    total[key] += _int(before.get(key))
                total["first_seq"] = before.get("first_seq") or result["first_seq"]
                self._set_meta(conn, "restore_import", _json(total))
                self._audit(conn, None, "restore.mirror_imported", identity["terminal_id"],
                            f"{result['imported']} registro(s) da cópia de segurança; "
                            f"{result['foreign']} de outra loja; {result['invalid']} inválido(s)")
        return result

    def mark_transferred(self, send_only_until=None):
        """The cloud said this terminal now lives on another computer.

        Without a date the cloud no longer takes the queue of this computer (the
        send-only period ended or was closed in the panel).
        """
        value = send_only_until if isinstance(send_only_until, str) else ""
        closed = "" if value else "1"
        with self.connection() as conn:
            if (self._meta(conn, "transferred_at") and (self._meta(conn, "send_only_until") or "") == value
                    and (self._meta(conn, "send_only_closed") or "") == closed):
                return
        with self.transaction() as conn:
            if not self._meta(conn, "transferred_at"):
                self._set_meta(conn, "transferred_at", str(_now_ms()))
                self._audit(conn, None, "terminal.transferred", self._meta(conn, "terminal_id"))
            if (self._meta(conn, "send_only_until") or "") != value:
                self._set_meta(conn, "send_only_until", value)
            if (self._meta(conn, "send_only_closed") or "") != closed:
                self._set_meta(conn, "send_only_closed", closed)
                if closed:
                    self._audit(conn, None, "terminal.send_only_closed", self._meta(conn, "terminal_id"))

    def clear_transferred(self):
        """The cloud accepted this computer as the cashier again (the hand-over was given up)."""
        with self.connection() as conn:
            if not self._meta(conn, "transferred_at"):
                return False
        with self.transaction() as conn:
            if not self._meta(conn, "transferred_at"):
                return False
            for key in ("transferred_at", "send_only_until", "send_only_closed"):
                conn.execute("DELETE FROM meta WHERE key=?", (key,))
            self._audit(conn, None, "terminal.transfer_undone", self._meta(conn, "terminal_id"))
        return True

    def is_transferred(self):
        with self.connection() as conn:
            return bool(self._meta(conn, "transferred_at"))

    def send_only_closed(self):
        with self.connection() as conn:
            return bool(self._meta(conn, "transferred_at")) and self._meta(conn, "send_only_closed") == "1"

    def _config_candidate(self, conn, enabled):
        """(terminal_id, data, digest) of the terminal.config still to be sent, or None."""
        terminal_id = self._meta(conn, "terminal_id")
        if (not terminal_id or not self._meta(conn, "token") or self._restore_active(conn)
                or self._meta(conn, "transferred_at")
                or "terminal-config" not in self._meta_json(self._meta(conn, "cloud_capabilities"), [])):
            return None
        data = {"version": 1, "app_version": (os.environ.get("PDV_VERSION") or paths.source_version())[:80],
                # Only the printers of the main cashier: the others belong to each computer.
                "print_settings": [dict(row) for row in conn.execute(
                    "SELECT kind,driver,target,format FROM print_settings WHERE station_id=? ORDER BY kind",
                    (SERVER_STATION_ID,))
                                   if row["driver"] in CONFIG_DRIVERS
                                   and row["format"] in CONFIG_FORMATS.get(row["kind"], ())][:4]}
        if enabled is not None:
            data["mirror"] = {"enabled": bool(enabled)}
        if "stations" in self._meta_json(self._meta(conn, "cloud_capabilities"), []):
            stations = [{"id": row["id"], "name": row["name"], "revoked": row["revoked_at"] is not None}
                        for row in conn.execute("SELECT id,name,revoked_at FROM stations WHERE id != ? "
                                                "ORDER BY created_at,id LIMIT 64", (SERVER_STATION_ID,))]
            if stations:
                data["stations"] = stations
        digest = _config_hash(data)
        synced = self._meta(conn, "config_synced_hash")
        if not synced and not data["print_settings"] and not enabled and not data.get("stations"):
            return None  # nothing set up yet: a fresh pairing sends nothing and stays easy to correct
        return None if digest == synced else (terminal_id, data, digest)

    def ensure_config_event(self):
        """Queue terminal.config when the printing or backup-copy setup changed since the last one sent."""
        enabled = _mirror_call("is_enabled", self)  # None when this build has no second-disk copy
        with self.connection() as conn:  # the worker asks on every pass: no write lock unless needed
            if self._config_candidate(conn, enabled) is None:
                return None
        with self.transaction() as conn:
            candidate = self._config_candidate(conn, enabled)
            if candidate is None:
                return None
            terminal_id, data, digest = candidate
            data["updated_at"] = _now_ms()
            event_id = self._event(conn, "terminal.config", terminal_id, data)
            self._set_meta(conn, "config_synced_hash", digest)
            return event_id

    def _queue_receipt(self, conn, snapshot):
        setting = self._print_setting(conn, "receipt")
        if not setting:
            return None
        snapshot = self._receipt_context(conn, snapshot)
        width = 58 if setting["format"] == "escpos58" else 80
        return self._queue_print(conn, "receipt", receipt_bytes(snapshot, width), setting)

    def _queue_print(self, conn, kind, payload, setting):
        job_id, timestamp = _uuid(), _now_ms()
        # The job belongs to the cashier that asked: its own computer prints it.
        conn.execute("""INSERT INTO print_jobs
            (id,kind,payload,driver,target,status,attempts,error,created_at,updated_at,station_id)
            VALUES (?,?,?,?,?,'queued',0,NULL,?,?,?)""",
                     (job_id, kind, payload, setting["driver"], setting["target"], timestamp, timestamp,
                      self.current_station()))
        return job_id

    def queue_label(self, product_id, quantity=1):
        quantity = _positive_int(quantity, "Quantidade de etiquetas")
        if quantity > 999:
            raise TerminalError("A quantidade de etiquetas deve ficar entre 1 e 999")
        with self.transaction() as conn:
            product = conn.execute("SELECT * FROM products WHERE id=?", (product_id,)).fetchone()
            setting = self._print_setting(conn, "label")
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
            setting = self._print_setting(conn, "receipt")
            if not setting:
                raise TerminalError("Configure impressora de recibos antes", 409)
            job_id = self._queue_print(conn, "receipt", receipt_bytes(snapshot,
                58 if setting["format"] == "escpos58" else 80), setting)
            self._audit(conn, operator_id, "sale.reprint", sale_id)
            return {"id": job_id, "status": "queued"}

    def print_jobs(self):
        with self.connection() as conn:
            return [dict(row) for row in conn.execute("""SELECT id,kind,driver,target,status,attempts,error,created_at,
                updated_at FROM print_jobs WHERE station_id=? ORDER BY created_at DESC LIMIT 100""",
                                                      (self.current_station(),))]

    def begin_print(self):
        with self.transaction() as conn:
            while True:
                row = conn.execute("SELECT * FROM print_jobs WHERE status='queued' AND station_id=? "
                                   "ORDER BY created_at,rowid LIMIT 1", (self.current_station(),)).fetchone()
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
            conn.execute("UPDATE print_jobs SET status=?,error=?,updated_at=? WHERE id=? AND station_id=?",
                         ("uncertain" if error else "printed", str(error)[:300] if error else None,
                          _now_ms(), job_id, self.current_station()))

    def recover_print(self):
        """The agent of this cashier restarted: what it was sending may or may not have come out."""
        with self.transaction() as conn:
            return conn.execute(
                "UPDATE print_jobs SET status='uncertain',error='Processo interrompido durante impressão',"
                "updated_at=? WHERE status='sending' AND station_id=?",
                (_now_ms(), self.current_station())).rowcount

    def retry_print(self, operator_id, job_id):
        with self.transaction() as conn:
            row = conn.execute("SELECT status,kind,payload FROM print_jobs WHERE id=? AND station_id=?",
                               (job_id, self.current_station())).fetchone()
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

    # ---------- phase 3: additional cashiers (stations) of the shop ----------

    def _station_limit(self, conn):
        """Additional cashiers the signed license allows (the main one is never counted)."""
        claims = self._meta_json(self._meta(conn, "lease"), {}).get("payload")
        limit = 0
        hub = not (isinstance(claims, dict) and claims.get("hub") is False)
        if (isinstance(claims, dict) and hub
                and "stations" in self._meta_json(self._meta(conn, "cloud_capabilities"), [])):
            limit = self._claim_stations(claims)
        used = conn.execute("SELECT COUNT(*) FROM stations WHERE id != ? AND revoked_at IS NULL",
                            (SERVER_STATION_ID,)).fetchone()[0]
        # hub False: the plan may include stations, but another terminal is the main one of the shop.
        return {"limit": limit, "used": used, "available": max(0, limit - used), "hub": hub}

    def station_limit(self):
        with self.connection() as conn:
            return self._station_limit(conn)

    @staticmethod
    def _station_row(row, open_ids, now):
        server = row["id"] == SERVER_STATION_ID
        linked = server or (row["token_hash"] is not None and row["revoked_at"] is None)
        seen = row["last_seen_at"]
        return {"id": row["id"], "name": row["name"], "is_server": server, "created_at": row["created_at"],
                "last_seen_at": seen, "revoked_at": row["revoked_at"], "hostname": row["hostname"],
                "app_version": row["app_version"], "last_address": row["last_address"], "linked": linked,
                "online": server or bool(linked and seen and now - seen <= STATION_ONLINE_SECONDS * 1000),
                "cash_open": row["id"] in open_ids}

    def _stations(self, conn, include_revoked=True, station_id=None):
        open_ids = {row[0] for row in conn.execute("SELECT station_id FROM cash_sessions WHERE closed_at IS NULL")}
        query, params = "SELECT * FROM stations", []
        if station_id is not None:
            query, params = query + " WHERE id=?", [station_id]
        now = _now_ms()
        rows = [self._station_row(row, open_ids, now)
                for row in conn.execute(query + " ORDER BY id != 'server',created_at,id", params)]
        return [row for row in rows if include_revoked or row["revoked_at"] is None]

    def stations(self, include_revoked=True):
        """Every cashier of the shop, the main one first. The token never leaves the database."""
        with self.connection() as conn:
            return self._stations(conn, include_revoked)

    def _config_event_quietly(self):
        try:
            self.ensure_config_event()
        except (TerminalError, sqlite3.Error):
            pass  # the worker tries again on its next pass

    def create_station_code(self, operator_id, name=""):
        """Six digits the manager reads on this screen and types on the new cashier. One at a time."""
        status = self.lease_status()
        if not status["can_open_cash"]:
            raise TerminalError(status["reason"], 403)
        name = name.strip()[:STATION_NAME_MAX] if isinstance(name, str) else ""
        with self.transaction() as conn:
            limit = self._station_limit(conn)
            if limit["limit"] <= 0:
                raise TerminalError(STATION_LIMIT_NONE if limit["hub"] else STATION_NOT_HUB, 409,
                                    code="station_limit")
            # A cashier that lost its link (promotion, recovery) links again with its own identity
            # and takes no new place: the code is refused only when nobody could use it. A removed
            # cashier gave its place away, so it is not a reason to hand out a code on a full plan.
            relinkable = conn.execute("SELECT 1 FROM stations WHERE id != ? AND token_hash IS NULL "
                                      "AND revoked_at IS NULL LIMIT 1", (SERVER_STATION_ID,)).fetchone()
            if limit["available"] <= 0 and not relinkable:
                raise TerminalError(STATION_LIMIT_FULL.format(n=limit["limit"]), 409, code="station_limit")
            code = f"{secrets.randbelow(10 ** 6):06d}"
            now = _now_ms()
            conn.execute("DELETE FROM station_enroll_codes")
            conn.execute("INSERT INTO station_enroll_codes(code_hash,created_at,expires_at,created_by,station_name) "
                         "VALUES (?,?,?,?,?)",
                         (self._enroll_hash(code), now, now + STATION_ENROLL_TTL_SECONDS * 1000, operator_id, name))
            self._audit(conn, operator_id, "station.code_created", None, name or None)  # never the code itself
            return {"code": code, "expires_at": now + STATION_ENROLL_TTL_SECONDS * 1000,
                    "expires_in": STATION_ENROLL_TTL_SECONDS}

    @staticmethod
    def _enroll_hash(code):
        return hashlib.sha256(("pdv2-enroll:" + code).encode()).hexdigest()

    def enroll_station(self, code, *, name="", hostname="", app_version="", station_id=None, address=""):
        """Link one more computer to this shop. The answer carries the only copy of its token."""
        digits = re.sub(r"\D", "", code) if isinstance(code, str) else ""
        if len(digits) != 6:
            raise TerminalError("Digite os 6 números do código.", 400)

        def text(value, maximum):
            return value.strip()[:maximum] if isinstance(value, str) else ""

        name, hostname, app_version, address = text(name, STATION_NAME_MAX), text(hostname, 120), \
            text(app_version, 40), text(address, 64)
        error = result = None
        # Refusals are decided inside the transaction and raised after it: a wrong code must be counted.
        with self.transaction() as conn:
            now = _now_ms()
            active = conn.execute("SELECT * FROM station_enroll_codes WHERE used_at IS NULL AND expires_at>? "
                                  "ORDER BY created_at DESC LIMIT 1", (now,)).fetchone()
            if not active:
                error = TerminalError(STATION_CODE_INVALID, 401, code="invalid_code")
            elif not hmac.compare_digest(active["code_hash"], self._enroll_hash(digits)):
                failures = active["failures"] + 1
                if failures >= STATION_ENROLL_MAX_FAILURES:
                    conn.execute("DELETE FROM station_enroll_codes WHERE code_hash=?", (active["code_hash"],))
                    self._audit(conn, None, "station.enroll_locked", None, address or None)
                else:
                    conn.execute("UPDATE station_enroll_codes SET failures=? WHERE code_hash=?",
                                 (failures, active["code_hash"]))
                error = TerminalError(STATION_CODE_INVALID, 401, code="invalid_code")
            else:
                existing = None
                if isinstance(station_id, str) and station_id and station_id != SERVER_STATION_ID:
                    existing = conn.execute("SELECT * FROM stations WHERE id=?", (station_id,)).fetchone()
                limit = self._station_limit(conn)
                # A known cashier that was not removed already holds its place. A new computer, or a
                # removed one coming back, needs a free place: otherwise it would push the newest
                # legitimate cashier out of the plan in the middle of a shift.
                takes_place = existing is None or existing["revoked_at"] is not None
                if takes_place and limit["used"] >= limit["limit"]:
                    # The code is not spent: it still serves once a place is freed.
                    message = STATION_LIMIT_FULL.format(n=limit["limit"]) if limit["limit"] else STATION_LIMIT_NONE
                    error = TerminalError(message, 409, code="station_limit")
                else:
                    token = secrets.token_urlsafe(32)
                    token_hash = hashlib.sha256(token.encode()).hexdigest()
                    if existing is not None:
                        # Same computer coming back: it keeps its open cashier and its history.
                        new_id = existing["id"]
                        final_name = name or active["station_name"] or existing["name"]
                        conn.execute("""UPDATE stations SET name=?,token_hash=?,revoked_at=NULL,last_seen_at=?,
                                        hostname=?,app_version=?,last_address=? WHERE id=?""",
                                     (final_name, token_hash, now, hostname, app_version, address, new_id))
                    else:
                        new_id = uuid.uuid4().hex
                        final_name = name or active["station_name"]
                        if not final_name:
                            taken = {row[0] for row in conn.execute("SELECT name FROM stations")}
                            number = 2
                            while f"Caixa {number}" in taken:
                                number += 1
                            final_name = f"Caixa {number}"
                        conn.execute("""INSERT INTO stations
                            (id,name,token_hash,created_at,last_seen_at,revoked_at,hostname,app_version,last_address)
                            VALUES (?,?,?,?,?,NULL,?,?,?)""",
                                     (new_id, final_name, token_hash, now, now, hostname, app_version, address))
                    conn.execute("UPDATE station_enroll_codes SET used_at=? WHERE code_hash=?",
                                 (now, active["code_hash"]))
                    self._audit(conn, active["created_by"], "station.enrolled", new_id, final_name)
                    result = {"station_id": new_id, "station_name": final_name, "token": token}
        if error is not None:
            raise error
        self._config_event_quietly()
        return result

    def authenticate_station(self, token):
        """Who is behind this token. Read only: called on every request of an additional cashier."""
        unknown = TerminalError("Este caixa não está vinculado ao caixa principal.", 401, code="station_unauthorized")
        if not isinstance(token, str) or not 20 <= len(token) <= 128:
            raise unknown
        digest = hashlib.sha256(token.encode("utf-8", "replace")).hexdigest()
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM stations WHERE token_hash=? AND id != ?",
                               (digest, SERVER_STATION_ID)).fetchone()
            if not row:
                raise unknown
            if row["revoked_at"] is not None:
                raise TerminalError("Este caixa foi removido do caixa principal.", 401, code="station_revoked")
            # The oldest links keep working when the plan shrinks; the newest ones wait.
            position = 1 + conn.execute(
                """SELECT COUNT(*) FROM stations WHERE id != ? AND revoked_at IS NULL
                   AND (created_at < ? OR (created_at = ? AND id < ?))""",
                (SERVER_STATION_ID, row["created_at"], row["created_at"], row["id"])).fetchone()[0]
            limit = self._station_limit(conn)
            if position > limit["limit"]:
                raise TerminalError("O plano da loja não inclui mais este caixa adicional. Fale com o responsável "
                                    "pela loja." if limit["hub"] else STATION_NOT_HUB_STATION, 403,
                                    code="station_limit")
            return {"id": row["id"], "name": row["name"]}

    def touch_station(self, station_id, *, address="", app_version="", hostname=""):
        with self.transaction() as conn:
            conn.execute("""UPDATE stations SET last_seen_at=?,
                            last_address=CASE WHEN ?='' THEN last_address ELSE ? END,
                            app_version=CASE WHEN ?='' THEN app_version ELSE ? END,
                            hostname=CASE WHEN ?='' THEN hostname ELSE ? END
                            WHERE id=? AND id != ?""",
                         (_now_ms(), *(str(value or "")[:120] for value in
                                       (address, address, app_version, app_version, hostname, hostname)),
                          station_id, SERVER_STATION_ID))

    def rename_station(self, station_id, name, operator_id=None):
        name = _str(name, "Nome do caixa", STATION_NAME_MAX)
        with self.transaction() as conn:
            if not conn.execute("UPDATE stations SET name=? WHERE id=?", (name, station_id)).rowcount:
                raise TerminalError("Caixa não encontrado", 404)
            self._audit(conn, operator_id, "station.renamed", station_id, name)
            result = self._stations(conn, station_id=station_id)[0]
        self._config_event_quietly()
        return result

    def revoke_station(self, station_id, operator_id=None):
        """"Remover caixa": the computer stops being accepted. Its open cashier, if any, stays open
        until a manager closes it from the main cashier."""
        if station_id == SERVER_STATION_ID:
            raise TerminalError("O caixa principal não pode ser removido.", 409)
        with self.transaction() as conn:
            row = conn.execute("SELECT name FROM stations WHERE id=?", (station_id,)).fetchone()
            if not row:
                raise TerminalError("Caixa não encontrado", 404)
            # The token hash stays: the removed computer is told it was removed, not that it is unknown.
            conn.execute("UPDATE stations SET revoked_at=COALESCE(revoked_at,?) WHERE id=?", (_now_ms(), station_id))
            self._audit(conn, operator_id, "station.revoked", station_id, row["name"])
            result = self._stations(conn, station_id=station_id)[0]
        self._config_event_quietly()
        return result

    # ---------- phase 3: an additional cashier takes over as the main one ----------

    @staticmethod
    def _valid_journal_item(item):
        event = item.get("event") if isinstance(item, dict) else None
        return (isinstance(event, dict) and type(item.get("seq")) is int and item["seq"] > 0
                and isinstance(event.get("event_id"), str) and EVENT_ID.fullmatch(event["event_id"])
                and event["event_id"] == item.get("event_id")
                and isinstance(event.get("type"), str) and bool(event["type"])
                and isinstance(event.get("aggregate_id"), str) and isinstance(event.get("data"), dict)
                and isinstance(event.get("identity"), dict))

    def apply_journal_events(self, events):
        """Replay what the old main cashier recorded after the last full copy (promotion).

        Each event enters the queue with its own event_id and seq and has its local effect
        applied (stock, fiado, store credit, cashier). Nothing new is created: no event, no
        print job. Calling it again with the same list changes nothing.
        """
        if not isinstance(events, list):
            raise TerminalError("Cópia de segurança inválida")
        result = {"applied": 0, "skipped": 0, "invalid": 0, "foreign": 0, "reseq": 0, "warnings": 0, "last_seq": 0}
        valid = []
        for item in events:
            if self._valid_journal_item(item):
                valid.append(item)
            else:
                result["invalid"] += 1
        valid.sort(key=lambda item: item["seq"])
        for start in range(0, max(len(valid), 1), 500):
            chunk = valid[start:start + 500]
            last_chunk = start + 500 >= len(valid)
            with self.transaction() as conn:
                identity = {key: self._meta(conn, key) for key in ("tenant_id", "shop_id", "terminal_id")}
                names = {"operators": {row["id"]: row["name"] for row in conn.execute("SELECT id,name FROM operators")},
                         "payments": {row["id"]: row["name"] for row in self._payment_config(conn)
                                      if row.get("kind") == "other"}}
                for item in chunk:
                    event = item["event"]
                    if {key: event["identity"].get(key) for key in identity} != identity:
                        result["foreign"] += 1
                        continue
                    if conn.execute("SELECT 1 FROM outbox WHERE id=?", (event["event_id"],)).fetchone():
                        result["skipped"] += 1
                        continue
                    seq = item["seq"]
                    if conn.execute("SELECT 1 FROM outbox WHERE seq=?", (seq,)).fetchone():
                        seq = conn.execute("SELECT COALESCE(MAX(seq),0)+1 FROM outbox").fetchone()[0]
                        result["reseq"] += 1
                    created_at = item.get("created_at") if type(item.get("created_at")) is int else _now_ms()
                    conn.execute("INSERT INTO outbox(id,event_type,aggregate_id,payload,created_at,acked_at,seq) "
                                 "VALUES (?,?,?,?,?,NULL,?)",
                                 (event["event_id"], event["type"], event["aggregate_id"], _json(event),
                                  created_at, seq))
                    # What happened at the counter is a fact: the event goes up even when its
                    # local effect cannot be rebuilt here (that only counts as a warning).
                    conn.execute("SAVEPOINT journal_effect")
                    try:
                        result["warnings"] += self._journal_effect(conn, event, names)
                    except (KeyError, TypeError, ValueError, AttributeError, sqlite3.IntegrityError):
                        conn.execute("ROLLBACK TO journal_effect")
                        result["warnings"] += 1
                    conn.execute("RELEASE journal_effect")
                    result["applied"] += 1
                    result["last_seq"] = max(result["last_seq"], seq)
                if last_chunk and events:
                    self._audit(conn, None, "promotion.journal_applied", identity["terminal_id"],
                                "; ".join(f"{key}={result[key]}" for key in
                                          ("applied", "skipped", "invalid", "foreign", "reseq", "warnings")))
        return result

    @staticmethod
    def _event_station(conn, event):
        """Station that produced an event; events of an older build belong to the main cashier."""
        station = event.get("station")
        if (not isinstance(station, dict) or not isinstance(station.get("id"), str)
                or not 0 < len(station["id"]) <= 64):
            return SERVER_STATION_ID
        name = station.get("name") if isinstance(station.get("name"), str) else ""
        conn.execute("INSERT OR IGNORE INTO stations(id,name,token_hash,created_at) VALUES (?,?,NULL,?)",
                     (station["id"], name.strip()[:STATION_NAME_MAX] or "Caixa", _now_ms()))
        return station["id"]

    @staticmethod
    def _credit_effect(conn, customer_id, payments, sign):
        """Fiado and store credit of a sale (sign=1) or of its cancellation (sign=-1)."""
        if not customer_id:
            return
        for payment in payments or []:
            amount = int(payment["amount_cents"])
            if payment.get("method") == "credit_account":
                conn.execute("UPDATE customers SET open_credit_cents=MAX(open_credit_cents+?,0) WHERE id=?",
                             (sign * amount, customer_id))
            elif payment.get("method") == "store_credit":
                conn.execute("UPDATE customers SET store_credit_cents=store_credit_cents-? WHERE id=?",
                             (sign * amount, customer_id))

    def _journal_effect(self, conn, event, names):
        """Local effect of one replayed event. Returns how many warnings it produced."""
        kind, aggregate_id, data = event["type"], event["aggregate_id"], event["data"]
        station_id = self._event_station(conn, event)
        if kind == "cash.opened":
            if conn.execute("SELECT 1 FROM cash_sessions WHERE id=?", (aggregate_id,)).fetchone():
                return 0
            opened_at = int(data["created_at"])
            values = [aggregate_id, str(data.get("operator_id") or ""), int(data.get("opening_cents") or 0),
                      opened_at, None, station_id]
            insert = """INSERT INTO cash_sessions
                        (id,operator_id,opening_cents,opened_at,closed_at,counted_cents,expected_cents,station_id)
                        VALUES (?,?,?,?,?,NULL,NULL,?)"""
            try:
                conn.execute(insert, values)
            except sqlite3.IntegrityError:
                # This station already has an open cashier: keep the record, as closed.
                values[4] = opened_at
                conn.execute(insert, values)
                return 1
        elif kind in ("cash.withdrawn", "cash.supplied"):
            conn.execute("INSERT OR IGNORE INTO cash_movements VALUES (?,?,?,?,?,?,?)",
                         (aggregate_id, str(data["cash_session_id"]), kind.split(".")[1], int(data["amount_cents"]),
                          str(data.get("reason") or ""), str(data.get("operator_id") or ""), int(data["created_at"])))
        elif kind == "cash.closed":
            conn.execute("UPDATE cash_sessions SET closed_at=?,counted_cents=?,expected_cents=? WHERE id=?",
                         (int(data["created_at"]), data.get("counted_cents"), data.get("expected_cents"),
                          aggregate_id))
            conn.execute("INSERT OR REPLACE INTO cash_closings(session_id,closed_by,supervisor_id) VALUES (?,?,?)",
                         (aggregate_id, data.get("operator_id"), data.get("supervisor_id")))
            conn.execute("UPDATE cash_pauses SET ended_at=? WHERE session_id=? AND ended_at IS NULL",
                         (int(data["created_at"]), aggregate_id))
        elif kind == "cash.paused":
            conn.execute("INSERT OR IGNORE INTO cash_pauses(id,session_id,operator_id,reason,started_at) "
                         "VALUES (?,?,?,?,?)", (aggregate_id, str(data["cash_session_id"]),
                                                str(data.get("operator_id") or ""), str(data.get("reason") or ""),
                                                int(data["created_at"])))
        elif kind == "cash.resumed":
            conn.execute("UPDATE cash_pauses SET ended_at=?,resumed_by=? WHERE id=? AND ended_at IS NULL",
                         (int(data["created_at"]), str(data.get("operator_id") or ""), aggregate_id))
        elif kind == "sale.completed":
            snapshot = {"id": aggregate_id, **data, "seller_name": names["operators"].get(data.get("seller_id"), ""),
                        "status": "completed", "station_id": station_id,
                        "station_name": self._station_name(conn, station_id)}
            payment_names = {payment["payment_method_id"]: names["payments"][payment["payment_method_id"]]
                             for payment in data.get("payments") or []
                             if isinstance(payment, dict) and payment.get("payment_method_id") in names["payments"]}
            if payment_names:
                snapshot["payment_names"] = payment_names
            snapshot = self._receipt_context(conn, snapshot)
            inserted = conn.execute(
                """INSERT OR IGNORE INTO sales (id,request_id,session_id,operator_id,customer_name,customer_id,
                       gross_cents,discount_cents,total_cents,change_cents,snapshot,status,created_at,voided_at,
                       void_reason,customer_document,station_id)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,'completed',?,NULL,NULL,?,?)""",
                (aggregate_id, str(data.get("request_id") or aggregate_id), str(data.get("cash_session_id") or ""),
                 str(data.get("operator_id") or ""), data.get("customer_name"), data.get("customer_id"),
                 int(data.get("gross_cents") or 0), int(data.get("discount_cents") or 0),
                 int(data.get("total_cents") or 0), int(data.get("change_cents") or 0), _json(snapshot),
                 int(data.get("created_at") or 0), data.get("customer_document"), station_id)).rowcount
            if not inserted:
                return 0  # the full copy already had this sale, with its effects
            for line in data.get("items") or []:
                if line.get("stock_controlled", True):
                    conn.execute("UPDATE products SET stock_milli=MAX(stock_milli-?,0) WHERE id=?",
                                 (int(line["quantity_milli"]), line["product_id"]))
            self._credit_effect(conn, data.get("customer_id"), data.get("payments"), 1)
        elif kind == "sale.voided":
            row = conn.execute("SELECT snapshot,status FROM sales WHERE id=?", (aggregate_id,)).fetchone()
            if not row or row["status"] != "completed":
                return 0
            snapshot = json.loads(row["snapshot"])
            for line in snapshot.get("items") or []:
                if line.get("stock_controlled", True):
                    conn.execute("UPDATE products SET stock_milli=stock_milli+? WHERE id=?",
                                 (int(line["quantity_milli"]), line["product_id"]))
            self._credit_effect(conn, snapshot.get("customer_id"), snapshot.get("payments"), -1)
            conn.execute("UPDATE sales SET status='voided',voided_at=?,void_reason=? WHERE id=?",
                         (int(data.get("created_at") or 0), str(data.get("reason") or ""), aggregate_id))
        elif kind == "sale.returned":
            items, total = data.get("items") or [], int(data["total_cents"])
            inserted = conn.execute(
                """INSERT OR IGNORE INTO sale_returns
                   (id,sale_id,session_id,operator_id,refund_method,total_cents,items,reason,created_at,station_id)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (aggregate_id, str(data["sale_id"]), data.get("cash_session_id") or None,
                 str(data.get("operator_id") or ""), str(data["refund_method"]), total, _json(items),
                 str(data.get("reason") or ""), int(data["created_at"]), station_id)).rowcount
            if not inserted:
                return 0
            sale = conn.execute("SELECT snapshot FROM sales WHERE id=?", (str(data["sale_id"]),)).fetchone()
            lines = {line["product_id"]: line for line in (json.loads(sale["snapshot"]).get("items") or [])} \
                if sale else {}
            for line in items:
                if lines.get(line["product_id"], {}).get("stock_controlled", True):
                    conn.execute("UPDATE products SET stock_milli=stock_milli+? WHERE id=?",
                                 (int(line["quantity_milli"]), line["product_id"]))
            customer_id = data.get("customer_id")
            if customer_id and data["refund_method"] == "store_credit":
                conn.execute("UPDATE customers SET store_credit_cents=store_credit_cents+? WHERE id=?",
                             (total, customer_id))
            elif customer_id and data["refund_method"] == "account":
                customer = conn.execute("SELECT open_credit_cents FROM customers WHERE id=?",
                                        (customer_id,)).fetchone()
                open_credit = customer["open_credit_cents"] if customer else 0
                conn.execute("UPDATE customers SET open_credit_cents=?,store_credit_cents=store_credit_cents+? "
                             "WHERE id=?", (max(0, open_credit - total), max(0, total - open_credit), customer_id))
        elif kind == "credit.received":
            amount = int(data["amount_cents"])
            inserted = conn.execute(
                """INSERT OR IGNORE INTO credit_receipts
                   (id,customer_id,session_id,method,amount_cents,operator_id,created_at,station_id)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (aggregate_id, str(data["customer_id"]), str(data.get("cash_session_id") or ""), str(data["method"]),
                 amount, str(data.get("operator_id") or ""), int(data["created_at"]), station_id)).rowcount
            if inserted:
                conn.execute("UPDATE customers SET open_credit_cents=MAX(open_credit_cents-?,0) WHERE id=?",
                             (amount, str(data["customer_id"])))
        elif kind == "stock.adjusted":
            conn.execute("UPDATE products SET stock_milli=MAX(stock_milli+?,0) WHERE id=?",
                         (int(data["delta_milli"]), str(data["product_id"])))
        return 0  # terminal.config and unknown types only go up

    def finish_promotion(self, station_id, station_name, mirror_key, operator_id=None):
        """This file, rebuilt from the copy on an additional cashier, becomes the main cashier.

        The old main cashier turns into the station "anterior-…" (its open cashier stays there for
        the manager to close) and the promoted station becomes "server". The other stations keep
        identity and open cashier, but must be linked again with a new code.
        """
        station_id = _str(station_id, "Caixa", 64)
        if station_id == SERVER_STATION_ID:
            raise TerminalError("Este computador já é o caixa principal de uma loja.", 409)
        station_name = station_name.strip()[:STATION_NAME_MAX] if isinstance(station_name, str) else ""
        with self.transaction() as conn:
            if self._restore_active(conn) or not self._meta(conn, "terminal_id"):
                raise TerminalError("A cópia do caixa principal não está completa. Não é possível assumir.", 409)
            now = _now_ms()
            old = "anterior-" + uuid.uuid4().hex[:8]
            for table in STATION_TABLES:
                # In this order: the two cashiers may be open and each station admits a single one.
                conn.execute(f"UPDATE {table} SET station_id=? WHERE station_id=?", (old, SERVER_STATION_ID))
                conn.execute(f"UPDATE {table} SET station_id=? WHERE station_id=?", (SERVER_STATION_ID, station_id))
            conn.execute("DELETE FROM print_settings WHERE station_id=?", (SERVER_STATION_ID,))
            conn.execute("UPDATE print_settings SET station_id=? WHERE station_id=?", (SERVER_STATION_ID, station_id))
            conn.execute("UPDATE print_jobs SET status='uncertain',error='Caixa principal anterior parou',updated_at=? "
                         "WHERE station_id=? AND status IN ('queued','sending')", (now, old))
            previous_open = conn.execute("SELECT 1 FROM cash_sessions WHERE closed_at IS NULL AND station_id=?",
                                         (old,)).fetchone() is not None
            conn.execute("INSERT INTO stations(id,name,token_hash,created_at,revoked_at) VALUES (?,?,NULL,?,?)",
                         (old, "Caixa principal anterior", now, now))
            conn.execute("DELETE FROM stations WHERE id=?", (station_id,))
            conn.execute("UPDATE stations SET token_hash=NULL WHERE id != ?", (SERVER_STATION_ID,))
            conn.execute("DELETE FROM login_attempts")
            conn.execute("DELETE FROM station_enroll_codes")
            if (isinstance(mirror_key, dict) and isinstance(mirror_key.get("key_id"), str) and mirror_key["key_id"]
                    and isinstance(mirror_key.get("key"), str) and len(mirror_key["key"]) == 44):
                self._set_meta(conn, "mirror_key", _json({"key_id": mirror_key["key_id"], "key": mirror_key["key"]}))
            try:
                generation = int(self._meta(conn, "server_generation") or 1) + 1
            except ValueError:
                generation = 2
            self._set_meta(conn, "promoted_at", str(now))
            self._set_meta(conn, "promotion_state", "awaiting_cloud")
            self._set_meta(conn, "promotion_previous_station", old)
            self._set_meta(conn, "server_generation", str(generation))
            # Each promotion presents a new identity to the cloud. The one of an earlier promotion
            # came in the copy and is the device_id of the computer that has just stopped: reusing it
            # would overwrite that computer's token instead of marking it as transferred.
            conn.execute("DELETE FROM meta WHERE key='promotion_device_id'")
            conn.execute("DELETE FROM meta WHERE key IN ('superseded_at','superseded_by')")
            # The cloud token stayed on the computer that stopped (the copy never carries it).
            for key in ("token", "last_heartbeat_at", "last_heartbeat_error", "last_sync_error"):
                conn.execute("DELETE FROM meta WHERE key=?", (key,))
            self._audit(conn, operator_id, "terminal.promoted", self._meta(conn, "terminal_id"),
                        station_name or station_id)
            return {"promoted": True, "previous_server_station": old, "previous_server_cash_open": previous_open,
                    "generation": generation}

    def promotion_status(self):
        with self.connection() as conn:
            state = self._meta(conn, "promotion_state")
            state = state if state in ("awaiting_cloud", "done") else "none"
            previous = self._meta(conn, "promotion_previous_station")
            return {"promoted": state != "none", "state": state,
                    "promoted_at": int(self._meta(conn, "promoted_at") or 0) or None,
                    "previous_server_cash_open": bool(previous) and conn.execute(
                        "SELECT 1 FROM cash_sessions WHERE closed_at IS NULL AND station_id=?",
                        (previous,)).fetchone() is not None,
                    "pending_events": conn.execute("SELECT COUNT(*) FROM outbox WHERE acked_at IS NULL").fetchone()[0]}

    def promotion_device_id(self):
        """Identity this computer presents to the cloud when it regularizes the promotion."""
        with self.connection() as conn:
            value = self._meta(conn, "promotion_device_id")
            awaiting = self._meta(conn, "promotion_state") == "awaiting_cloud"
            current = self._meta(conn, "device_id")
        # While the cloud is awaited, the device_id in the database is the one of the computer that
        # stopped: this computer never presents that same identity.
        if value and not (awaiting and value == current):
            return value
        with self.transaction() as conn:
            value = self._meta(conn, "promotion_device_id")
            if not value or (self._meta(conn, "promotion_state") == "awaiting_cloud"
                             and value == self._meta(conn, "device_id")):
                value = _uuid()
                self._set_meta(conn, "promotion_device_id", value)
            return value

    def reset_promotion_device_id(self):
        """Forget the identity presented to the cloud: the next attempt uses a new one.

        Needed when the cloud bound that identity to another terminal (a code typed by mistake):
        it would refuse the same device for the right terminal afterwards.
        """
        with self.transaction() as conn:
            if self._meta(conn, "promotion_state") == "awaiting_cloud":
                conn.execute("DELETE FROM meta WHERE key='promotion_device_id'")

    def adopt_pairing(self, response, cloud_url):
        """The cloud accepted this computer as the new home of the same terminal (after a promotion).

        Unlike set_pairing, nothing is cleared and no recovery starts: the data is already here.
        """
        from shared.licensing import LicenseError

        if not isinstance(response, dict):
            raise TerminalError("Resposta inesperada do servidor", 502)
        for key in ("token", "tenant_id", "shop_id", "terminal_id", "lease", "public_key"):
            if key not in response:
                raise TerminalError(f"Pareamento incompleto: {key}")
        device_id = self.promotion_device_id()
        with self.transaction() as conn:
            if self._meta(conn, "promotion_state") != "awaiting_cloud":
                raise TerminalError("Este caixa não está aguardando a regularização com a nuvem.", 409)
            if any(response[key] != self._meta(conn, key) for key in ("tenant_id", "shop_id", "terminal_id")):
                raise TerminalError("Este código é de outra loja ou de outro caixa. Gere o código no painel, em "
                                    "Terminais → Trocar de computador, no terminal desta loja.", 409,
                                    code="other_terminal")
            old_key = self._meta(conn, "public_key")
            if old_key and old_key != response["public_key"]:
                raise TerminalError("Chave pública mudou; suporte precisa verificar o servidor", 409)
            self._set_meta(conn, "public_key", response["public_key"])
            # From here on the license is bound to this computer, no longer to the one that stopped.
            self._set_meta(conn, "device_id", device_id)
            try:
                claims = self._accept_lease(conn, response["lease"], response["public_key"],
                                            response.get("server_time"))
            except (LicenseError, TypeError) as exc:
                raise TerminalError("Licença do pareamento inválida", 403) from exc
            self._store_lease(conn, response["lease"], claims)
            self._set_meta(conn, "token", _str(response["token"], "Token", 400))
            self._set_meta(conn, "cloud_url", cloud_url)
            for key, parent in (("tenant_name", "tenant"), ("shop_name", "shop"), ("terminal_name", "terminal")):
                value = response.get(parent, {}).get("name") if isinstance(response.get(parent), dict) else None
                if _optional_text(value, 160):
                    self._set_meta(conn, key, _optional_text(value, 160))
            if _optional_text(response.get("license_hint"), 16):
                self._set_meta(conn, "license_hint", _optional_text(response["license_hint"], 16))
            self._pin_release_key(conn, response)
            self._store_cloud_extras(conn, response, mirror_key=True)
            # `recovery: true` is ignored on purpose: restore_state stays as it is (done).
            self._set_meta(conn, "promotion_state", "done")
            self._audit(conn, None, "terminal.promotion_regularized", response["terminal_id"])
        return {"regularized": True}
