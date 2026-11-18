"""Painel: configuração da NFC-e (Minha empresa > Nota fiscal), documentos, eventos e inutilizações.

Segredos (CSC, token da Focus, certificado A1 e senha) entram cifrados com PDV_ENCRYPTION_KEY e
nunca voltam ao navegador: as respostas só dizem se estão configurados.
"""
import base64
import binascii

from django.db import transaction
from django.db.models import Max
from django.http import HttpResponse, JsonResponse
from django.utils import timezone

from core.api_common import APIError, audit, endpoint, fields, membership_context, method, payload, text, uuid_value
from core.models import FiscalDocument, FiscalEvent, FiscalInutilization, Sale
from core.nfce import service
from core.nfce.certificate import CertificateError, load_pfx, validity
from integrations.base import ProviderError
from shared.fiscal.validators import cnpj_value
from shared.nfce.webservices import AUTHORIZER, cancel_minutes
from shared.secrets import encrypt_secret
from .models import FiscalPayload, IntegrationAccount

OWNER = ("owner", "admin")
READERS = ("owner", "admin", "manager", "finance", "viewer")
PRODUCTION_CONFIRMATION = "EMITIR EM PRODUCAO"


def _encrypt(value):
    try:
        return encrypt_secret(value)
    except ValueError:
        raise APIError("Chave de criptografia do servidor não configurada", 503)


def config_data(tenant):
    config = service.config_for(tenant)
    focus = IntegrationAccount.objects.filter(provider="focusnfe", tenant=tenant).first()
    certificate = None
    if config.certificate_encrypted:
        state, days = ("missing", None)
        if config.certificate_not_after:
            class _Dates:
                not_before = config.certificate_not_before
                not_after = config.certificate_not_after
            state, days = validity(_Dates)
        certificate = {"subject": config.certificate_subject, "cnpj": config.certificate_cnpj,
                       "not_before": config.certificate_not_before.isoformat() if config.certificate_not_before else None,
                       "not_after": config.certificate_not_after.isoformat() if config.certificate_not_after else None,
                       "state": state, "days_left": days,
                       "matches_company": cnpj_value(config.certificate_cnpj) == cnpj_value(tenant.document)}
    last = (FiscalDocument.objects.filter(tenant=tenant, environment=tenant.fiscal_environment, series=tenant.nfce_series)
            .aggregate(value=Max("number"))["value"])
    return {"environment": tenant.fiscal_environment, "mode": config.mode, "qr_version": config.qr_version,
            "series": tenant.nfce_series, "next_number": tenant.nfce_next_number, "last_used_number": last,
            "csc": {"homologation": {"id": config.csc_homologation_id, "configured": bool(config.csc_homologation_encrypted)},
                    "production": {"id": config.csc_production_id, "configured": bool(config.csc_production_encrypted)}},
            "focus": {"configured": bool(focus), "enabled": bool(focus and focus.enabled)},
            "certificate": certificate, "terminal_contingency": config.terminal_contingency,
            "accountant_email": tenant.accountant_email, "uf": tenant.address_state,
            "authorizer": AUTHORIZER.get(tenant.address_state, ""), "cancel_minutes": cancel_minutes(tenant.address_state),
            "live_allowed": service.live_allowed("production"),
            "production_confirmation": PRODUCTION_CONFIRMATION, "alerts": service.contingency_alerts(tenant)}


