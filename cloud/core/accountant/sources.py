"""Adaptador único de leitura das NFC-e para o pacote do contador e o SPED.

Contrato com a frente de emissão (nomes podem variar; tudo aqui usa getattr com fallback):
- FiscalDocument: access_key, number, series, model (65), issued_at, emission_type, cancelled_at e
  status em {pending, authorized, contingency, rejected, cancelled};
- operations.FiscalPayload.xml_content: nfeProc completo (NFe + protNFe);
- FiscalEvent (document FK, kind, xml_content = procEventoNFe) para cancelamentos;
- um modelo de inutilização com o procInutNFe em xml_content.

O XML é a fonte da verdade: data de emissão (dhEmi, no fuso do próprio texto), chave, número, série,
totais, itens, CFOP/CST e pagamentos saem dele. Campos do modelo só completam o que falta (nota sem XML).
Se os nomes da outra frente forem diferentes, ajuste só as listas *_MODEL_NAMES abaixo.
"""
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from django.apps import apps
from django.conf import settings
from django.db.models import Max

from shared.fiscal import nfce_xml
from shared.fiscal.efd import month_bounds

EVENT_MODEL_NAMES = ("FiscalEvent", "FiscalDocumentEvent", "NFCeEvent")
INUTILIZATION_MODEL_NAMES = ("FiscalInutilization", "FiscalNumberVoid", "NumberInutilization",
                             "FiscalInutilizacao", "Inutilization", "FiscalVoidRange")
XML_FIELDS = ("xml_content", "xml", "processed_xml")
MODEL_STATUS = {"authorized": "authorized", "autorizado": "authorized", "cancelled": "cancelled",
                "canceled": "cancelled", "cancelado": "cancelled", "contingency": "contingency",
                "rejected": "rejected", "error": "rejected", "erro_autorizacao": "rejected",
                "denied": "denied", "denegado": "denied"}


def local_zone():
    return ZoneInfo(settings.TIME_ZONE)


def _model(names):
    for model in apps.get_models():
        if model.__name__ in names:
            return model
    return None


def _has_field(model, name):
    return any(f.name == name for f in model._meta.get_fields())


def _xml_of(row):
    for name in XML_FIELDS:
        value = getattr(row, name, None)
        if value:
            return bytes(value) if not isinstance(value, str) else value.encode()
    return None


def _local_date(moment, zone):
    if moment is None:
        return None
    if moment.tzinfo is None:
        return moment.date()
    return moment.astimezone(zone).date()


def load_events(tenant, document_ids):
    """{document_id: [procEventoNFe bytes]} — vazio enquanto o modelo de eventos não existir."""
    model = _model(EVENT_MODEL_NAMES)
    if model is None or not document_ids:
        return {}
    result = {}
    for row in model.objects.filter(document_id__in=list(document_ids)):
        xml = _xml_of(row)
        if xml:
            result.setdefault(row.document_id, []).append(xml)
    return result


def load_inutilization_xmls(tenant):
    """[procInutNFe bytes] da empresa — vazio enquanto o modelo de inutilização não existir."""
    model = _model(INUTILIZATION_MODEL_NAMES)
    if model is None:
        return []
    rows = model.objects.filter(tenant=tenant)
    return [xml for xml in (_xml_of(row) for row in rows) if xml]


def _window(year, month, zone):
    start, end = month_bounds(year, month)
    begin = datetime.combine(start, time.min, zone)
    finish = datetime.combine(end + timedelta(days=1), time.min, zone)
    return start, end, begin, finish


def _candidates(tenant, begin, finish):
    from core.models import FiscalDocument
    rows = FiscalDocument.objects.filter(tenant=tenant)
    margin = timedelta(days=2)
    if _has_field(FiscalDocument, "issued_at"):
        from django.db.models import Q
        # Sem issued_at (nota que nunca saiu) cai na data de criação, com folga para contingência.
        return rows.filter(Q(issued_at__gte=begin - margin, issued_at__lt=finish + margin)
                           | Q(issued_at__isnull=True, created_at__gte=begin - margin,
                               created_at__lt=finish + timedelta(days=45)))
    # Modelo atual: a nota em contingência pode chegar à nuvem dias depois da emissão.
    return rows.filter(created_at__gte=begin - margin, created_at__lt=finish + timedelta(days=45))


