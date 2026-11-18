"""Leitura de XMLs da NFC-e/NF-e (nfeProc, NFe, procEventoNFe, procInutNFe).

Python puro (biblioteca padrão). O XML autorizado é a fonte da verdade do pacote do
contador e do SPED: data de emissão, chave, número, série, totais, itens, CFOP/CST e
pagamentos saem daqui. Dinheiro sempre em centavos inteiros.

Os XMLs vêm da SEFAZ/provedor, mas por segurança recusamos DOCTYPE/ENTITY.
"""
import xml.etree.ElementTree as ET
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

NS = "{http://www.portalfiscal.inf.br/nfe}"
MAX_BYTES = 5 * 1024 * 1024

# cStat de autorização (100 = autorizado, 150 = autorizado fora de prazo).
AUTHORIZED = {"100", "150"}
# Denegação: 110, 301, 302, 303 (NT 2013.003 / MOC).
DENIED = {"110", "301", "302", "303"}
# Evento de cancelamento homologado (135 vinculado, 155 fora de prazo).
EVENT_OK = {"135", "136", "155"}
CANCEL_EVENTS = {"110111", "110112"}  # cancelamento e cancelamento por substituição
INUT_OK = {"102"}

TOTAL_FIELDS = ("vBC", "vICMS", "vICMSDeson", "vFCP", "vBCST", "vST", "vFCPST", "vFCPSTRet",
                "vProd", "vFrete", "vSeg", "vDesc", "vII", "vIPI", "vIPIDevol", "vPIS", "vCOFINS",
                "vOutro", "vNF", "vTotTrib")


class XMLError(ValueError):
    pass


def _q(path):
    return "/".join(NS + part for part in path.split("/"))


def _find(node, path):
    return node.find(_q(path)) if node is not None else None


def _text(node, path, default=""):
    found = _find(node, path)
    return (found.text or "").strip() if found is not None and found.text else default


