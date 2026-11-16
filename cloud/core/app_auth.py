"""Device-token authentication of the PDV2 Gestão app (/api/v1/app/*).

The app never uses the Django session or CSRF: every call carries
`Authorization: Bearer pa_...`. Tokens are opaque and only their SHA-256 is
stored (same pattern as terminal tokens). Who may use the app is decided by
the SaaS administrator through AppAccess, checked at login, at refresh and on
every request, so a revocation takes effect at once.
"""
import hashlib
import logging
import secrets
import uuid
from datetime import timedelta
from functools import wraps

from django.db.models import Exists, F, OuterRef, Q
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt

from .api_common import PLATFORM_GROUPS, APIError, client_ip, take_rate
from .models import AppAccess, AppDevice, Membership, RateLimit, Shop, Tenant

logger = logging.getLogger(__name__)

ACCESS_TTL = timedelta(minutes=15)
REFRESH_TTL_TENANT = timedelta(days=60)
REFRESH_TTL_PLATFORM = timedelta(days=30)
MFA_CHALLENGE_TTL = timedelta(minutes=10)
REFRESH_REUSE_GRACE = timedelta(seconds=30)
SEEN_EVERY = timedelta(seconds=60)
PENDING_DEVICE_TTL = timedelta(days=1)
MAX_ACTIVE_DEVICES = 10
ALL_PERMISSIONS = list(AppAccess.PERMISSIONS)

# Stable error codes of the app API: (HTTP status, standard message in plain Portuguese).
ERRORS = {
    "invalid_request": (400, "Dados inválidos. Confira e tente de novo."),
    "tenant_required": (400, "Escolha uma empresa."),
    "invalid_period": (400, "Período inválido."),
    "invalid_credentials": (401, "E-mail ou senha incorretos."),
    "unauthenticated": (401, "Entre de novo para continuar."),
    "token_expired": (401, "Sessão expirada."),
    "refresh_invalid": (401, "Sua sessão terminou. Entre de novo."),
    "device_revoked": (401, "Este aparelho foi desconectado da sua conta. Entre de novo."),
    "mfa_invalid": (401, "Código incorreto. Confira o aplicativo autenticador."),
    "mfa_expired": (401, "O tempo para digitar o código acabou. Entre de novo."),
    "app_access_denied": (403, "Acesso ao app não liberado. Peça ao administrador do PDV2 para liberar seu acesso."),
    "mfa_setup_required": (403, "Ative a verificação em duas etapas no painel web do PDV2 antes de usar o app."),
    "tenant_forbidden": (403, "Você não tem acesso a esta empresa."),
    "shop_forbidden": (403, "Você não tem acesso a esta loja."),
    "permission_denied": (403, "Seu acesso não inclui esta área."),
    "platform_forbidden": (403, "Somente a administração do PDV2 pode fazer isso."),
    "not_found": (404, "Registro não encontrado."),
    "method_not_allowed": (405, "Operação não permitida."),
    "access_exists": (409, "Essa pessoa já tem acesso liberado a esta empresa."),
    "member_required": (409, "Essa pessoa não faz parte da equipe dessa empresa. "
                             "Cadastre-a no painel da empresa primeiro."),
    "superuser_implicit": (409, "O administrador principal já tem acesso ao app."),
    "conflict": (409, "Não foi possível concluir. Atualize e tente de novo."),
    "rate_limited": (429, "Muitas tentativas. Aguarde alguns minutos e tente de novo."),
    "mfa_locked": (429, "Muitas tentativas com código errado. Aguarde 15 minutos."),
    "unavailable": (503, "Serviço indisponível. Tente de novo em instantes."),
}
STATUS_CODES = {400: "invalid_request", 401: "unauthenticated", 403: "permission_denied", 404: "not_found",
                405: "method_not_allowed", 409: "conflict", 413: "invalid_request", 429: "rate_limited"}
# Generic helper messages that would read badly in the app are replaced by the standard ones.
STANDARD_MESSAGE_STATUSES = (401, 405, 429)


def fail(code, message=None, **extra):
    status, standard = ERRORS[code]
    raise APIError(message or standard, status, code=code, extra=extra)


def error_response(exc):
    code = exc.code or (STATUS_CODES.get(exc.status) or ("unavailable" if exc.status >= 500 else "invalid_request"))
    message = exc.message
    if code in ERRORS and (not exc.code and exc.status in STANDARD_MESSAGE_STATUSES):
        message = ERRORS[code][1]
    body = {"error": message, **exc.extra, "code": code}
    return JsonResponse(body, status=exc.status)


def app_endpoint(fn):
    """JSON errors with a stable `code`; CSRF exempt because only Bearer tokens are accepted."""
    @wraps(fn)
    def wrapped(request, *args, **kwargs):
        try:
            return fn(request, *args, **kwargs)
        except APIError as exc:
            return error_response(exc)
        except Exception:  # noqa: BLE001 - the app must always receive JSON
            logger.exception("Erro inesperado na API do app")
            return JsonResponse({"error": ERRORS["unavailable"][1], "code": "unavailable"}, status=500)
    return csrf_exempt(wrapped)


