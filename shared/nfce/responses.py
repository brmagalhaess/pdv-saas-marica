"""Leitura dos retornos da SEFAZ (SOAP 1.2) e montagem dos XML de distribuição.

Retornos: retEnviNFe (autorização síncrona com protNFe), retConsSitNFe, retConsStatServ,
retEnvEvento e retInutNFe. Distribuição: nfeProc (NFe + protNFe), procEventoNFe e procInutNFe.
"""
from . import messages
from .xml import E, NFE_NS, XMLError, from_etree, parse, split_tag, tobytes


def _find(root, local):
    for node in root.iter():
        if split_tag(node.tag) == (NFE_NS, local):
            return node
    return None


def _text(node, local):
    if node is None:
        return ""
    for child in node:
        if split_tag(child.tag) == (NFE_NS, local):
            return (child.text or "").strip()
    return ""


def body(data, expected):
    """The element ``expected`` (retEnviNFe, ...) inside the SOAP answer."""
    root = parse(data)
    found = _find(root, expected)
    if found is None:
        fault = next((n for n in root.iter() if split_tag(n.tag)[1] in ("Fault", "faultstring")), None)
        text = " ".join(t.strip() for t in fault.itertext() if t.strip())[:300] if fault is not None else ""
        raise XMLError("Resposta da SEFAZ sem " + expected + (f": {text}" if text else ""))
    return found


def _protocol(prot):
    info = _find(prot, "infProt") if prot is not None else None
    if info is None:
        return None
    return {"status": _text(info, "cStat"), "reason": _text(info, "xMotivo"), "key": _text(info, "chNFe"),
            "protocol": _text(info, "nProt"), "received_at": _text(info, "dhRecbto"),
            "digest": _text(info, "digVal"), "environment": _text(info, "tpAmb"),
            "element": from_etree(prot, NFE_NS)}


def authorization(data):
    ret = body(data, "retEnviNFe")
    result = {"batch_status": _text(ret, "cStat"), "batch_reason": _text(ret, "xMotivo"),
              "receipt": _text(_find(ret, "infRec"), "nRec") if _find(ret, "infRec") is not None else "",
              "protocol": _protocol(_find(ret, "protNFe"))}
    return result


def consultation(data):
    ret = body(data, "retConsSitNFe")
    events = []
    for node in ret.iter():
        if split_tag(node.tag) == (NFE_NS, "procEventoNFe"):
            info = _find(node, "infEvento")
            retinfo = _find(_find(node, "retEvento"), "infEvento") if _find(node, "retEvento") is not None else None
            events.append({"type": _text(info, "tpEvento"), "status": _text(retinfo, "cStat"),
                           "protocol": _text(retinfo, "nProt"), "element": from_etree(node, NFE_NS)})
    return {"status": _text(ret, "cStat"), "reason": _text(ret, "xMotivo"), "key": _text(ret, "chNFe"),
            "protocol": _protocol(_find(ret, "protNFe")), "events": events}


def service_status(data):
    ret = body(data, "retConsStatServ")
    return {"status": _text(ret, "cStat"), "reason": _text(ret, "xMotivo"), "received_at": _text(ret, "dhRecbto"),
            "average_seconds": _text(ret, "tMed"), "online": _text(ret, "cStat") == messages.SERVICE_OK}


def event(data):
    ret = body(data, "retEnvEvento")
    node = _find(ret, "retEvento")
    info = _find(node, "infEvento") if node is not None else None
    return {"batch_status": _text(ret, "cStat"), "batch_reason": _text(ret, "xMotivo"),
            "status": _text(info, "cStat"), "reason": _text(info, "xMotivo"), "protocol": _text(info, "nProt"),
            "registered_at": _text(info, "dhRegEvento"),
            "element": from_etree(node, NFE_NS) if node is not None else None}


def inutilization(data):
    ret = body(data, "retInutNFe")
    info = _find(ret, "infInut")
    return {"status": _text(info, "cStat"), "reason": _text(info, "xMotivo"), "protocol": _text(info, "nProt"),
            "received_at": _text(info, "dhRecbto"), "element": from_etree(ret, NFE_NS)}


def nfe_proc(nfe, protocol_element):
    """nfeProc com a NFe assinada e o protNFe da SEFAZ (XML de distribuição)."""
    nfe.ns = None
    prot = protocol_element
    prot.ns = None
    return tobytes(E("nfeProc", nfe, prot, ns=NFE_NS, versao="4.00"), declaration=True)


def event_proc(event_element, ret_element):
    event_element.ns = None
    ret_element.ns = None
    return tobytes(E("procEventoNFe", event_element, ret_element, ns=NFE_NS, versao="1.00"), declaration=True)


def inutilization_proc(request_element, ret_element):
    request_element.ns = None
    ret_element.ns = None
    return tobytes(E("procInutNFe", request_element, ret_element, ns=NFE_NS, versao="4.00"), declaration=True)
