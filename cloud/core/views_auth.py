from datetime import timedelta
import secrets

import pyotp
from django.conf import settings
from django.contrib.auth import authenticate, get_user_model, login, logout, password_validation
from django.contrib.auth.hashers import check_password, make_password
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import IntegrityError, transaction
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import ensure_csrf_cookie
from django.middleware.csrf import get_token

from .api_common import APIError, audit, client_ip, endpoint, fields, method, password_policy, payload, require_user, reset_rate, take_rate, text, user_dict
from .models import Membership, Plan, PlatformMFA, Shop, Subscription, Tenant
from shared.secrets import decrypt_secret, encrypt_secret


MFA_PENDING_USER = "platform_mfa_pending_user"
MFA_PENDING_AT = "platform_mfa_pending_at"
MFA_VERIFIED = "platform_mfa_verified"
MFA_PENDING_TTL = 10 * 60
PLATFORM_GROUPS = {"platform_support", "platform_billing", "platform_ops"}


def memberships_for(user):
    return [{"id": str(member.id), "tenant_id": str(member.tenant_id),
            "tenant_name": member.tenant.name, "role": member.role}
            for member in Membership.objects.select_related("tenant").filter(user=user, active=True, tenant__active=True).order_by("tenant__name")]


def _is_platform_account(user):
    return bool(user.is_active and (user.is_superuser or
                    (user.is_staff and user.groups.filter(name__in=PLATFORM_GROUPS).exists())))


def _pending_platform_user(request):
    user_id = request.session.get(MFA_PENDING_USER)
    started = request.session.get(MFA_PENDING_AT, 0)
    try:
        expired = timezone.now().timestamp() - float(started) > MFA_PENDING_TTL
    except (TypeError, ValueError):
        expired = True
    if not user_id or expired:
        request.session.pop(MFA_PENDING_USER, None)
        request.session.pop(MFA_PENDING_AT, None)
        raise APIError("A sessão de autenticação multifator expirou. Entre novamente.", 401)
    User = get_user_model()
    user = User.objects.filter(pk=user_id, is_active=True).first()
    if user is None or not _is_platform_account(user):
        request.session.pop(MFA_PENDING_USER, None)
        request.session.pop(MFA_PENDING_AT, None)
        raise APIError("Conta de administração indisponível", 403)
    return user


def _recovery_codes(count=10):
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return ["PDV2-" + "".join(secrets.choice(alphabet) for _ in range(4)) + "-" +
            "".join(secrets.choice(alphabet) for _ in range(4)) for _ in range(count)]


def _finish_platform_login(request, user):
    login(request, user)
    request.session[MFA_VERIFIED] = True
    request.session.pop(MFA_PENDING_USER, None)
    request.session.pop(MFA_PENDING_AT, None)
    return {"user": user_dict(user), "memberships": memberships_for(user)}


@ensure_csrf_cookie
@endpoint
def csrf(request):
    method(request, "GET")
    return JsonResponse({"csrfToken": get_token(request)})


@endpoint
def register(request):
    method(request, "POST")
    take_rate(f"register:{client_ip(request)}", 10, 3600)
    data = payload(request)
    fields(data, ("company", "name", "email", "password"), ("company", "name", "email", "password"))
    company = text(data["company"], "Empresa", 160)
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
    except ValidationError as exc:
        raise APIError("Senha não atende à política de segurança") from exc
    User = get_user_model()
    if User.objects.filter(email__iexact=email).exists() or User.objects.filter(username=email).exists():
        raise APIError("E-mail já cadastrado", 409)
    plan = Plan.objects.filter(slug="essential", active=True).first()
    if plan is None:
        raise APIError("Plano inicial indisponível", 503)
    try:
        with transaction.atomic():
            user = User.objects.create_user(username=email, email=email, password=password, first_name=name)
            tenant = Tenant.objects.create(name=company)
            member = Membership.objects.create(tenant=tenant, user=user, name=name, role="owner")
            Shop.objects.create(tenant=tenant, name="Loja principal")
            Subscription.objects.create(tenant=tenant, plan=plan, status="trial",
                                        trial_ends_at=timezone.now() + timedelta(days=14))
            audit(tenant, user, "tenant.created", tenant)
    except IntegrityError:
        raise APIError("E-mail já cadastrado", 409)
    login(request, user)
    return JsonResponse({"user": user_dict(user),
                         "tenant": {"id": str(tenant.id), "name": tenant.name},
                         "membership": {"id": str(member.id), "role": member.role}}, status=201)


@endpoint
def login_view(request):
    method(request, "POST")
    data = payload(request)
    fields(data, ("email", "password"), ("email", "password"))
    email = text(data["email"], "E-mail", 254).lower()
    password = data["password"]
    if not isinstance(password, str):
        raise APIError("Credenciais inválidas", 401)
    client = client_ip(request)
    key = take_rate(f"login:{client}:{email}", 5, 900)
    user = authenticate(request, username=email, password=password)
    if user is None or not user.is_active:
        raise APIError("Credenciais inválidas", 401)
    reset_rate(key)
    if settings.PLATFORM_MFA_REQUIRED and _is_platform_account(user):
        request.session.cycle_key()
        request.session[MFA_PENDING_USER] = user.pk
        request.session[MFA_PENDING_AT] = int(timezone.now().timestamp())
        request.session.pop(MFA_VERIFIED, None)
        mfa = PlatformMFA.objects.filter(user=user).first()
        if mfa and mfa.enabled:
            return JsonResponse({"mfa_required": True, "user": user_dict(user), "memberships": []})
        return JsonResponse({"mfa_setup_required": True, "user": user_dict(user), "memberships": []})
    login(request, user)
    return JsonResponse({"user": user_dict(user), "memberships": memberships_for(user)})