def cents(raw):
    """'12.34' -> 1234 (HALF_UP). Vazio -> 0."""
    if raw in (None, ""):
        return 0
    try:
        value = Decimal(str(raw).strip())
    except InvalidOperation:
        raise XMLError(f"Valor numérico inválido: {raw!r}")
    if not value.is_finite():
        raise XMLError(f"Valor numérico inválido: {raw!r}")
    return int((value * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def decimal_text(raw, default="0"):
    try:
        value = Decimal(str(raw or default).strip())
    except InvalidOperation:
        raise XMLError(f"Valor numérico inválido: {raw!r}")
    return value


def parse_datetime(raw):
    """dhEmi/dhRecbto ISO 8601 com fuso ('2026-10-31T23:30:00-03:00'). Data local = a do próprio texto."""
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        raise XMLError(f"Data inválida no XML: {raw!r}")


def load(raw):
    if isinstance(raw, memoryview):
        raw = raw.tobytes()
    if isinstance(raw, str):
        raw = raw.encode("utf-8")
    if not raw:
        raise XMLError("XML vazio")
    if len(raw) > MAX_BYTES:
        raise XMLError("XML maior que o limite")
    upper = raw.upper()
    if b"<!DOCTYPE" in upper or b"<!ENTITY" in upper:
        raise XMLError("XML com DOCTYPE/ENTITY não é aceito")
    try:
        return ET.fromstring(raw), raw
    except ET.ParseError as exc:
        raise XMLError(f"XML inválido: {exc}")


def kind_of(raw):
    """'nfeProc', 'NFe', 'procEventoNFe', 'procInutNFe', ... (tag raiz sem namespace)."""
    root, _ = load(raw)
    return root.tag.replace(NS, "")


def _icms(imposto):
    group = _find(imposto, "ICMS")
    node = next(iter(group), None) if group is not None else None
    if node is None:
        return {"group": "", "origin": "", "cst": "", "csosn": ""}
    get = lambda tag: _text(node, tag)  # noqa: E731
    return {
        "group": node.tag.replace(NS, ""),
        "origin": get("orig"), "cst": get("CST"), "csosn": get("CSOSN"),
        "bc_cents": cents(get("vBC")), "rate": decimal_text(get("pICMS")),
        "value_cents": cents(get("vICMS")), "bc_reduction": decimal_text(get("pRedBC")),
        "fcp_cents": cents(get("vFCP")),
        "st_bc_cents": cents(get("vBCST")), "st_cents": cents(get("vICMSST")),
        "fcp_st_cents": cents(get("vFCPST")),
        "st_retained_bc_cents": cents(get("vBCSTRet")), "st_retained_cents": cents(get("vICMSSTRet")),
        "desoneration_cents": cents(get("vICMSDeson")),
    }


def _pis_cofins(imposto, tag):
    group = _find(imposto, tag)
    node = next(iter(group), None) if group is not None else None
    if node is None:
        return {"cst": "", "bc_cents": 0, "rate": Decimal("0"), "value_cents": 0}
    rate_tag = "pPIS" if tag == "PIS" else "pCOFINS"
    value_tag = "vPIS" if tag == "PIS" else "vCOFINS"
    return {"cst": _text(node, "CST"), "bc_cents": cents(_text(node, "vBC")),
            "rate": decimal_text(_text(node, rate_tag)), "value_cents": cents(_text(node, value_tag))}


def _ipi(imposto):
    group = _find(imposto, "IPI")
    if group is None:
        return 0
    trib = _find(group, "IPITrib")
    return cents(_text(trib, "vIPI")) if trib is not None else 0


def parse_nfe(raw):
    """nfeProc (ou NFe sem protocolo) -> dict normalizado."""
    root, data = load(raw)
    tag = root.tag.replace(NS, "")
    if tag == "nfeProc":
        nfe = _find(root, "NFe")
        prot = _find(root, "protNFe/infProt")
    elif tag == "NFe":
        nfe, prot = root, None
    else:
        raise XMLError(f"Esperado nfeProc ou NFe, recebido {tag}")
    inf = _find(nfe, "infNFe")
    if inf is None:
        raise XMLError("infNFe não encontrado")
    key = (inf.get("Id") or "").replace("NFe", "")
    ide = _find(inf, "ide")
    tot = _find(inf, "total/ICMSTot")
    totals = {name: cents(_text(tot, name)) for name in TOTAL_FIELDS}
    items = []
    for det in inf.findall(_q("det")):
        prod = _find(det, "prod")
        imposto = _find(det, "imposto")
        gtin = _text(prod, "cEAN")
        items.append({
            "number": int(det.get("nItem") or len(items) + 1),
            "code": _text(prod, "cProd"), "gtin": "" if gtin.upper() == "SEM GTIN" else gtin,
            "description": _text(prod, "xProd"), "ncm": _text(prod, "NCM"), "cest": _text(prod, "CEST"),
            "ex_tipi": _text(prod, "EXTIPI"),
            "cfop": _text(prod, "CFOP"), "unit": _text(prod, "uCom"),
            "quantity": decimal_text(_text(prod, "qCom")),
            "unit_price": decimal_text(_text(prod, "vUnCom")),
            "gross_cents": cents(_text(prod, "vProd")), "discount_cents": cents(_text(prod, "vDesc")),
            "freight_cents": cents(_text(prod, "vFrete")), "insurance_cents": cents(_text(prod, "vSeg")),
            "other_cents": cents(_text(prod, "vOutro")),
            "icms": _icms(imposto), "pis": _pis_cofins(imposto, "PIS"),
            "cofins": _pis_cofins(imposto, "COFINS"), "ipi_cents": _ipi(imposto),
        })
    payments = []
    pag = _find(inf, "pag")
    for det in (pag.findall(_q("detPag")) if pag is not None else []):
        payments.append({"type": _text(det, "tPag"), "amount_cents": cents(_text(det, "vPag")),
                         "description": _text(det, "xPag")})
    change = cents(_text(pag, "vTroco")) if pag is not None else 0
    dest = _find(inf, "dest")
    customer = None
    if dest is not None:
        customer = {"cnpj": _text(dest, "CNPJ"), "cpf": _text(dest, "CPF"), "name": _text(dest, "xNome")}
    emit = _find(inf, "emit")
    protocol = None
    if prot is not None:
        protocol = {"status": _text(prot, "cStat"), "reason": _text(prot, "xMotivo"),
                    "number": _text(prot, "nProt"), "received_at": parse_datetime(_text(prot, "dhRecbto")),
                    "key": _text(prot, "chNFe")}
    return {
        "kind": tag, "key": key or (protocol or {}).get("key", ""),
        "model": _text(ide, "mod"), "series": int(_text(ide, "serie") or 0),
        "number": int(_text(ide, "nNF") or 0), "issued_at": parse_datetime(_text(ide, "dhEmi")),
        "exit_at": parse_datetime(_text(ide, "dhSaiEnt")),
        "emission_type": _text(ide, "tpEmis") or "1", "environment": _text(ide, "tpAmb"),
        "operation": _text(ide, "tpNF"), "uf_code": _text(ide, "cUF"),
        "freight_mode": _text(_find(inf, "transp"), "modFrete") or "9",
        "issuer": {"cnpj": _text(emit, "CNPJ"), "cpf": _text(emit, "CPF"), "ie": _text(emit, "IE"),
                   "crt": _text(emit, "CRT"), "uf": _text(emit, "enderEmit/UF")},
        "customer": customer, "totals": totals, "items": items, "payments": payments,
        "change_cents": change, "protocol": protocol, "raw": data,
    }


def parse_event(raw):
    """procEventoNFe -> {type, key, status, protocol, registered_at, sequence, reason}."""
    root, data = load(raw)
    tag = root.tag.replace(NS, "")
    if tag != "procEventoNFe":
        raise XMLError(f"Esperado procEventoNFe, recebido {tag}")
    inf = _find(root, "evento/infEvento")
    ret = _find(root, "retEvento/infEvento")
    return {
        "type": _text(inf, "tpEvento"), "key": _text(inf, "chNFe") or _text(ret, "chNFe"),
        "event_at": parse_datetime(_text(inf, "dhEvento")),
        "sequence": int(_text(inf, "nSeqEvento") or 1),
        "reason": _text(inf, "detEvento/xJust"),
        "status": _text(ret, "cStat"), "protocol": _text(ret, "nProt"),
        "registered_at": parse_datetime(_text(ret, "dhRegEvento")), "raw": data,
    }


def is_cancellation(event):
    return event["type"] in CANCEL_EVENTS and event["status"] in EVENT_OK


def parse_inutilization(raw):
    """procInutNFe -> {model, series, start, end, year, status, protocol, received_at, cnpj, reason}."""
    root, data = load(raw)
    tag = root.tag.replace(NS, "")
    if tag != "procInutNFe":
        raise XMLError(f"Esperado procInutNFe, recebido {tag}")
    inf = _find(root, "inutNFe/infInut")
    ret = _find(root, "retInutNFe/infInut")
    pick = lambda name: _text(ret, name) or _text(inf, name)  # noqa: E731
    return {
        "model": pick("mod"), "series": int(pick("serie") or 0),
        "start": int(pick("nNFIni") or 0), "end": int(pick("nNFFin") or 0),
        "year": pick("ano"), "cnpj": pick("CNPJ"), "reason": _text(inf, "xJust"),
        "status": _text(ret, "cStat"), "protocol": _text(ret, "nProt"),
        "received_at": parse_datetime(_text(ret, "dhRecbto")), "raw": data,
    }