def normalize(row, xml, events, zone):
    """Um FiscalDocument (+ XMLs) -> dict normalizado usado pelo resumo, pelo ZIP e pelo SPED."""
    warnings = []
    nfe = None
    if xml:
        try:
            nfe = nfce_xml.parse_nfe(xml)
        except nfce_xml.XMLError as exc:
            warnings.append(f"XML ilegível ({exc})")
    cancel = None
    cancel_xml = None
    for raw in events:
        try:
            event = nfce_xml.parse_event(raw)
        except nfce_xml.XMLError as exc:
            warnings.append(f"Evento ilegível ({exc})")
            continue
        if nfce_xml.is_cancellation(event):
            cancel, cancel_xml = event, raw
    model_status = MODEL_STATUS.get(str(getattr(row, "status", "") or "").lower(), "pending")
    protocol = (nfe or {}).get("protocol") or {}
    if cancel:
        status = "cancelled"
    elif model_status == "cancelled":
        status = "cancelled"
        warnings.append("cancelada sem o XML do evento de cancelamento arquivado")
    elif nfe and protocol.get("status") in nfce_xml.AUTHORIZED:
        status = "authorized"
    elif nfe and protocol.get("status") in nfce_xml.DENIED:
        status = "denied"
    elif model_status == "authorized":
        status = "authorized"
        if nfe:
            warnings.append("autorizada, mas o XML arquivado não tem o protocolo de autorização (nfeProc)")
    elif model_status in ("contingency",) or (nfe and nfe["emission_type"] == "9" and not protocol):
        status = "contingency"
    elif model_status in ("rejected", "denied"):
        status = model_status
    else:
        status = "pending"
    if nfe and nfe.get("model") and nfe["model"] != "65":
        return None
    if not nfe and str(getattr(row, "model", 65) or 65) != "65":
        return None
    issued_at = (nfe or {}).get("issued_at") or getattr(row, "issued_at", None) or row.created_at
    # dhEmi já vem no fuso do emitente: a data do próprio texto é a data fiscal.
    local_date = issued_at.date() if nfe and nfe.get("issued_at") else _local_date(issued_at, zone)
    key = (nfe or {}).get("key") or getattr(row, "access_key", "") or ""
    if status in ("cancelled",) and not nfe and not key and cancel:
        key = cancel["key"]
    return {
        "id": str(row.pk), "sale_id": str(getattr(row, "sale_id", "") or ""), "status": status,
        "model_status": getattr(row, "status", ""), "model": "65",
        "series": (nfe or {}).get("series") or getattr(row, "series", None) or 0,
        "number": (nfe or {}).get("number") or getattr(row, "number", None) or 0,
        "key": key, "issued_at": issued_at, "local_date": local_date,
        "emission_type": (nfe or {}).get("emission_type") or str(getattr(row, "emission_type", "") or "1"),
        "protocol": protocol.get("number") or getattr(row, "authorization", "") or "",
        "nfe": nfe, "xml": xml if nfe else None, "cancel": cancel, "cancel_xml": cancel_xml,
        "cancelled_at": (cancel or {}).get("registered_at") or getattr(row, "cancelled_at", None),
        "error_message": getattr(row, "error_message", "") or "", "warnings": warnings, "corrected": False,
    }


def iter_month(tenant, year, month):
    """Documentos NFC-e da empresa emitidos no mês (data local da emissão), normalizados e ordenados."""
    from operations.models import FiscalPayload
    zone = local_zone()
    start, end, begin, finish = _window(year, month, zone)
    rows = list(_candidates(tenant, begin, finish).order_by("created_at"))
    payloads = {p.document_id: p.xml_content for p in
                FiscalPayload.objects.filter(document__in=rows).only("document_id", "xml_content")}
    events = load_events(tenant, [row.pk for row in rows])
    docs = []
    for row in rows:
        xml = payloads.get(row.pk)
        doc = normalize(row, bytes(xml) if xml else None, events.get(row.pk, ()), zone)
        if doc and start <= doc["local_date"] <= end:
            docs.append(doc)
    # Rejeitada "corrigida": a mesma venda saiu depois, ou o número foi reaproveitado por outra nota.
    issued = {(d["series"], d["number"]) for d in docs if d["status"] in ("authorized", "cancelled", "contingency")}
    sales_ok = {d["sale_id"] for d in docs if d["status"] in ("authorized", "cancelled", "contingency", "pending")}
    for doc in docs:
        if doc["status"] == "rejected":
            doc["corrected"] = bool((doc["sale_id"] and doc["sale_id"] in sales_ok)
                                    or (doc["number"] and (doc["series"], doc["number"]) in issued))
    docs.sort(key=lambda d: (d["series"], d["number"], d["issued_at"].timestamp() if d["issued_at"] else 0))
    return docs


def inutilizations_month(tenant, year, month):
    """Inutilizações homologadas (cStat 102) no mês, pela data do recibo (dhRecbto)."""
    start, end = month_bounds(year, month)
    result = []
    for raw in load_inutilization_xmls(tenant):
        try:
            inut = nfce_xml.parse_inutilization(raw)
        except nfce_xml.XMLError:
            continue
        if inut["status"] not in nfce_xml.INUT_OK or (inut["model"] and inut["model"] != "65"):
            continue
        when = inut["received_at"]
        if when is None:
            continue
        inut["local_date"] = when.date()
        inut["xml"] = raw
        if start <= inut["local_date"] <= end:
            result.append(inut)
    return sorted(result, key=lambda i: (i["series"], i["start"]))


def previous_numbers(tenant, year, month):
    """{série: maior número antes do mês}, quando o modelo já tem number/series/issued_at."""
    from core.models import FiscalDocument
    if not all(_has_field(FiscalDocument, f) for f in ("number", "series", "issued_at")):
        return {}
    zone = local_zone()
    _, _, begin, _ = _window(year, month, zone)
    rows = (FiscalDocument.objects.filter(tenant=tenant, issued_at__lt=begin, issued_at__gte=begin - timedelta(days=62))
            .exclude(status__in=("rejected", "pending")).values("series").annotate(top=Max("number")))
    return {int(r["series"] or 0): int(r["top"] or 0) for r in rows if r["top"]}
