"""NFC-e no caixa: estado local por venda, regra de impressão e contingência off-line.

Fluxo (docs/NFCE.md):
1. ``Store.sale`` grava a venda; com a NFC-e ligada o comprovante NÃO é impresso na hora.
2. A tela chama ``POST /api/sales/<id>/nfce``: o caixa envia a fila e pede a nota à nuvem
   (até ~8 s). Autorizada (ou contingência feita na nuvem) -> DANFE com chave e QR Code.
3. Sem nuvem: com a contingência ligada, o caixa monta a NFC-e tpEmis 9 com ``shared.nfce``
   (número da faixa reservada, QR Code v2 com CSC), imprime o DANFE de contingência e guarda o
   XML sem assinatura; a nuvem assina com o A1 e transmite quando o caixa voltar a falar com ela.
   O certificado nunca sai da nuvem.
4. A regra de impressão vem da forma de pagamento (sempre / perguntar / não imprimir).

Tabelas: ``nfce_docs`` (estado por venda) e ``nfce_reservation`` (faixa + CSC; fora de ``meta``).
"""
import json
import time
from datetime import datetime, timezone

from shared.fiscal import compute_document
from shared.nfce import NfceError, build_nfce, qrcode
from shared.nfce.payments import print_decision
from shared.nfce.webservices import key_url
from shared.nfce.xml import tostring

SCHEMA = """
CREATE TABLE IF NOT EXISTS nfce_docs(
    sale_id TEXT PRIMARY KEY, status TEXT NOT NULL, data TEXT NOT NULL DEFAULT '{}',
    xml TEXT, uploaded INTEGER NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 0,
    error TEXT NOT NULL DEFAULT '', created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS nfce_docs_status ON nfce_docs(status,uploaded);
CREATE TABLE IF NOT EXISTS nfce_reservation(id INTEGER PRIMARY KEY CHECK(id=1), data TEXT NOT NULL);
"""
# local states: pending (asked, no answer), authorized, contingency, rejected, cancelled, offline (no NFC-e yet)
OVERDUE_MS = 20 * 60 * 60 * 1000
RESERVATION_LOW = 3


def _now_ms():
    return int(time.time() * 1000)


def migrate(conn):
    conn.executescript(SCHEMA)


def config(conn, meta):
    raw = meta(conn, "nfce_config")
    try:
        value = json.loads(raw) if raw else {}
    except ValueError:
        value = {}
    return value if isinstance(value, dict) else {}


def store_config(conn, set_meta, value):
    if isinstance(value, dict):
        allowed = {key: value.get(key) for key in ("enabled", "environment", "mode", "contingency", "wait_ms",
                                                    "cancel_minutes", "print_rules")}
        rules = allowed.get("print_rules")
        allowed["print_rules"] = {str(k)[:64]: v for k, v in (rules or {}).items()
                                  if v in ("always", "ask", "never")} if isinstance(rules, dict) else {}
        set_meta(conn, "nfce_config", json.dumps(allowed, sort_keys=True))


def enabled(cfg):
    return bool(cfg.get("enabled")) and cfg.get("environment") in ("homologation", "production")


def decision(cfg, payments):
    return print_decision(payments, cfg.get("print_rules") or {})


def row(conn, sale_id):
    found = conn.execute("SELECT * FROM nfce_docs WHERE sale_id=?", (sale_id,)).fetchone()
    if not found:
        return None
    result = dict(found)
    result["data"] = json.loads(result["data"] or "{}")
    return result


