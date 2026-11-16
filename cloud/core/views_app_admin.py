"""Web panel (/platform) and public routes around the PDV2 Gestão app.

Session + CSRF as in the rest of the panel; only the SaaS superuser (with the
platform MFA of the session) releases or revokes app access. Rules and JSON
are the same as the app's platform routes (core.app_access).
"""
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.http import JsonResponse

from .api_common import APIError, client_ip, endpoint, method, payload, platform_user, take_rate
from .app_access import (DELETION_MESSAGE, access_dict, access_dicts, access_options, access_queryset,
                         deletion_dict, filtered_accesses, filtered_deletions, filtered_devices, grant_access,
                         only_keys, platform_device_dict, request_deletion, resolve_deletion, revoke_access,
                         revoke_any_device, update_access)
from .models import AccountDeletionRequest, AppDevice


def _paged(request, rows, serialize_page):
    """Like api_common.page, but the serializer receives the whole page (batched counts)."""
    try:
        limit = int(request.GET.get("limit", "50"))
        offset = int(request.GET.get("cursor", "0") or "0")
    except ValueError:
        raise APIError("Paginação inválida")
    if not 1 <= limit <= 100 or not 0 <= offset <= 10**7:
        raise APIError("Paginação inválida")
    chunk = list(rows[offset:offset + limit + 1])
    return JsonResponse({"results": serialize_page(chunk[:limit]),
                         "next_cursor": str(offset + limit) if len(chunk) > limit else None})


def _access(pk):
    row = access_queryset().filter(pk=pk).first()
    if row is None:
        raise APIError("Acesso não encontrado", 404, code="not_found")
    return row


@endpoint
def app_access(request):
    method(request, "GET", "POST")
    actor = platform_user(request, superuser=True)
    if request.method == "GET":
        return _paged(request, filtered_accesses(request.GET), access_dicts)
    return JsonResponse(access_dict(grant_access(actor, payload(request))), status=201)


@endpoint
def app_access_detail(request, pk):
    method(request, "PATCH")
    actor = platform_user(request, superuser=True)
    return JsonResponse(access_dict(update_access(actor, _access(pk), payload(request))))


@endpoint
def app_access_revoke(request, pk):
    method(request, "POST")
    actor = platform_user(request, superuser=True)
    return JsonResponse(access_dict(revoke_access(actor, _access(pk), payload(request))))


@endpoint
def app_access_options(request):
    method(request, "GET")
    platform_user(request, superuser=True)
    return JsonResponse(access_options(request.GET.get("tenant_id", "").strip()))


@endpoint
def app_devices(request):
    method(request, "GET")
    platform_user(request, superuser=True)
    return _paged(request, filtered_devices(request.GET), lambda chunk: [platform_device_dict(row) for row in chunk])


@endpoint
def app_device_revoke(request, pk):
    method(request, "POST")
    actor = platform_user(request, superuser=True)
    row = AppDevice.objects.select_related("user").filter(pk=pk, activated_at__isnull=False).first()
    if row is None:
        raise APIError("Aparelho não encontrado", 404, code="not_found")
    return JsonResponse(platform_device_dict(revoke_any_device(actor, row, payload(request))))


@endpoint
def account_deletions(request):
    method(request, "GET")
    platform_user(request, superuser=True)
    return _paged(request, filtered_deletions(request.GET), lambda chunk: [deletion_dict(row) for row in chunk])


@endpoint
def account_deletion_resolve(request, pk):
    method(request, "POST")
    actor = platform_user(request, superuser=True)
    row = AccountDeletionRequest.objects.filter(pk=pk).first()
    if row is None:
        raise APIError("Pedido não encontrado", 404, code="not_found")
    return JsonResponse(deletion_dict(resolve_deletion(actor, row, payload(request))))


@endpoint
def public_account_deletion(request):
    """Form of /excluir-conta/. Always the same answer: it never reveals whether the e-mail exists."""
    method(request, "POST")
    ip = client_ip(request)
    take_rate(f"public-delete:{ip}", 5, 3600)
    data = payload(request)
    only_keys(data, ("email", "note"), ("email",))
    email = data["email"].strip().lower() if isinstance(data["email"], str) else ""
    note = data.get("note") or ""
    try:
        validate_email(email)
    except ValidationError:
        raise APIError("Informe um e-mail válido.", 400, code="invalid_request")
    if len(email) > 254 or not isinstance(note, str) or len(note) > 500:
        raise APIError("Dados inválidos. Confira e tente de novo.", 400, code="invalid_request")
    user = get_user_model().objects.filter(email__iexact=email, is_active=True).order_by("pk").first()
    request_deletion(user, email, "web", note.strip(), ip)
    return JsonResponse({"status": "received", "message": DELETION_MESSAGE}, status=202)
