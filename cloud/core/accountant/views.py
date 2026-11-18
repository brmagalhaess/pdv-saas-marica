"""API do pacote do contador (painel: Fiscal > Pacote do contador). Ver docs/PACOTE-CONTADOR.md."""
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import transaction
from django.http import HttpResponse, JsonResponse
from django.utils import timezone

from shared.fiscal.validators import digits, valid_cnpj, valid_cpf, valid_ibge_city

from ..api_common import (APIError, audit, client_ip, endpoint, fields, membership_context, method, payload, take_rate,
                          text, uuid_value)
from . import package as builder
from .models import AccountantDelivery, AccountantPackage, AccountantSettings

READ = ("owner", "admin", "finance")
# Public download link: attempts per client IP per window (shared RateLimit table).
DOWNLOAD_LIMIT = 20
DOWNLOAD_WINDOW_S = 600
WRITE_SETTINGS = ("owner", "admin")
TEXT_FIELDS = (("name", "Nome do contador", 100), ("crc", "CRC", 15), ("address", "Endereço", 60),
               ("number", "Número", 10), ("complement", "Complemento", 60), ("neighborhood", "Bairro", 60),
               ("e116_cod_rec", "Código de receita (E116)", 30))


def _settings_body(tenant):
    config = builder.settings_for(tenant)
    wanted, notice = builder.sped_policy(tenant)
    return {
        "accountant_email": tenant.accountant_email, "name": config.name, "cpf": config.cpf, "crc": config.crc,
        "cnpj": config.cnpj, "cep": config.cep, "address": config.address, "number": config.number,
        "complement": config.complement, "neighborhood": config.neighborhood, "phone": config.phone,
        "city_ibge": config.city_ibge, "auto_send": config.auto_send, "sped_profile": config.sped_profile,
        "sped_for_simples": config.sped_for_simples, "e116_cod_or": config.e116_cod_or,
        "e116_cod_rec": config.e116_cod_rec, "e116_due_day": config.e116_due_day,
        "crt": tenant.crt, "sped_default": tenant.crt == 3, "sped_notice": notice,
        "pva_notice": builder.PVA_NOTE,
    }


@endpoint
def accountant_settings(request):
    method(request, "GET", "PATCH")
    member = membership_context(request, READ if request.method == "GET" else WRITE_SETTINGS,
                                write=request.method == "PATCH")
    tenant = member.tenant
    if request.method == "PATCH":
        data = payload(request)
        allowed = ("accountant_email", "cpf", "cnpj", "cep", "phone", "city_ibge", "auto_send", "sped_profile",
                   "sped_for_simples", "e116_cod_or", "e116_due_day") + tuple(f for f, _, _ in TEXT_FIELDS)
        fields(data, allowed)
        if not data:
            raise APIError("Informe os dados a alterar")
        config = AccountantSettings.objects.filter(tenant=tenant).first() or AccountantSettings(tenant=tenant)
        tenant_fields = []
        if "accountant_email" in data:
            value = text(data["accountant_email"], "E-mail do contador", 254, optional=True).lower()
            if value:
                try:
                    validate_email(value)
                except ValidationError:
                    raise APIError("E-mail do contador inválido")
            tenant.accountant_email = value
            tenant_fields.append("accountant_email")
        for field, label, limit in TEXT_FIELDS:
            if field in data:
                setattr(config, field, text(data[field], label, limit, optional=True))
        checks = (("cpf", "CPF do contador", lambda v: len(v) == 11 and valid_cpf(v)),
                  ("cnpj", "CNPJ do escritório", lambda v: valid_cnpj(v)),
                  ("cep", "CEP", lambda v: len(v) == 8),
                  ("phone", "Telefone (DDD + número)", lambda v: 10 <= len(v) <= 11),
                  ("city_ibge", "Código IBGE do município", lambda v: valid_ibge_city(v)),
                  ("e116_cod_or", "Código da obrigação (E116)", lambda v: len(v) == 3))
        for field, label, valid in checks:
            if field in data:
                value = digits(text(data[field], label, 30, optional=True))
                if value and not valid(value):
                    raise APIError(f"{label} inválido")
                setattr(config, field, value)
        for field in ("auto_send", "sped_for_simples"):
            if field in data:
                if type(data[field]) is not bool:
                    raise APIError("Opção inválida")
                setattr(config, field, data[field])
        if "sped_profile" in data:
            if data["sped_profile"] not in ("A", "B", "C"):
                raise APIError("Perfil do SPED deve ser A, B ou C")
            config.sped_profile = data["sped_profile"]
        if "e116_due_day" in data:
            value = data["e116_due_day"]
            if value is not None and (type(value) is not int or not 1 <= value <= 31):
                raise APIError("Dia de vencimento do ICMS deve ficar entre 1 e 31")
            config.e116_due_day = value
        with transaction.atomic():
            if tenant_fields:
                tenant.save(update_fields=tenant_fields)
            config.save()
            audit(tenant, request.user, "accountant.settings_updated", tenant, {"fields": sorted(data)})
    return JsonResponse(_settings_body(tenant))


