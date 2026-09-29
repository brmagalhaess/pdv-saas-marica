"""Tenant membership administration and separate offline operator PINs."""
from django.contrib.auth import get_user_model, password_validation
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import IntegrityError, transaction
from django.db.models import F
from django.http import JsonResponse

from shared.security import hash_pin
from .api_common import APIError, audit, endpoint, fields, membership_context, method, page, password_policy, payload, text, uuid_value
from .models import Membership, Tenant
from .serializers import member as member_data

MANAGE = {"owner", "admin"}
READ = MANAGE | {"manager"}
ROLES = {role for role, _ in Membership.ROLES}


def _member(tenant, pk):
    row = Membership.objects.select_related("user").filter(tenant=tenant, pk=uuid_value(pk)).first()
    if row is None:
        raise APIError("Usuário não encontrado", 404)
    return row


def _last_owner(tenant, row, role=None, active=None):
    resulting_role = row.role if role is None else role
    resulting_active = row.active if active is None else active
    if row.role == "owner" and row.active and (resulting_role != "owner" or not resulting_active):
        if Membership.objects.filter(tenant=tenant, role="owner", active=True).count() <= 1:
            raise APIError("A empresa precisa manter um proprietário ativo", 409)


@endpoint
def users(request):
    method(request, "GET", "POST")
    member = membership_context(request, roles=READ if request.method == "GET" else MANAGE,
                                write=request.method == "POST")
    tenant = member.tenant
    if request.method == "GET":
        rows = Membership.objects.select_related("user").filter(tenant=tenant).order_by("name", "id")
        return page(request, rows, member_data)
    data = payload(request)
    fields(data, ("name", "email", "password", "role", "pin"),
           ("name", "email", "password", "role"))
    name = text(data["name"], "Nome", 150)
    email = text(data["email"], "E-mail", 254).lower()
    try:
        validate_email(email)
    except ValidationError:
        raise APIError("E-mail inválido")
    password = data["password"]
    if not password_policy(password):
        raise APIError("Senha deve ter 8 a 128 caracteres, maiúscula, minúscula, número e símbolo")
    try:
        password_validation.validate_password(password)
    except ValidationError:
        raise APIError("Senha não atende à política de segurança")
    role = data["role"]
    if not isinstance(role, str) or role not in ROLES or (role == "owner" and member.role != "owner"):
        raise APIError("Papel inválido ou sem permissão", 403)
    pin_hash = ""
    if "pin" in data:
        try:
            pin_hash = hash_pin(data["pin"])
        except ValueError as exc:
            raise APIError(str(exc))
    User = get_user_model()
    if User.objects.filter(email__iexact=email).exists() or User.objects.filter(username=email).exists():
        raise APIError("E-mail já cadastrado", 409)
    try:
        with transaction.atomic():
            user = User.objects.create_user(username=email, email=email, password=password, first_name=name)
            row = Membership.objects.create(tenant=tenant, user=user, name=name, role=role,
                                            pin_hash=pin_hash)
            Tenant.objects.filter(pk=tenant.pk).update(roles_version=F("roles_version") + 1)
            audit(tenant, member.user, "membership.created", row)
    except IntegrityError:
        raise APIError("E-mail já cadastrado", 409)
    return JsonResponse(member_data(row), status=201)


@endpoint
def user_detail(request, pk):
    method(request, "GET", "PATCH", "DELETE")
    actor = membership_context(request, roles=READ if request.method == "GET" else MANAGE,
                               write=request.method != "GET")
    row = _member(actor.tenant, pk)
    if request.method == "GET":
        return JsonResponse(member_data(row))
    data = {"active": False} if request.method == "DELETE" else payload(request)
    fields(data, ("name", "role", "active"))
    if not data:
        raise APIError("Informe ao menos um campo")
    if "name" in data:
        data["name"] = text(data["name"], "Nome", 150)
    if "role" in data:
        if not isinstance(data["role"], str) or data["role"] not in ROLES:
            raise APIError("Papel inválido")
        if (row.role == "owner" or data["role"] == "owner") and actor.role != "owner":
            raise APIError("Somente proprietário altera papel de proprietário", 403)
    if "active" in data and type(data["active"]) is not bool:
        raise APIError("Estado inválido")
    with transaction.atomic():
        Tenant.objects.select_for_update().get(pk=actor.tenant_id)
        row = _member(actor.tenant, pk)
        _last_owner(actor.tenant, row, data.get("role"), data.get("active"))
        for key, value in data.items():
            setattr(row, key, value)
        row.save(update_fields=list(data))
        Tenant.objects.filter(pk=actor.tenant_id).update(roles_version=F("roles_version") + 1)
        audit(actor.tenant, actor.user, "membership.updated", row, {"fields": list(data)})
    return JsonResponse(member_data(row))


@endpoint
def user_pin(request, pk):
    method(request, "POST")
    actor = membership_context(request, roles=MANAGE, write=True)
    data = payload(request)
    fields(data, ("pin",), ("pin",))
    try:
        encoded = hash_pin(data["pin"])
    except ValueError as exc:
        raise APIError(str(exc))
    with transaction.atomic():
        Tenant.objects.select_for_update().get(pk=actor.tenant_id)
        row = _member(actor.tenant, pk)
        row.pin_hash = encoded
        row.save(update_fields=["pin_hash"])
        Tenant.objects.filter(pk=actor.tenant_id).update(roles_version=F("roles_version") + 1)
        audit(actor.tenant, actor.user, "membership.pin_changed", row)
    return JsonResponse({"id": str(row.id), "has_pin": True})