@endpoint
def fiscal_config(request):
    method(request, "GET", "PATCH")
    member = membership_context(request, OWNER, write=request.method == "PATCH")
    tenant = member.tenant
    if request.method == "PATCH":
        data = payload(request)
        fields(data, ("environment", "mode", "qr_version", "series", "next_number", "csc_homologation_id",
                      "csc_homologation", "csc_production_id", "csc_production", "focus_token", "terminal_contingency",
                      "accountant_email", "confirmation"))
        if not data:
            raise APIError("Informe os dados a alterar")
        config = service.config_for(tenant)
        changed = sorted(key for key in data if key not in ("csc_homologation", "csc_production", "focus_token"))
        tenant_fields = set()
        if "environment" in data:
            value = data["environment"]
            if value not in ("non_fiscal", "homologation", "production"):
                raise APIError("Ambiente fiscal inválido")
            if value == "production" and tenant.fiscal_environment != "production":
                if data.get("confirmation") != PRODUCTION_CONFIRMATION:
                    raise APIError(f'Para emitir notas com valor fiscal digite "{PRODUCTION_CONFIRMATION}"', 400,
                                   code="production_confirmation")
                if not service.live_allowed("production"):
                    raise APIError("A plataforma ainda não liberou emissão em produção", 409, code="fiscal_live_disabled")
            tenant.fiscal_environment = value
            tenant_fields.add("fiscal_environment")
        if "mode" in data:
            if data["mode"] not in ("sefaz", "focusnfe"):
                raise APIError("Modo de emissão inválido")
            config.mode = data["mode"]
        if "qr_version" in data:
            if data["qr_version"] not in (2, 3):
                raise APIError("Versão do QR Code deve ser 2 ou 3")
            config.qr_version = data["qr_version"]
        if "series" in data:
            if type(data["series"]) is not int or not 0 <= data["series"] <= 999:
                raise APIError("Série NFC-e deve ficar entre 0 e 999")
            tenant.nfce_series = data["series"]
            tenant_fields.add("nfce_series")
        if "next_number" in data:
            value = data["next_number"]
            if type(value) is not int or not 1 <= value <= 999999999:
                raise APIError("Próximo número inválido")
            series = tenant.nfce_series
            used = (FiscalDocument.objects.filter(tenant=tenant, environment=tenant.fiscal_environment, series=series)
                    .aggregate(value=Max("number"))["value"])
            if used and value <= used:
                raise APIError(f"O número {value} já foi usado nesta série (último: {used}). Nunca reutilize números.", 409)
            tenant.nfce_next_number = value
            tenant_fields.add("nfce_next_number")
        for environment in ("homologation", "production"):
            id_key, value_key = f"csc_{environment}_id", f"csc_{environment}"
            if id_key in data:
                value = text(data[id_key], "Identificador do CSC", 6, optional=True)
                if value and not value.isdigit():
                    raise APIError("O identificador do CSC tem só números (ex.: 000001)")
                setattr(config, id_key, value)
            if value_key in data:
                secret = text(data[value_key], "CSC", 36, optional=True)
                if secret and len(secret) < 16:
                    raise APIError("CSC deve ter de 16 a 36 caracteres")
                setattr(config, f"csc_{environment}_encrypted", _encrypt(secret) if secret else "")
        if "terminal_contingency" in data:
            if type(data["terminal_contingency"]) is not bool:
                raise APIError("Opção de contingência inválida")
            config.terminal_contingency = data["terminal_contingency"]
        if "accountant_email" in data:
            value = text(data["accountant_email"], "E-mail do contador", 254, optional=True)
            if value and ("@" not in value or "." not in value.split("@")[-1] or " " in value):
                raise APIError("E-mail do contador inválido")
            # Um só e-mail do contador: Tenant.accountant_email (usado também pelo pacote mensal).
            tenant.accountant_email = value
            tenant.save(update_fields=["accountant_email"])
        with transaction.atomic():
            if "focus_token" in data:
                token = text(data["focus_token"], "Token da Focus NFe", 1000)
                row = IntegrationAccount.objects.filter(provider="focusnfe", tenant=tenant).first() or \
                    IntegrationAccount(provider="focusnfe", tenant=tenant)
                row.token_encrypted = _encrypt(token)
                row.environment = tenant.fiscal_environment if tenant.fiscal_environment != "non_fiscal" else "homologation"
                row.enabled = True
                row.save()
            if tenant_fields:
                tenant.save(update_fields=sorted(tenant_fields))
            config.save()
            audit(tenant, request.user, "fiscal.config_updated", tenant,
                  {"fields": changed, "secrets": sorted(k for k in data if k in ("csc_homologation", "csc_production", "focus_token"))})
        # The Focus account follows the company environment.
        if tenant.fiscal_environment in ("homologation", "production"):
            IntegrationAccount.objects.filter(provider="focusnfe", tenant=tenant).update(
                environment=tenant.fiscal_environment)
    return JsonResponse(config_data(tenant))