def _delivery_body(row):
    return {"id": str(row.id), "to_email": row.to_email, "status": row.status, "trigger": row.trigger,
            "error": row.error, "created_at": row.created_at.isoformat(),
            "sent_at": row.sent_at.isoformat() if row.sent_at else None,
            "link_expires_at": row.link_expires_at.isoformat() if row.link_expires_at else None,
            "downloads": row.downloads}


def _package_body(row, detail=False):
    summary = row.summary or {}
    body = {"id": str(row.id), "year": row.year, "month": row.month, "status": row.status, "trigger": row.trigger,
            "include_sped": row.include_sped, "sped_forced": row.sped_forced, "filename": row.filename,
            "size_bytes": row.size_bytes, "error": row.error, "created_at": row.created_at.isoformat(),
            "ready_at": row.ready_at.isoformat() if row.ready_at else None,
            "expires_at": row.expires_at.isoformat() if row.expires_at else None,
            # ZIP is purged together with status "expired": no need to load the (deferred) bytes here.
            "available": row.status == "ready" and row.size_bytes > 0,
            "sped_available": row.status == "ready" and bool(summary.get("sped", {}).get("generated")),
            "warning_count": summary.get("warning_count", 0),
            "by_status": summary.get("by_status", []), "sped": summary.get("sped", {}),
            "deliveries": [_delivery_body(d) for d in row.deliveries.all().order_by("-created_at")[:10]]}
    if detail:
        body.update({"taxes": summary.get("taxes", {}), "checks": summary.get("checks", {}),
                     "warnings": summary.get("warnings", {}), "gaps": summary.get("gaps", [])})
    return body


@endpoint
def packages(request):
    method(request, "GET", "POST")
    member = membership_context(request, READ, write=request.method == "POST")
    tenant = member.tenant
    if request.method == "GET":
        rows = (AccountantPackage.objects.filter(tenant=tenant).defer("content", "sped_content")
                .order_by("-created_at")[:36])
        return JsonResponse({"results": [_package_body(row) for row in rows],
                             "settings": _settings_body(tenant)})
    from operations.services import enqueue
    data = payload(request)
    fields(data, ("year", "month", "include_sped", "force_sped", "send"), ("year", "month"))
    year, month = data["year"], data["month"]
    if type(year) is not int or type(month) is not int or not 1 <= month <= 12 or not 2020 <= year <= 2100:
        raise APIError("Mês inválido")
    today = timezone.localdate()
    if (year, month) > (today.year, today.month):
        raise APIError("Escolha um mês que já começou")
    for flag in ("include_sped", "force_sped", "send"):
        if flag in data and type(data[flag]) is not bool:
            raise APIError("Opção inválida")
    include_sped = data.get("include_sped", tenant.crt == 3)
    forced = data.get("force_sped", False)
    if include_sped and tenant.crt != 3 and not forced:
        wanted, notice = builder.sped_policy(tenant)
        if not wanted:
            raise APIError(notice + " Para gerar mesmo assim, confirme a opção.", 409, code="sped_simples_confirm")
    if data.get("send") and not tenant.accountant_email:
        raise APIError("Cadastre o e-mail do contador antes de enviar", 409)
    with transaction.atomic():
        row = AccountantPackage.objects.create(tenant=tenant, year=year, month=month, trigger="manual",
                                               requested_by=request.user, include_sped=include_sped,
                                               sped_forced=bool(forced and tenant.crt != 3))
        body = {"package_id": str(row.pk)}
        if data.get("send"):
            delivery = AccountantDelivery.objects.create(tenant=tenant, package=row, trigger="manual",
                                                         to_email=tenant.accountant_email, requested_by=request.user)
            body["delivery_id"] = str(delivery.pk)
        job = enqueue("accountant.package", tenant, f"accountant.package:{row.pk}", body)
        audit(tenant, request.user, "accountant.package_requested", row,
              {"period": f"{year}-{month:02d}", "sped": include_sped, "send": bool(data.get("send"))})
    return JsonResponse({"id": str(row.pk), "job_id": str(job.pk), "status": row.status}, status=202)