def rate_error(key, seconds):
    """429 with the seconds left on the throttle window of `key`."""
    row = RateLimit.objects.filter(key=hashlib.sha256(key.encode()).hexdigest()).first()
    retry = seconds
    if row is not None:
        retry = max(1, int((row.expires_at - timezone.now()).total_seconds()))
    fail("rate_limited", retry_after_seconds=retry)


def app_rate(key, maximum, seconds):
    try:
        return take_rate(key, maximum, seconds)
    except APIError as exc:
        if exc.status == 429:
            rate_error(key, seconds)
        raise


# Tokens ---------------------------------------------------------------------

def token_hash(value):
    return hashlib.sha256(value.encode()).hexdigest()


def new_access():
    return "pa_" + secrets.token_urlsafe(32)


def new_refresh():
    return "pr_" + secrets.token_urlsafe(48)


def new_mfa_challenge():
    return "pm_" + secrets.token_urlsafe(32)


def token_shape(value, prefix):
    return isinstance(value, str) and 20 <= len(value) <= 200 and value.startswith(prefix)


def bearer_token(request):
    header = request.META.get("HTTP_AUTHORIZATION", "")
    if not header.startswith("Bearer "):
        return None
    value = header[7:].strip()
    return value if token_shape(value, "pa_") else None


def refresh_ttl(device):
    return REFRESH_TTL_PLATFORM if device.mfa_verified_at else REFRESH_TTL_TENANT


def issue_tokens(device, now=None, *, rotate_from=None):
    """Give the device a new access/refresh pair and return the TokenPair dict.

    `rotate_from` is the refresh hash being exchanged; it is kept as
    `previous_refresh_hash` to detect a stolen token used twice.
    """
    from .monitoring import local_iso
    now = now or timezone.now()
    access, refresh = new_access(), new_refresh()
    device.access_hash = token_hash(access)
    device.access_expires_at = now + ACCESS_TTL
    device.refresh_hash = token_hash(refresh)
    device.refresh_expires_at = now + refresh_ttl(device)
    fields = ["access_hash", "access_expires_at", "refresh_hash", "refresh_expires_at"]
    if rotate_from is not None:
        device.previous_refresh_hash = rotate_from
        device.refresh_rotated_at = now
        fields += ["previous_refresh_hash", "refresh_rotated_at"]
    if device.activated_at is None:
        device.activated_at = now
        fields.append("activated_at")
    device.save(update_fields=fields)
    return {"access_token": access, "access_expires_at": local_iso(device.access_expires_at),
            "access_expires_in": int(ACCESS_TTL.total_seconds()),
            "refresh_token": refresh, "refresh_expires_at": local_iso(device.refresh_expires_at)}


def revoke_device(device, reason, actor=None):
    """Revoke one device (idempotent). Hashes stay so the next call answers device_revoked."""
    now = timezone.now()
    changed = AppDevice.objects.filter(pk=device.pk, revoked_at__isnull=True).update(
        revoked_at=now, revoked_by=actor, revoke_reason=reason[:240], mfa_challenge_hash=None)
    if changed:
        device.revoked_at, device.revoked_by, device.revoke_reason = now, actor, reason[:240]
    return bool(changed)


def revoke_user_devices(user, reason, actor=None):
    return AppDevice.objects.filter(user=user, revoked_at__isnull=True).update(
        revoked_at=timezone.now(), revoked_by=actor, revoke_reason=reason[:240], mfa_challenge_hash=None)


def enforce_device_limit(device):
    """At most 10 active devices per person: the one unused for longest is disconnected."""
    others = (AppDevice.objects.filter(user=device.user, revoked_at__isnull=True, activated_at__isnull=False)
              .exclude(pk=device.pk).order_by(F("last_seen_at").asc(nulls_first=True), "activated_at"))
    extra = others.count() - (MAX_ACTIVE_DEVICES - 1)
    for old in list(others[:max(extra, 0)]):
        revoke_device(old, "limit")


def cleanup_pending_devices():
    AppDevice.objects.filter(activated_at__isnull=True,
                             created_at__lt=timezone.now() - PENDING_DEVICE_TTL).delete()


# Who may use the app -------------------------------------------------------

def is_platform_account(user):
    return bool(user.is_active and (user.is_superuser or
                                    (user.is_staff and user.groups.filter(name__in=PLATFORM_GROUPS).exists())))


def active_access_q(now=None):
    now = now or timezone.now()
    return Q(revoked_at__isnull=True) & (Q(expires_at__isnull=True) | Q(expires_at__gt=now))