@endpoint
def fiscal_certificate(request):
    method(request, "POST", "DELETE")
    member = membership_context(request, OWNER, write=True)
    tenant = member.tenant
    config = service.config_for(tenant)
    if request.method == "DELETE":
        config.certificate_encrypted = config.certificate_password_encrypted = ""
        config.certificate_subject = config.certificate_cnpj = ""
        config.certificate_not_before = config.certificate_not_after = config.certificate_uploaded_at = None
        config.save()
        audit(tenant, request.user, "fiscal.certificate_removed", tenant)
        return JsonResponse(config_data(tenant))
    data = payload(request)
    fields(data, ("pfx_base64", "password"), ("pfx_base64", "password"))
    if not isinstance(data["pfx_base64"], str) or not isinstance(data["password"], str) or len(data["password"]) > 200:
        raise APIError("Envie o arquivo e a senha do certificado")
    try:
        raw = base64.b64decode(data["pfx_base64"], validate=True)
    except (binascii.Error, ValueError):
        raise APIError("Arquivo do certificado ilegível")
    try:
        certificate = load_pfx(raw, data["password"])
    except CertificateError as exc:
        raise APIError(str(exc))
    company = cnpj_value(tenant.document)
    if not certificate.cnpj:
        raise APIError("Não encontramos o CNPJ no certificado. Use um certificado e-CNPJ A1 da empresa.")
    if certificate.cnpj != company:
        raise APIError(f"O certificado é do CNPJ {certificate.cnpj}, diferente do CNPJ da empresa ({company or 'não informado'}).",
                       409, code="certificate_cnpj_mismatch")
    state, _days = validity(certificate)
    if state == "expired":
        raise APIError("Este certificado já venceu. Envie o certificado renovado.")
    config.certificate_encrypted = _encrypt(base64.b64encode(raw).decode("ascii"))
    config.certificate_password_encrypted = _encrypt(data["password"])
    config.certificate_subject = certificate.subject[:300]
    config.certificate_cnpj = certificate.cnpj
    config.certificate_not_before = certificate.not_before
    config.certificate_not_after = certificate.not_after
    config.certificate_uploaded_at = timezone.now()
    config.save()
    audit(tenant, request.user, "fiscal.certificate_uploaded", tenant,
          {"cnpj": certificate.cnpj, "not_after": certificate.not_after.isoformat()})
    return JsonResponse(config_data(tenant))


@endpoint
def fiscal_test(request):
    method(request, "POST")
    member = membership_context(request, OWNER, write=True)
    tenant = member.tenant
    try:
        backend = service.backend_for(tenant)
        result = backend.status()
    except ProviderError as exc:
        result = {"online": False, "message": str(exc)}
    except (ValueError, CertificateError) as exc:
        result = {"online": False, "message": str(exc)}
    audit(tenant, request.user, "fiscal.communication_tested", tenant, {"online": result.get("online")})
    return JsonResponse({"environment": tenant.fiscal_environment, **result})


@endpoint
def fiscal_submit(request):
    """Emit (or re-query) the NFC-e of a sale from the panel. The payload comes from the tax engine."""
    method(request, "POST")
    member = membership_context(request, ("owner", "admin", "manager"), write=True)
    data = payload(request)
    fields(data, ("sale_id", "fiscal_payload"), ("sale_id",))
    sale = Sale.objects.select_related("tenant", "shop").filter(pk=uuid_value(data["sale_id"]), tenant=member.tenant).first()
    if not sale:
        raise APIError("Venda não encontrada", 404)
    if sale.status != "completed":
        raise APIError("Venda cancelada não emite nota", 409)
    try:
        service.validate_sale(sale)
    except service.NfceError as exc:
        raise APIError(service._issues_text(exc), 422, code="fiscal_data_incomplete")
    document, created = service.create_document(sale)
    job = None
    if created or document.status == "pending":
        job = service._enqueue("fiscal.submit", member.tenant, "fiscal.submit:" + str(document.pk),
                               {"document_id": str(document.pk)})
    audit(member.tenant, request.user, "fiscal.requested", document)
    return JsonResponse({"document_id": str(document.pk), "job_id": str(job.pk) if job else None,
                         "status": document.status}, status=202)


@endpoint
def fiscal_documents(request):
    method(request, "GET")
    member = membership_context(request, READERS)
    rows = FiscalDocument.objects.filter(tenant=member.tenant)
    if request.GET.get("sale_id"):
        rows = rows.filter(sale_id=uuid_value(request.GET["sale_id"]))
    if request.GET.get("status") in FiscalDocument.STATUSES:
        rows = rows.filter(status=request.GET["status"])
    rows = list(rows.order_by("-created_at")[:100])
    archived = set(FiscalPayload.objects.filter(document__in=rows).exclude(xml_sha256="").values_list("document_id", flat=True))
    events = {}
    for event in FiscalEvent.objects.filter(document__in=rows).order_by("created_at"):
        events.setdefault(event.document_id, []).append(event)
    results = []
    for row in rows:
        item = service.document_data(row)
        item["reference"] = row.external_id
        item["xml_archived"] = row.pk in archived
        item["events"] = [{"id": str(e.pk), "kind": e.kind, "status": e.status, "protocol": e.protocol,
                           "reason": e.reason, "error_message": e.error_message,
                           "xml_archived": bool(e.xml_sha256)} for e in events.get(row.pk, [])]
        results.append(item)
    inutilizations = [{"id": str(x.pk), "environment": x.environment, "series": x.series, "start": x.start, "end": x.end,
                       "status": x.status, "protocol": x.protocol, "error_message": x.error_message,
                       "created_at": x.created_at.isoformat(), "xml_archived": bool(x.xml_sha256)}
                      for x in FiscalInutilization.objects.filter(tenant=member.tenant).order_by("-created_at")[:50]]
    return JsonResponse({"results": results, "inutilizations": inutilizations,
                         "alerts": service.contingency_alerts(member.tenant)})