def _own_package(request, pk, write=False):
    member = membership_context(request, READ, write=write)
    row = AccountantPackage.objects.filter(pk=uuid_value(pk), tenant=member.tenant).first()
    if not row:
        raise APIError("Pacote não encontrado", 404)
    return member, row


@endpoint
def package_detail(request, pk):
    method(request, "GET")
    _, row = _own_package(request, pk)
    return JsonResponse(_package_body(row, detail=True))


def _file(data, filename, content_type):
    response = HttpResponse(bytes(data), content_type=content_type)
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    response["Cache-Control"] = "private, no-store"
    response["X-Content-Type-Options"] = "nosniff"
    return response


@endpoint
def package_download(request, pk):
    method(request, "GET")
    member, row = _own_package(request, pk)
    if row.status != "ready" or not row.content:
        raise APIError("Pacote indisponível: ainda em geração ou expirado (gere novamente)", 409)
    audit(member.tenant, request.user, "accountant.package_downloaded", row)
    return _file(row.content, row.filename or "pacote-contador.zip", "application/zip")


@endpoint
def package_sped(request, pk):
    method(request, "GET")
    member, row = _own_package(request, pk)
    if row.status != "ready" or not row.sped_content:
        raise APIError("Este pacote não tem SPED disponível", 409)
    audit(member.tenant, request.user, "accountant.sped_downloaded", row)
    cnpj = "".join(c for c in member.tenant.document if c.isalnum())
    return _file(row.sped_content, f"SPED-EFD-ICMS-IPI-{cnpj}-{row.year}{row.month:02d}.txt",
                 "text/plain; charset=iso-8859-1")


@endpoint
def package_send(request, pk):
    method(request, "POST")
    from operations.services import enqueue
    member, row = _own_package(request, pk, write=True)
    tenant = member.tenant
    if not tenant.accountant_email:
        raise APIError("Cadastre o e-mail do contador antes de enviar", 409)
    if row.status != "ready" or not row.content:
        raise APIError("O pacote precisa estar pronto (gere novamente se expirou)", 409)
    with transaction.atomic():
        delivery = AccountantDelivery.objects.create(tenant=tenant, package=row, trigger="manual",
                                                     to_email=tenant.accountant_email, requested_by=request.user)
        job = enqueue("accountant.send", tenant, f"accountant.send:{delivery.pk}", {"delivery_id": str(delivery.pk)})
        audit(tenant, request.user, "accountant.package_send_requested", row, {"to": tenant.accountant_email})
    return JsonResponse({"delivery_id": str(delivery.pk), "job_id": str(job.pk), "status": "queued"}, status=202)


@endpoint
def public_download(request, token=None):
    """Link do e-mail: sem login, válido por 30 dias, um token por envio (guardado só como hash).

    O token vem em ``?t=`` (o log de acesso do gunicorn grava só o caminho); a forma antiga
    ``/download/<token>`` continua aceita. Limite por IP no RateLimit compartilhado.
    """
    method(request, "GET")
    take_rate("accountant-download:" + client_ip(request), DOWNLOAD_LIMIT, DOWNLOAD_WINDOW_S)
    delivery = builder.find_by_token(token or request.GET.get("t", ""))
    if not delivery:
        raise APIError("Link inválido ou expirado. Peça um novo envio à empresa.", 404)
    AccountantDelivery.objects.filter(pk=delivery.pk).update(downloads=delivery.downloads + 1,
                                                            last_download_at=timezone.now())
    audit(delivery.tenant, None, "accountant.package_link_downloaded", delivery.package,
          {"delivery_id": str(delivery.pk)})
    return _file(delivery.package.content, delivery.package.filename or "pacote-contador.zip", "application/zip")
