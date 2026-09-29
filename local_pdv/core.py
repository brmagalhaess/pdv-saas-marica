"""Núcleo transacional de um único caixa local.

Todos os valores monetários são inteiros em centavos. Quantidades são unidades
inteiras; produtos vendidos por peso exigem um modelo próprio antes do piloto.
"""

import hashlib
import json
import sqlite3
import time
import uuid
from contextlib import closing
from pathlib import Path


class PDVError(ValueError):
    """Operação inválida que pode ser exibida ao operador."""


def _now():
    return time.time_ns() // 1_000_000


def _id():
    return uuid.uuid4().hex


def _text(value, label):
    if not isinstance(value, str) or not value.strip() or len(value) > 160:
        raise PDVError(f"{label} inválido")
    return value.strip()


def _amount(value, label, allow_zero=True):
    if isinstance(value, bool) or not isinstance(value, int) or value < 0 or (not allow_zero and value == 0):
        raise PDVError(f"{label} deve ser um inteiro positivo em centavos")
    return value


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class LocalPDV:
    """Estado de um terminal em um arquivo SQLite, sem acesso à rede.

    Identidades de empresa, loja e terminal são gravadas no primeiro uso. Não há
    autenticação de dispositivo neste protótipo; integrar ao SaaS requer isso.
    """

    def __init__(self, path, tenant_id=None, shop_id=None, terminal_id=None, fault_hook=None):
        self.path = Path(path)
        self.fault_hook = fault_hook
        exists = self.path.exists()
        if not exists and not all((tenant_id, shop_id, terminal_id)):
            raise PDVError("Informe empresa, loja e terminal para criar o banco local")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS products(
                    id TEXT PRIMARY KEY, barcode TEXT UNIQUE, name TEXT NOT NULL,
                    price_cents INTEGER NOT NULL CHECK(price_cents >= 0),
                    stock_qty INTEGER NOT NULL CHECK(stock_qty >= 0),
                    active INTEGER NOT NULL CHECK(active IN (0,1)), updated_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS cash_sessions(
                    id TEXT PRIMARY KEY, opening_cents INTEGER NOT NULL CHECK(opening_cents >= 0),
                    opened_at INTEGER NOT NULL, closed_at INTEGER,
                    counted_cents INTEGER, expected_cents INTEGER
                );
                CREATE UNIQUE INDEX IF NOT EXISTS one_open_cash ON cash_sessions((1)) WHERE closed_at IS NULL;
                CREATE TABLE IF NOT EXISTS sales(
                    id TEXT PRIMARY KEY, request_id TEXT NOT NULL UNIQUE,
                    request_hash TEXT NOT NULL, cash_session_id TEXT NOT NULL REFERENCES cash_sessions(id),
                    total_cents INTEGER NOT NULL, change_cents INTEGER NOT NULL,
                    created_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sale_items(
                    sale_id TEXT NOT NULL REFERENCES sales(id), product_id TEXT NOT NULL REFERENCES products(id),
                    name TEXT NOT NULL, quantity INTEGER NOT NULL, unit_price_cents INTEGER NOT NULL,
                    line_total_cents INTEGER NOT NULL, PRIMARY KEY(sale_id, product_id)
                );
                CREATE TABLE IF NOT EXISTS sale_payments(
                    sale_id TEXT NOT NULL REFERENCES sales(id), sequence INTEGER NOT NULL,
                    method TEXT NOT NULL CHECK(method IN ('cash','pix','card')),
                    amount_cents INTEGER NOT NULL, PRIMARY KEY(sale_id, sequence)
                );
                CREATE TABLE IF NOT EXISTS stock_adjustments(
                    id TEXT PRIMARY KEY, request_id TEXT NOT NULL UNIQUE,
                    product_id TEXT NOT NULL REFERENCES products(id), delta INTEGER NOT NULL,
                    reason TEXT NOT NULL, created_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS outbox(
                    id TEXT PRIMARY KEY, event_type TEXT NOT NULL, aggregate_id TEXT NOT NULL,
                    payload TEXT NOT NULL, created_at INTEGER NOT NULL, acked_at INTEGER
                );
                CREATE INDEX IF NOT EXISTS outbox_pending ON outbox(acked_at, created_at);
            """)
            saved = dict(conn.execute("SELECT key,value FROM meta").fetchall())
            if not saved:
                values = {"tenant_id": _text(tenant_id, "Empresa"),
                          "shop_id": _text(shop_id, "Loja"),
                          "terminal_id": _text(terminal_id, "Terminal")}
                conn.executemany("INSERT INTO meta(key,value) VALUES (?,?)", values.items())
            else:
                for key, supplied in (("tenant_id", tenant_id), ("shop_id", shop_id), ("terminal_id", terminal_id)):
                    if key not in saved or (supplied is not None and supplied != saved[key]):
                        raise PDVError("Identidade do banco local não corresponde ao terminal")
            conn.commit()

    def _connect(self):
        conn = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=10000")
        conn.execute("PRAGMA synchronous=FULL")
        return conn

    def _tx(self, operation):
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            result = operation(conn)
            conn.commit()
            return result
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _identity(self, conn):
        return dict(conn.execute("SELECT key,value FROM meta").fetchall())

    def _emit(self, conn, event_type, aggregate_id, data):
        event_id = _id()
        payload = {"event_id": event_id, "type": event_type, "aggregate_id": aggregate_id,
                   "identity": self._identity(conn), "data": data}
        conn.execute("INSERT INTO outbox VALUES (?,?,?,?,?,NULL)",
                     (event_id, event_type, aggregate_id, _canonical(payload), _now()))
        return event_id

    def upsert_product(self, product_id, name, price_cents, initial_stock=0, barcode=None, active=True):
        """Carrega catálogo local; em produto existente preserva o estoque local."""
        product_id = _text(product_id, "Código do produto")
        name = _text(name, "Nome do produto")
        price_cents = _amount(price_cents, "Preço")
        initial_stock = _amount(initial_stock, "Estoque inicial")
        barcode = _text(barcode, "Código de barras") if barcode is not None else None
        def work(conn):
            conn.execute("""INSERT INTO products VALUES (?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET barcode=excluded.barcode, name=excluded.name,
                price_cents=excluded.price_cents, active=excluded.active, updated_at=excluded.updated_at""",
                (product_id, barcode, name, price_cents, initial_stock, int(bool(active)), _now()))
            return self._product(conn, product_id)
        try:
            return self._tx(work)
        except sqlite3.IntegrityError as exc:
            raise PDVError("Código de barras já usado por outro produto") from exc

    def _product(self, conn, product_id):
        row = conn.execute("SELECT * FROM products WHERE id=?", (product_id,)).fetchone()
        if not row:
            raise PDVError(f"Produto {product_id} não encontrado")
        return dict(row)

    def products(self):
        with closing(self._connect()) as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM products ORDER BY name")]

    def lookup_product(self, code):
        """Busca exata por código interno ou código de barras do leitor."""
        code = _text(code, "Código do produto")
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT * FROM products WHERE id=? OR barcode=? LIMIT 1", (code, code)).fetchone()
            if not row:
                raise PDVError(f"Produto {code} não encontrado")
            return dict(row)

    def adjust_stock(self, product_id, delta, reason, request_id):
        product_id = _text(product_id, "Código do produto")
        reason = _text(reason, "Motivo")
        request_id = _text(request_id, "Identificador da operação")
        if isinstance(delta, bool) or not isinstance(delta, int) or delta == 0:
            raise PDVError("Ajuste deve ser inteiro e diferente de zero")
        def work(conn):
            old = conn.execute("SELECT * FROM stock_adjustments WHERE request_id=?", (request_id,)).fetchone()
            if old:
                if (old["product_id"], old["delta"], old["reason"]) != (product_id, delta, reason):
                    raise PDVError("Identificador já usado para outro ajuste")
                return dict(old)
            product = self._product(conn, product_id)
            if product["stock_qty"] + delta < 0:
                raise PDVError("Estoque insuficiente para o ajuste")
            adjustment_id = _id()
            timestamp = _now()
            conn.execute("UPDATE products SET stock_qty=stock_qty+?,updated_at=? WHERE id=?", (delta, timestamp, product_id))
            conn.execute("INSERT INTO stock_adjustments VALUES (?,?,?,?,?,?)",
                         (adjustment_id, request_id, product_id, delta, reason, timestamp))
            self._emit(conn, "stock.adjusted", adjustment_id,
                       {"product_id": product_id, "delta": delta, "reason": reason, "created_at": timestamp})
            return dict(conn.execute("SELECT * FROM stock_adjustments WHERE id=?", (adjustment_id,)).fetchone())
        return self._tx(work)

    def open_cash(self, opening_cents):
        opening_cents = _amount(opening_cents, "Saldo de abertura")
        def work(conn):
            if conn.execute("SELECT 1 FROM cash_sessions WHERE closed_at IS NULL").fetchone():
                raise PDVError("Já existe um caixa aberto")
            session_id, timestamp = _id(), _now()
            conn.execute("INSERT INTO cash_sessions VALUES (?,?,?,NULL,NULL,NULL)",
                         (session_id, opening_cents, timestamp))
            self._emit(conn, "cash.opened", session_id,
                       {"opening_cents": opening_cents, "opened_at": timestamp})
            return dict(conn.execute("SELECT * FROM cash_sessions WHERE id=?", (session_id,)).fetchone())
        return self._tx(work)

    def _open_cash(self, conn):
        row = conn.execute("SELECT * FROM cash_sessions WHERE closed_at IS NULL").fetchone()
        if not row:
            raise PDVError("Abra o caixa antes de vender")
        return row

    def complete_sale(self, request_id, items, payments):
        """Grava venda, baixa de estoque e evento na mesma transação.

        request_id é a chave de repetição: reenviar os mesmos dados retorna a
        venda original; dados diferentes com a mesma chave são rejeitados.
        """
        request_id = _text(request_id, "Identificador da venda")
        if not isinstance(items, list) or not items:
            raise PDVError("Informe ao menos um item")
        if not isinstance(payments, list) or not payments:
            raise PDVError("Informe ao menos um pagamento")
        normalized_items = []
        seen = set()
        for item in items:
            if not isinstance(item, dict):
                raise PDVError("Item inválido")
            product_id = _text(item.get("product_id"), "Produto")
            quantity = _amount(item.get("quantity"), "Quantidade", allow_zero=False)
            if product_id in seen:
                raise PDVError("Produto repetido; some as quantidades")
            seen.add(product_id)
            normalized_items.append({"product_id": product_id, "quantity": quantity})
        normalized_payments = []
        for payment in payments:
            if not isinstance(payment, dict) or payment.get("method") not in ("cash", "pix", "card"):
                raise PDVError("Forma de pagamento inválida")
            amount = _amount(payment.get("amount_cents"), "Pagamento", allow_zero=False)
            normalized_payments.append({"method": payment["method"], "amount_cents": amount})
        request_hash = hashlib.sha256(_canonical({"items": normalized_items, "payments": normalized_payments}).encode()).hexdigest()

        def work(conn):
            old = conn.execute("SELECT id,request_hash FROM sales WHERE request_id=?", (request_id,)).fetchone()
            if old:
                if old["request_hash"] != request_hash:
                    raise PDVError("Identificador já usado para outra venda")
                return self._sale(conn, old["id"])
            cash = self._open_cash(conn)
            lines = []
            total = 0
            for item in normalized_items:
                product = self._product(conn, item["product_id"])
                if not product["active"]:
                    raise PDVError(f"Produto {product['id']} inativo")
                if product["stock_qty"] < item["quantity"]:
                    raise PDVError(f"Estoque insuficiente para {product['name']}")
                line_total = product["price_cents"] * item["quantity"]
                total += line_total
                lines.append({"product_id": product["id"], "name": product["name"],
                              "quantity": item["quantity"], "unit_price_cents": product["price_cents"],
                              "line_total_cents": line_total})
            paid = sum(p["amount_cents"] for p in normalized_payments)
            cash_paid = sum(p["amount_cents"] for p in normalized_payments if p["method"] == "cash")
            change = paid - total
            if total == 0 or change < 0 or change > cash_paid:
                raise PDVError("Pagamentos não fecham o total; troco exige dinheiro suficiente")
            sale_id, timestamp = _id(), _now()
            for line in lines:
                conn.execute("UPDATE products SET stock_qty=stock_qty-?,updated_at=? WHERE id=?",
                             (line["quantity"], timestamp, line["product_id"]))
            if self.fault_hook:
                self.fault_hook("after_stock_update")
            conn.execute("INSERT INTO sales VALUES (?,?,?,?,?,?,?)",
                         (sale_id, request_id, request_hash, cash["id"], total, change, timestamp))
            conn.executemany("INSERT INTO sale_items VALUES (?,?,?,?,?,?)",
                             [(sale_id, x["product_id"], x["name"], x["quantity"], x["unit_price_cents"], x["line_total_cents"]) for x in lines])
            conn.executemany("INSERT INTO sale_payments VALUES (?,?,?,?)",
                             [(sale_id, i, x["method"], x["amount_cents"]) for i, x in enumerate(normalized_payments)])
            self._emit(conn, "sale.completed", sale_id,
                       {"request_id": request_id, "cash_session_id": cash["id"], "total_cents": total,
                        "change_cents": change, "items": lines, "payments": normalized_payments,
                        "created_at": timestamp})
            return self._sale(conn, sale_id)
        return self._tx(work)

    def _sale(self, conn, sale_id):
        sale = dict(conn.execute("SELECT id,request_id,cash_session_id,total_cents,change_cents,created_at FROM sales WHERE id=?",
                                 (sale_id,)).fetchone())
        sale["items"] = [dict(x) for x in conn.execute("SELECT product_id,name,quantity,unit_price_cents,line_total_cents FROM sale_items WHERE sale_id=? ORDER BY rowid", (sale_id,))]
        sale["payments"] = [dict(x) for x in conn.execute("SELECT method,amount_cents FROM sale_payments WHERE sale_id=? ORDER BY sequence", (sale_id,))]
        return sale

    def close_cash(self, counted_cents):
        counted_cents = _amount(counted_cents, "Valor contado")
        def work(conn):
            cash = self._open_cash(conn)
            row = conn.execute("""SELECT COALESCE(SUM(p.amount_cents),0) AS cash_paid FROM sale_payments p
                JOIN sales s ON s.id=p.sale_id WHERE s.cash_session_id=? AND p.method='cash'""", (cash["id"],)).fetchone()
            changes = conn.execute("SELECT COALESCE(SUM(change_cents),0) FROM sales WHERE cash_session_id=?",
                                   (cash["id"],)).fetchone()[0]
            expected = cash["opening_cents"] + row["cash_paid"] - changes
            timestamp = _now()
            conn.execute("UPDATE cash_sessions SET closed_at=?,counted_cents=?,expected_cents=? WHERE id=?",
                         (timestamp, counted_cents, expected, cash["id"]))
            self._emit(conn, "cash.closed", cash["id"],
                       {"closed_at": timestamp, "counted_cents": counted_cents,
                        "expected_cents": expected, "difference_cents": counted_cents - expected})
            result = dict(conn.execute("SELECT * FROM cash_sessions WHERE id=?", (cash["id"],)).fetchone())
            result["difference_cents"] = counted_cents - expected
            return result
        return self._tx(work)

    def status(self):
        with closing(self._connect()) as conn:
            cash = conn.execute("SELECT * FROM cash_sessions WHERE closed_at IS NULL").fetchone()
            return {"identity": self._identity(conn), "open_cash": dict(cash) if cash else None,
                    "sales_count": conn.execute("SELECT COUNT(*) FROM sales").fetchone()[0],
                    "pending_events": conn.execute("SELECT COUNT(*) FROM outbox WHERE acked_at IS NULL").fetchone()[0],
                    "database_integrity": conn.execute("PRAGMA integrity_check").fetchone()[0]}

    def pending_events(self, limit=100):
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise PDVError("Limite inválido")
        with closing(self._connect()) as conn:
            return [json.loads(r[0]) for r in conn.execute("SELECT payload FROM outbox WHERE acked_at IS NULL ORDER BY created_at,rowid LIMIT ?", (limit,))]

    def acknowledge_event(self, event_id):
        """Marque recebido somente após confirmação persistida no servidor.

        O servidor futuro deverá deduplicar por event_id e aplicar o evento uma
        única vez; a ausência de resposta mantém o evento pendente para reenvio.
        """
        event_id = _text(event_id, "Identificador do evento")
        def work(conn):
            row = conn.execute("SELECT acked_at FROM outbox WHERE id=?", (event_id,)).fetchone()
            if not row:
                raise PDVError("Evento não encontrado")
            if row["acked_at"] is None:
                conn.execute("UPDATE outbox SET acked_at=? WHERE id=?", (_now(), event_id))
            return True
        return self._tx(work)