@endpoint
def mfa_setup(request):
    """Create or return the pending administrator's TOTP provisioning data."""
    method(request, "GET")
    user = _pending_platform_user(request)
    mfa, _ = PlatformMFA.objects.get_or_create(user=user)
    if mfa.enabled:
        raise APIError("A MFA desta conta já está ativa", 409)
    if not mfa.secret_encrypted:
        mfa.secret_encrypted = encrypt_secret(pyotp.random_base32())
        mfa.save(update_fields=["secret_encrypted"])
    secret = decrypt_secret(mfa.secret_encrypted)
    uri = pyotp.TOTP(secret).provisioning_uri(name=user.email, issuer_name="PDV2 SaaS")
    return JsonResponse({"enabled": False, "account": user.email, "issuer": "PDV2 SaaS",
                         "secret": secret, "otpauth_uri": uri})


@endpoint
def mfa_setup_confirm(request):
    method(request, "POST")
    data = payload(request)
    fields(data, ("code",), ("code",))
    user = _pending_platform_user(request)
    code = text(data["code"], "Código MFA", 32)
    mfa = PlatformMFA.objects.filter(user=user).first()
    if mfa is None or not mfa.secret_encrypted or mfa.enabled:
        raise APIError("Inicie a configuração da MFA novamente", 409)
    key = take_rate(f"mfa-setup:{client_ip(request)}:{user.pk}", 5, 900)
    if not pyotp.TOTP(decrypt_secret(mfa.secret_encrypted)).verify(code.replace(" ", ""), valid_window=1):
        raise APIError("Código MFA inválido", 401)
    reset_rate(key)
    recovery = _recovery_codes()
    mfa.enabled = True
    mfa.recovery_codes = [make_password(value) for value in recovery]
    mfa.confirmed_at = timezone.now()
    mfa.failed_attempts = 0
    mfa.locked_until = None
    mfa.save(update_fields=["enabled", "recovery_codes", "confirmed_at", "failed_attempts", "locked_until"])
    result = _finish_platform_login(request, user)
    audit(None, user, "platform.mfa_enabled", user, {"recovery_codes": len(recovery)})
    result["recovery_codes"] = recovery
    return JsonResponse(result)


@endpoint
def mfa_verify(request):
    method(request, "POST")
    data = payload(request)
    fields(data, ("code", "recovery_code"))
    user = _pending_platform_user(request)
    mfa = PlatformMFA.objects.filter(user=user, enabled=True).first()
    if mfa is None:
        raise APIError("Configure a MFA antes de entrar na administração SaaS", 409)
    now = timezone.now()
    if mfa.locked_until and mfa.locked_until > now:
        raise APIError("Muitas tentativas de MFA; aguarde alguns minutos", 429)
    code = text(data.get("code", ""), "Código MFA", 32, optional=True).replace(" ", "")
    recovery_code = text(data.get("recovery_code", ""), "Código de recuperação", 40, optional=True).upper()
    key = take_rate(f"mfa-verify:{client_ip(request)}:{user.pk}", 5, 900)
    valid = bool(code and pyotp.TOTP(decrypt_secret(mfa.secret_encrypted)).verify(code, valid_window=1))
    used_recovery = None
    if not valid and recovery_code:
        for index, hashed in enumerate(mfa.recovery_codes):
            if check_password(recovery_code, hashed):
                valid = True
                used_recovery = index
                break
    if not valid:
        mfa.failed_attempts += 1
        if mfa.failed_attempts >= 5:
            mfa.locked_until = now + timedelta(minutes=15)
        mfa.save(update_fields=["failed_attempts", "locked_until"])
        raise APIError("Código MFA inválido", 401)
    reset_rate(key)
    if used_recovery is not None:
        codes = list(mfa.recovery_codes)
        codes.pop(used_recovery)
        mfa.recovery_codes = codes
    mfa.failed_attempts = 0
    mfa.locked_until = None
    mfa.last_used_at = now
    mfa.save(update_fields=["recovery_codes", "failed_attempts", "locked_until", "last_used_at"])
    result = _finish_platform_login(request, user)
    audit(None, user, "platform.mfa_verified", user, {"recovery_code": used_recovery is not None})
    return JsonResponse(result)


@endpoint
def logout_view(request):
    method(request, "POST")
    require_user(request)
    logout(request)
    return JsonResponse({"ok": True})


@endpoint
def me(request):
    method(request, "GET")
    user = require_user(request)
    if (settings.PLATFORM_MFA_REQUIRED and _is_platform_account(user) and
            not request.session.get(MFA_VERIFIED)):
        logout(request)
        raise APIError("Entre novamente e conclua a autenticação multifator", 401)
    return JsonResponse({"user": user_dict(user), "memberships": memberships_for(user)})


@endpoint
def plans(request):
    method(request, "GET")
    rows = Plan.objects.filter(active=True).order_by("price_month_cents", "slug")
    return JsonResponse({"results": [{"id": str(p.id), "slug": p.slug, "name": p.name,
                                      "price_month_cents": p.price_month_cents,
                                      "price_year_cents": p.price_year_cents,
                                      "terminal_limit": p.terminal_limit,
                                      "shop_limit": p.shop_limit,
                                      "features": p.features} for p in rows], "next_cursor": None})
