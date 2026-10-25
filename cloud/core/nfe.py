"""Parse an inbound NF-e (modelo 55) XML into purchase data.

Only what a stock entry needs is read. The document is not validated against
the SEFAZ schema or signature: the user is importing a file they received.
DOCTYPE/ENTITY declarations are refused before parsing, which rules out
entity-expansion and external-entity attacks without extra dependencies.
"""
import re
import xml.etree.ElementTree as ET
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from .api_common import APIError

NS = "{http://www.portalfiscal.inf.br/nfe}"
MAX_BYTES = 3 * 1024 * 1024
UNITS = {"KG": "KG", "KGS": "KG", "QUILO": "KG", "L": "L", "LT": "L", "LTS": "L", "LITRO": "L"}


def _find(node, path):
    return node.find("/".join(NS + part for part in path.split("/"))) if node is not None else None


def _text(node, path, default=""):
    found = _find(node, path)
    return (found.text or "").strip() if found is not None and found.text else default


def _decimal(node, path, default="0"):
    raw = _text(node, path, default) or default
    try:
        return Decimal(raw)
    except InvalidOperation:
        raise APIError(f"Valor numérico inválido na NF-e: {path}")


def _cents(value):
    return int((value * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def valid_gtin(code):
    if not code or not code.isdigit() or len(code) not in (8, 12, 13, 14):
        return False
    digits = [int(c) for c in code]
    check = digits.pop()
    total = sum(d * (3 if i % 2 == 0 else 1) for i, d in enumerate(reversed(digits)))
    return (10 - total % 10) % 10 == check


def parse(raw):
    if isinstance(raw, str):
        raw = raw.encode("utf-8")
    if not raw or len(raw) > MAX_BYTES:
        raise APIError("Arquivo XML vazio ou maior que 3 MB")
    head = raw[:4096].upper()
    if b"<!DOCTYPE" in head or b"<!ENTITY" in raw.upper():
        raise APIError("XML com declarações DOCTYPE/ENTITY não é aceito")
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        raise APIError("Arquivo não é um XML válido")
    nfe = root if root.tag == NS + "NFe" else _find(root, "NFe")
    inf = _find(nfe, "infNFe")
    if inf is None:
        raise APIError("O arquivo não é uma NF-e (infNFe não encontrado)")
    if _text(inf, "ide/mod") not in ("55", ""):
        raise APIError("Somente NF-e modelo 55 pode ser importada como compra")
    key = re.sub(r"\D", "", inf.get("Id", "")) or _text(_find(root, "protNFe"), "infProt/chNFe")
    if len(key) != 44:
        raise APIError("Chave de acesso da NF-e inválida")
    protocol = _text(_find(root, "protNFe"), "infProt/nProt")
    emit = _find(inf, "emit")
    issued_raw = _text(inf, "ide/dhEmi") or _text(inf, "ide/dEmi")
    try:
        issued = datetime.fromisoformat(issued_raw).date() if "T" in issued_raw else date.fromisoformat(issued_raw)
    except ValueError:
        issued = None
    items = []
    for det in inf.findall(NS + "det"):
        prod = _find(det, "prod")
        imposto = _find(det, "imposto")
        quantity = _decimal(prod, "qCom")
        if quantity <= 0:
            raise APIError(f"Item {det.get('nItem')}: quantidade inválida")
        gross = _decimal(prod, "vProd")
        extras = (_decimal(prod, "vFrete") + _decimal(prod, "vSeg") + _decimal(prod, "vOutro")
                  - _decimal(prod, "vDesc"))
        # Taxes added on top of the product value become part of purchase cost.
        ipi = _decimal(imposto, "IPI/IPITrib/vIPI")
        st = Decimal("0")
        icms = _find(imposto, "ICMS")
        if icms is not None and len(icms):
            st = _decimal(icms[0], "vICMSST")
        total = gross + extras + ipi + st
        ean = _text(prod, "cEAN")
        ean_trib = _text(prod, "cEANTrib")
        items.append({
            "n_item": int(det.get("nItem") or len(items) + 1),
            "code": _text(prod, "cProd")[:60],
            "ean": ean if valid_gtin(ean) else "",
            "ean_trib": ean_trib if valid_gtin(ean_trib) else "",
            "name": _text(prod, "xProd")[:180],
            "ncm": re.sub(r"\D", "", _text(prod, "NCM"))[:8],
            "unit": _text(prod, "uCom")[:6].upper(),
            "suggested_unit": UNITS.get(_text(prod, "uCom").upper(), "UN"),
            "quantity": str(quantity.normalize()),
            "quantity_milli": int((quantity * 1000).quantize(Decimal("1"), rounding=ROUND_HALF_UP)),
            "unit_price_cents": _cents(_decimal(prod, "vUnCom")),
            "gross_cents": _cents(gross),
            "total_cost_cents": _cents(total),
        })
    if not items:
        raise APIError("NF-e sem itens")
    if len(items) > 200:
        raise APIError("NF-e com mais de 200 itens não é suportada")
    installments = []
    for dup in inf.findall(f"{NS}cobr/{NS}dup"):
        try:
            due = date.fromisoformat(_text(dup, "dVenc"))
        except ValueError:
            raise APIError("Vencimento de duplicata inválido")
        installments.append({"number": _text(dup, "nDup")[:20], "due_date": due.isoformat(),
                             "amount_cents": _cents(_decimal(dup, "vDup"))})
    return {
        "key": key, "number": _text(inf, "ide/nNF"), "series": _text(inf, "ide/serie"),
        "issued_on": issued.isoformat() if issued else None, "protocol": protocol,
        "supplier": {"document": _text(emit, "CNPJ") or _text(emit, "CPF"), "name": _text(emit, "xNome")[:180],
                     "trade_name": _text(emit, "xFant")[:180]},
        "recipient_document": _text(_find(inf, "dest"), "CNPJ") or _text(_find(inf, "dest"), "CPF"),
        "total_cents": _cents(_decimal(inf, "total/ICMSTot/vNF")),
        "items": items, "installments": installments,
    }
