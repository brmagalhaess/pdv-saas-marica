"""Small JSON API helpers with explicit tenant and role checks."""
import hashlib
import json
import re
import uuid
from ipaddress import ip_address
from datetime import timedelta
from functools import wraps

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction
from django.http import JsonResponse
from django.utils import timezone

from .models import AuditEvent, Membership, RateLimit, Subscription, Tenant


class APIError(Exception):
    def __init__(self, message, status=400):
        self.message = message
        self.status = status
        super().__init__(message)


def endpoint(fn):
    @wraps(fn)
    def wrapped(request, *args, **kwargs):
        try:
            return fn(request, *args, **kwargs)
        except APIError as exc:
            return JsonResponse({"error": exc.message}, status=exc.status)
    return wrapped


def csrf_failure(request, reason=""):
    return JsonResponse({"error": "CSRF inválido; atualize a página e tente novamente"}, status=403)


def method(request, *allowed):
    if request.method not in allowed:
        raise APIError("Método não permitido", 405)


def payload(request):
    if not request.content_type.startswith("application/json"):
        raise APIError("Envie JSON")
    if len(request.body) > 1024 * 1024:
        raise APIError("Corpo muito grande", 413)
    try:
        value = json.loads(request.body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise APIError("JSON inválido")
    if not isinstance(value, dict):
        raise APIError("JSON deve ser um objeto")
    return value


def fields(data, allowed, required=()):
    if set(data) - set(allowed) or any(key not in data for key in required):
        raise APIError("Campos inválidos ou ausentes")


def text(value, name, maximum=160, optional=False):
    if value is None and optional:
        return ""
    if not isinstance(value, str):
        raise APIError(f"{name} inválido")
    value = value.strip()
    if (not optional and not value) or len(value) > maximum:
        raise APIError(f"{name} inválido")
    return value


def money(value, name="Valor", allow_zero=True):
    if type(value) is not int or value < (0 if allow_zero else 1) or value > 10**12:
        raise APIError(f"{name} inválido")
    return value


def quantity(value, name="Quantidade", allow_negative=False):
    if type(value) is not int or abs(value) > 10**12 or (value <= 0 if not allow_negative else value == 0):
        raise APIError(f"{name} inválida")
    return value


def product_quantity(product, value, *, allow_negative=False):
    value = quantity(value, allow_negative=allow_negative)
    if product.unit == "UN" and value % 1000:
        raise APIError("Produto UN exige quantidade inteira")
    return value


def uuid_value(value, name="ID"):
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        raise APIError(f"{name} inválido")


def iso(value):
    return value.isoformat() if value is not None else None


def user_dict(user):
    return {"id": user.pk, "name": user.get_full_name() or user.username,
            "email": user.email, "is_staff": user.is_staff,
            "is_superuser": user.is_superuser,
            "platform_roles": sorted(PLATFORM_GROUPS) if user.is_superuser else
                list(user.groups.filter(name__in=PLATFORM_GROUPS).order_by("name").values_list("name", flat=True))}


def require_user(request):
    if not request.user.is_authenticated or not request.user.is_active:
        raise APIError("Não autenticado", 401)
    return request.user


def membership_context(request, roles=None, write=False):
    user = require_user(request)
    header = request.headers.get("X-Tenant-ID", "")
    if not header:
        raise APIError("Informe X-Tenant-ID", 400)
    tenant_id = uuid_value(header, "Empresa")
    member = (Membership.objects.select_related("tenant")
              .filter(user=user, tenant_id=tenant_id, active=True, tenant__active=True).first())
    if not member:
        raise APIError("Acesso à empresa negado", 403)
    if roles and member.role not in roles:
        raise APIError("Sem permissão", 403)
    if write:
        subscription_window(member.tenant)
    return member


def subscription_window(tenant):
    """Return the effective status and hard offline deadline of a paid/trial account.

    The 72-hour paid grace is anchored to the last paid period, so a stale
    `past_due` flag cannot keep renewing terminal grants forever.
    """
    sub = Subscription.objects.select_related("plan").filter(tenant=tenant).first()
    now = timezone.now()
    if sub is None or sub.status not in ("trial", "active", "past_due"):
        raise APIError("Assinatura sem acesso", 403)
    if sub.status == "trial":
        if sub.trial_ends_at is None or sub.trial_ends_at <= now:
            raise APIError("Período de avaliação encerrado", 403)
        return sub, "trial", sub.trial_ends_at + timedelta(hours=24)
    if sub.current_period_end is None:
        raise APIError("Assinatura sem período de cobrança", 403)
    deadline = sub.current_period_end + timedelta(hours=72)
    if deadline <= now:
        raise APIError("Prazo de pagamento encerrado", 403)
    effective = "active" if sub.status == "active" and sub.current_period_end > now else "past_due"
    return sub, effective, deadline


def require_feature(member, feature):
    sub = Subscription.objects.select_related("plan").filter(tenant=member.tenant).first()
    if sub is None or feature not in sub.plan.features:
        raise APIError("Recurso não incluído no plano", 403)


PLATFORM_GROUPS = {"platform_support", "platform_billing", "platform_ops"}


def platform_user(request, roles=None, superuser=False):
    user = require_user(request)
    if user.is_superuser:
        return user
    if superuser or not user.is_staff:
        raise APIError("Sem permissão de plataforma", 403)
    allowed = PLATFORM_GROUPS if roles is None else set(roles)
    if not user.groups.filter(name__in=allowed).exists():
        raise APIError("Sem permissão de plataforma", 403)
    return user


def scoped(model, tenant, pk):
    obj = model.objects.filter(tenant=tenant, pk=uuid_value(pk)).first()
    if obj is None:
        raise APIError("Registro não encontrado", 404)
    return obj


def page(request, queryset, serializer, max_limit=100):
    try:
        limit = int(request.GET.get("limit", "50"))
        offset = int(request.GET.get("cursor", "0"))
    except ValueError:
        raise APIError("Paginação inválida")
    if not 1 <= limit <= max_limit or offset < 0 or offset > 10**7:
        raise APIError("Paginação inválida")
    rows = list(queryset[offset:offset + limit + 1])
    return JsonResponse({"results": [serializer(row) for row in rows[:limit]],
                         "next_cursor": str(offset + limit) if len(rows) > limit else None})


def audit(tenant, actor, action, obj=None, detail=None):
    AuditEvent.objects.create(tenant=tenant, actor=actor, action=action,
                              subject_type=obj.__class__.__name__ if obj is not None else "",
                              subject_id=str(obj.pk) if obj is not None else "", detail=detail or {})


def password_policy(password):
    return (isinstance(password, str) and 8 <= len(password) <= 128 and
            bool(re.search(r"[A-Z]", password)) and bool(re.search(r"[a-z]", password)) and
            bool(re.search(r"[0-9]", password)) and bool(re.search(r"[^A-Za-z0-9]", password)))


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                                   allow_nan=False).encode()).hexdigest()