def save(conn, sale_id, status, data=None, xml=None, error="", uploaded=None):
    now = _now_ms()
    existing = row(conn, sale_id)
    merged = dict(existing["data"]) if existing else {}
    merged.update(data or {})
    conn.execute("""INSERT INTO nfce_docs(sale_id,status,data,xml,uploaded,attempts,error,created_at,updated_at)
                    VALUES (?,?,?,?,?,0,?,?,?)
                    ON CONFLICT(sale_id) DO UPDATE SET status=excluded.status,data=excluded.data,
                      xml=COALESCE(excluded.xml,nfce_docs.xml),error=excluded.error,updated_at=excluded.updated_at,
                      uploaded=CASE WHEN ? IS NULL THEN nfce_docs.uploaded ELSE ? END,
                      attempts=nfce_docs.attempts+1""",
                 (sale_id, status, json.dumps(merged, sort_keys=True), xml, 1 if uploaded else 0, error[:500], now, now,
                  uploaded, 1 if uploaded else 0))


def fiscal_for_receipt(conn, sale_id):
    """sale["fiscal"] for terminal/printers.receipt_layout (empty when there is no NFC-e)."""
    found = row(conn, sale_id)
    if not found:
        return None
    data = dict(found["data"])
    data["status"] = found["status"]
    if found["status"] not in ("authorized", "contingency", "cancelled"):
        data["state"] = "none"
    elif found["status"] == "cancelled":
        data["state"] = "none"
    return data


def from_cloud(answer):
    """Normalize the cloud answer (core.nfce.service.danfe_data)."""
    fiscal = (answer or {}).get("fiscal") or {}
    status = fiscal.get("status") or (answer or {}).get("status") or "pending"
    keep = ("state", "access_key", "protocol", "number", "series", "issued_at", "authorized_at", "url_consulta",
            "qr_url", "environment", "emission_type", "error", "taxes_cents", "taxes_federal_cents",
            "taxes_state_cents", "taxes_municipal_cents", "taxes_source", "infcpl", "document_id")
    return status, {key: fiscal.get(key) for key in keep if fiscal.get(key) is not None}


# ------------------------------------------------------------------ reservation (contingency)

def reservation(conn):
    found = conn.execute("SELECT data FROM nfce_reservation WHERE id=1").fetchone()
    if not found:
        return None
    try:
        return json.loads(found["data"])
    except ValueError:
        return None


def store_reservation(conn, value):
    if not isinstance(value, dict) or not isinstance(value.get("numbers"), list):
        return
    clean = {key: value.get(key) for key in ("id", "environment", "series", "numbers", "csc_id", "csc", "qr_version",
                                              "issuer", "tech", "approx_fallback", "expires_at")}
    used = set((reservation(conn) or {}).get("used") or [])
    clean["numbers"] = [n for n in clean["numbers"] if isinstance(n, int) and n not in used]
    clean["used"] = sorted(used)
    conn.execute("INSERT INTO nfce_reservation(id,data) VALUES (1,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data",
                 (json.dumps(clean, sort_keys=True),))


def needs_reservation(conn, cfg):
    if not (enabled(cfg) and cfg.get("contingency")):
        return False
    current = reservation(conn)
    return (not current or current.get("environment") != cfg.get("environment")
            or len(current.get("numbers") or []) < RESERVATION_LOW)


def _product(conn, product_id):
    """(resolved fiscal fields from the cloud, barcode) of a catalog product."""
    found = conn.execute("SELECT fiscal,barcode FROM products WHERE id=?", (product_id,)).fetchone()
    try:
        data = json.loads(found["fiscal"] or "{}") if found else {}
    except ValueError:
        data = {}
    return data.get("fields") or {}, (found["barcode"] if found else "") or ""