def _xml_response(content, checksum, filename):
    response = HttpResponse(bytes(content), content_type="application/xml")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    response["Cache-Control"] = "private, no-store"
    response["X-Content-SHA256"] = checksum
    return response


@endpoint
def fiscal_xml(request, pk):
    method(request, "GET")
    member = membership_context(request, ("owner", "admin", "manager", "finance"))
    row = FiscalPayload.objects.select_related("document").filter(document_id=uuid_value(pk),
                                                                   document__tenant=member.tenant).first()
    if not row:
        raise APIError("Documento não encontrado", 404)
    if not row.xml_content:
        raise APIError("XML autorizado ainda não disponível; a nota precisa estar autorizada", 409)
    audit(member.tenant, request.user, "fiscal.xml_downloaded", row.document)
    name = f"{row.document.access_key or row.document_id}-nfce.xml"
    return _xml_response(row.xml_content, row.xml_sha256, name)


@endpoint
def fiscal_event_xml(request, pk):
    method(request, "GET")
    member = membership_context(request, ("owner", "admin", "manager", "finance"))
    row = FiscalEvent.objects.select_related("document").filter(pk=uuid_value(pk), tenant=member.tenant).first()
    if not row:
        raise APIError("Evento não encontrado", 404)
    if not row.xml_content:
        raise APIError("XML do evento ainda não disponível", 409)
    return _xml_response(row.xml_content, row.xml_sha256, f"{row.document.access_key}-{row.event_type}-{row.sequence:02d}-evento.xml")


@endpoint
def fiscal_inutilization_xml(request, pk):
    method(request, "GET")
    member = membership_context(request, ("owner", "admin", "manager", "finance"))
    row = FiscalInutilization.objects.filter(pk=uuid_value(pk), tenant=member.tenant).first()
    if not row:
        raise APIError("Inutilização não encontrada", 404)
    if not row.xml_content:
        raise APIError("XML da inutilização ainda não disponível", 409)
    return _xml_response(row.xml_content, row.xml_sha256, f"inutilizacao-{row.series}-{row.start}-{row.end}.xml")


@endpoint
def fiscal_cancel(request, pk):
    method(request, "POST")
    member = membership_context(request, ("owner", "admin", "manager"), write=True)
    data = payload(request)
    fields(data, ("reason",), ("reason",))
    reason = text(data["reason"], "Justificativa", 255)
    with transaction.atomic():
        row = FiscalDocument.objects.select_for_update().filter(pk=uuid_value(pk), tenant=member.tenant).first()
        if not row:
            raise APIError("Documento não encontrado", 404)
        if row.status not in ("authorized", "contingency", "pending"):
            raise APIError("Somente notas autorizadas podem solicitar cancelamento", 409)
        job = service.request_cancel(row, reason, request.user)
        audit(member.tenant, request.user, "fiscal.cancel_requested", row)
    return JsonResponse({"job_id": str(job.pk) if job else None, "status": job.status if job else "scheduled"}, status=202)


@endpoint
def fiscal_retry(request, pk):
    method(request, "POST")
    member = membership_context(request, ("owner", "admin", "manager"), write=True)
    row = FiscalDocument.objects.select_related("sale", "tenant").filter(pk=uuid_value(pk), tenant=member.tenant).first()
    if not row:
        raise APIError("Documento não encontrado", 404)
    try:
        row = service.retry(row)
    except service.NfceError as exc:
        raise APIError(service._issues_text(exc), 422, code="fiscal_data_incomplete")
    audit(member.tenant, request.user, "fiscal.retry_requested", row)
    return JsonResponse(service.document_data(row), status=202)


@endpoint
def fiscal_inutilize(request):
    method(request, "POST")
    member = membership_context(request, OWNER, write=True)
    data = payload(request)
    fields(data, ("series", "start", "end", "reason"), ("start", "end", "reason"))
    tenant = member.tenant
    if tenant.fiscal_environment not in ("homologation", "production"):
        raise APIError("Ative a emissão (homologação ou produção) antes de inutilizar números", 409)
    series = data.get("series", tenant.nfce_series)
    for value in (series, data["start"], data["end"]):
        if type(value) is not int:
            raise APIError("Série e números devem ser inteiros")
    row = service.request_inutilization(tenant, tenant.fiscal_environment, series, data["start"], data["end"],
                                        text(data["reason"], "Justificativa", 255))
    audit(tenant, request.user, "fiscal.inutilization_requested", row, {"start": row.start, "end": row.end})
    return JsonResponse({"id": str(row.pk), "status": row.status}, status=202)