def take_rate(key, maximum, seconds):
    """Count an attempt in SQL so parallel web workers share one throttle."""
    hashed = hashlib.sha256(key.encode()).hexdigest()
    now = timezone.now()
    with transaction.atomic():
        row, _ = RateLimit.objects.select_for_update().get_or_create(
            key=hashed, defaults={"attempts": 0, "expires_at": now + timedelta(seconds=seconds)})
        if row.expires_at <= now:
            row.attempts = 0
            row.expires_at = now + timedelta(seconds=seconds)
        if row.attempts >= maximum:
            raise APIError("Muitas tentativas; aguarde", 429)
        row.attempts += 1
        row.save(update_fields=["attempts", "expires_at"])
    return hashed


def reset_rate(hashed):
    RateLimit.objects.filter(key=hashed).delete()


def client_ip(request):
    """Use the proxy-appended client address only from an explicitly trusted peer.

    The rightmost valid X-Forwarded-For entry is the address appended by the
    immediately preceding reverse proxy in the supported deployment topology.
    A direct client cannot choose its throttle identity with a forged header.
    """
    raw_remote = request.META.get("REMOTE_ADDR", "")
    try:
        remote = ip_address(raw_remote)
    except ValueError:
        return str(raw_remote)[:64] or "unknown"
    if (not settings.PDV_TRUST_PROXY or
            not any(remote in network for network in settings.PDV_TRUSTED_PROXY_NETWORKS)):
        return str(remote)
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if len(forwarded) > 2048:
        return str(remote)
    for token in reversed(forwarded.split(",")):
        try:
            return str(ip_address(token.strip()))
        except ValueError:
            continue
    return str(remote)