def build_contingency(conn, sale, cfg, now=None):
    """Offline NFC-e of a sale (tpEmis 9, QR v2 with CSC). Returns (data, xml) or raises NfceError."""
    current = reservation(conn)
    if not cfg.get("contingency") or not current or not current.get("numbers"):
        raise NfceError("Contingência indisponível neste caixa (sem faixa de números reservada)")
    if current.get("environment") != cfg.get("environment"):
        raise NfceError("Faixa reservada é de outro ambiente fiscal")
    issuer = current.get("issuer") or {}
    items = []
    for line in sale["items"]:
        fields, barcode = _product(conn, line["product_id"])
        items.append({"product_id": line["product_id"], "code": line.get("sku") or line["product_id"],
                      "description": line["name"], "barcode": barcode, "unit": line["unit"],
                      "quantity_milli": line["quantity_milli"], "unit_price_cents": line["unit_price_cents"],
                      "gross_cents": line.get("gross_cents"),
                      "discount_cents": (line.get("promo_discount_cents") or 0) + (line.get("discount_cents") or 0),
                      "fiscal": fields})
    doc = compute_document({"crt": issuer.get("crt") or 1, "uf": issuer.get("address_state") or ""}, items, model=65,
                           approx_fallback=current.get("approx_fallback"))
    if doc["totals"]["total_cents"] != sale["total_cents"]:
        raise NfceError("Total da nota não confere com a venda")
    moment = now or datetime.now(timezone.utc)
    number = current["numbers"][0]
    customer = {"document": sale.get("customer_document"), "name": sale.get("customer_name")} \
        if sale.get("customer_document") else None
    draft = build_nfce(issuer, doc, series=current["series"], number=number, issued_at=moment,
                       environment=current["environment"], emission_type=9,
                       contingency={"at": moment, "reason": None}, customer=customer,
                       payments=sale["payments"], change_cents=sale.get("change_cents") or 0,
                       payment_names=sale.get("payment_names") or {}, tech=current.get("tech") or {},
                       app_version="PDV2 caixa")
    uf = issuer.get("address_state")
    qr = qrcode.build_v2(draft.key, draft.environment_code, current["csc_id"], current["csc"], uf=uf,
                         environment=current["environment"], offline=True, issued_at=draft.issued_at,
                         total_cents=draft.total_cents, digest_value=draft.digest_value)
    url = key_url(uf, current["environment"])
    draft.supplement(qr, url)
    current["numbers"] = current["numbers"][1:]
    current["used"] = sorted(set(current.get("used") or []) | {number})
    conn.execute("UPDATE nfce_reservation SET data=? WHERE id=1", (json.dumps(current, sort_keys=True),))
    totals = doc["totals"]
    data = {"state": "contingency", "access_key": draft.key, "number": number, "series": current["series"],
            "issued_at": int(moment.timestamp() * 1000), "url_consulta": url, "qr_url": qr,
            "environment": current["environment"], "emission_type": 9, "taxes_cents": totals["approx_tax_cents"] or None,
            "infcpl": draft.infnfe.findtext("infAdic/infCpl") or None, "origin": "terminal"}
    return {k: v for k, v in data.items() if v is not None}, tostring(draft.nfe)


def pending_uploads(conn, limit=10):
    return [dict(r) for r in conn.execute(
        "SELECT sale_id,xml FROM nfce_docs WHERE status='contingency' AND uploaded=0 AND xml IS NOT NULL "
        "ORDER BY created_at LIMIT ?", (limit,))]


def to_retry(conn, limit=5):
    """Sales whose NFC-e was asked but never answered (offline without contingency)."""
    return [r["sale_id"] for r in conn.execute(
        "SELECT sale_id FROM nfce_docs WHERE status IN ('pending','offline') ORDER BY created_at LIMIT ?", (limit,))]


def to_refresh(conn, limit=5):
    """Contingency notes already handed to the cloud: ask now and then whether they were authorized."""
    return [r["sale_id"] for r in conn.execute(
        "SELECT sale_id FROM nfce_docs WHERE status='contingency' AND uploaded=1 ORDER BY updated_at LIMIT ?", (limit,))]


def summary(conn):
    now = _now_ms()
    rows = conn.execute("SELECT status,uploaded,created_at FROM nfce_docs WHERE status IN "
                        "('contingency','pending','offline','rejected')").fetchall()
    waiting = [r for r in rows if r["status"] != "rejected"]
    return {"waiting": len(waiting), "overdue": sum(1 for r in waiting if now - r["created_at"] > OVERDUE_MS),
            "rejected": sum(1 for r in rows if r["status"] == "rejected"),
            "contingency": sum(1 for r in rows if r["status"] == "contingency")}