def platform_eligible(user):
    if not user.is_active:
        return False
    if user.is_superuser:
        return True
    return is_platform_account(user) and AppAccess.objects.filter(
        active_access_q(), user=user, scope="platform").exists()


def tenant_accesses(user):
    """Active company accesses whose person is still an active member of an active company."""
    member = Membership.objects.filter(user=user, tenant=OuterRef("tenant"), active=True)
    return (AppAccess.objects.filter(active_access_q(), user=user, scope="tenant", tenant__active=True)
            .filter(Exists(member)).select_related("tenant").order_by("tenant__name", "tenant_id"))


def platform_in_session(device, user):
    """Platform reach requires TOTP done in this sign-in of this device."""
    return device.mfa_verified_at is not None and platform_eligible(user)


def platform_roles(user):
    if user.is_superuser:
        return sorted(PLATFORM_GROUPS)
    return list(user.groups.filter(name__in=PLATFORM_GROUPS).order_by("name").values_list("name", flat=True))


def touch(device, request, now):
    if device.last_seen_at is None or now - device.last_seen_at >= SEEN_EVERY:
        device.last_seen_at, device.last_ip = now, client_ip(request)[:64]
        AppDevice.objects.filter(pk=device.pk).update(last_seen_at=now, last_ip=device.last_ip)


def authenticate_device(request, *, allow_expired=False):
    token = bearer_token(request)
    if token is None:
        fail("unauthenticated")
    device = AppDevice.objects.select_related("user").filter(access_hash=token_hash(token)).first()
    if device is None:
        fail("unauthenticated")
    if device.revoked_at is not None:
        fail("device_revoked")
    if not device.user.is_active:
        revoke_device(device, "user_inactive")
        fail("device_revoked")
    now = timezone.now()
    if not allow_expired and (device.access_expires_at is None or device.access_expires_at <= now):
        fail("token_expired")
    touch(device, request, now)
    return device


class AppContext:
    """What the signed-in person may see in this request."""

    def __init__(self, device, platform):
        self.device = device
        self.user = device.user
        self.platform = platform
        self.accesses = []
        self.tenant = None
        self.access = None
        self.shops = []          # active shops in reach (for pickers and stock)
        self.restricted = False  # True when the access lists specific shops
        self.shop = None         # shop chosen with ?shop_id=
        self.permissions = list(ALL_PERMISSIONS) if platform else []

    @property
    def shop_filter(self):
        """None = whole company; otherwise the shop ids every query is limited to."""
        if self.shop is not None:
            return [self.shop.id]
        if self.restricted:
            return [shop.id for shop in self.shops]
        return None

    @property
    def scope_shop_ids(self):
        return [self.shop.id] if self.shop is not None else [shop.id for shop in self.shops]

    def can(self, permission):
        return permission in self.permissions


def app_context(request, *, tenant_required=True, permission=None):
    device = authenticate_device(request)
    user = device.user
    ctx = AppContext(device, platform_in_session(device, user))
    if not ctx.platform:
        ctx.accesses = list(tenant_accesses(user).prefetch_related("shops"))
        if not ctx.accesses:
            revoke_device(device, "access_ended")
            fail("app_access_denied")
    if not tenant_required:
        return ctx
    header = request.headers.get("X-Tenant-ID", "").strip()
    if not header:
        fail("tenant_required")
    try:
        tenant_id = uuid.UUID(header)
    except ValueError:
        fail("invalid_request", "Empresa inválida.")
    if ctx.platform:
        ctx.tenant = Tenant.objects.filter(pk=tenant_id).first()
        if ctx.tenant is None:
            fail("tenant_forbidden")
        ctx.shops = list(Shop.objects.filter(tenant=ctx.tenant, active=True).order_by("name", "id"))
    else:
        ctx.access = next((row for row in ctx.accesses if row.tenant_id == tenant_id), None)
        if ctx.access is None:
            fail("tenant_forbidden")
        ctx.tenant = ctx.access.tenant
        chosen = sorted((shop for shop in ctx.access.shops.all() if shop.active), key=lambda s: (s.name, str(s.id)))
        ctx.restricted = bool(ctx.access.shops.all())
        ctx.shops = chosen if ctx.restricted else list(
            Shop.objects.filter(tenant=ctx.tenant, active=True).order_by("name", "id"))
        ctx.permissions = [value for value in ALL_PERMISSIONS if value in (ctx.access.permissions or [])]
    raw_shop = request.GET.get("shop_id", "").strip()
    if raw_shop:
        try:
            shop_id = uuid.UUID(raw_shop)
        except ValueError:
            fail("shop_forbidden")
        allowed = {shop.id for shop in ctx.shops}
        shop = Shop.objects.filter(pk=shop_id, tenant=ctx.tenant).first()
        if shop is None or (ctx.restricted and shop.id not in allowed):
            fail("shop_forbidden")
        ctx.shop = shop
    if permission and not ctx.can(permission):
        fail("permission_denied")
    return ctx
