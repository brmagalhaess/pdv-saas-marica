from datetime import timedelta

from django.contrib.auth import authenticate, get_user_model, login, logout, password_validation
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import IntegrityError, transaction
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import ensure_csrf_cookie
from django.middleware.csrf import get_token

from .api_common import APIError, audit, client_ip, endpoint, fields, method, password_policy, payload, require_user, reset_rate, take_rate, text, user_dict
from .models import Membership, Plan, Shop, Subscription, Tenant


def memberships_for(user):
    return [{"id": str(member.id), "tenant_id": str(member.tenant_id),
             "tenant_name": member.tenant.name, "role": member.role}
            for member in Membership.objects.select_related("tenant").filter(user=user, active=True, tenant__active=True).order_by("tenant__name")]


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
    login(request, user)
    return JsonResponse({"user": user_dict(user), "memberships": memberships_for(user)})


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
