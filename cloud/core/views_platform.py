"""Platform metadata API with explicit support, billing and ops groups."""
from django.contrib.auth import get_user_model, password_validation
from django.contrib.auth.models import Group
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import IntegrityError, transaction
from django.db.models import Count, Q
from django.http import JsonResponse
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from .api_common import APIError, PLATFORM_GROUPS, audit, endpoint, fields, method, money, page, password_policy, payload, platform_user, subscription_window, text, uuid_value
from .models import AuditEvent, BillingInvoice, Membership, Plan, Shop, Subscription, Tenant, Terminal
from .serializers import plan as plan_data, terminal as terminal_data
from integrations.base import ProviderError
from operations.models import BillingBinding, ProviderJob
from operations.services import account


def _platform_staff(row):
    memberships = [{"tenant_id": str(member.tenant_id), "tenant_name": member.tenant.name,
                    "role": member.role, "active": member.active}
                   for member in row.pdv_memberships.all()]
    company_owner = any(member.role == "owner" and member.active for member in row.pdv_memberships.all())
    roles = sorted(PLATFORM_GROUPS) if row.is_superuser else sorted(
        row.groups.filter(name__in=PLATFORM_GROUPS).values_list("name", flat=True))
    return {"id": row.pk, "name": row.get_full_name() or row.username,
            "email": row.email,
            "roles": roles,
            "active": row.is_active,
            "platform_access": row.is_superuser or bool(roles),
            "eligible_for_promotion": row.is_superuser or bool(roles) or company_owner,
            "memberships": memberships,
            "superuser": row.is_superuser,
            "protected": row.is_superuser}


def _staff_values(data, *, creating=False):
    values = {}
    if "name" in data:
        values["name"] = text(data["name"], "Nome", 150)
    if "email" in data:
        email = text(data["email"], "E-mail", 254).lower()
        try:
            validate_email(email)
        except ValidationError as exc:
            raise APIError("E-mail inválido") from exc
        values["email"] = email
    if "password" in data:
        password = data["password"]
        if not password_policy(password):
            raise APIError("Senha deve ter 8 a 128 caracteres, maiúscula, minúscula, número e símbolo")
        try:
            password_validation.validate_password(password)
        except ValidationError as exc:
            raise APIError("Senha não atende à política de segurança") from exc
        values["password"] = password
    if "roles" in data:
        roles = data["roles"]
        if (not isinstance(roles, list) or len(roles) > len(PLATFORM_GROUPS) or
                any(not isinstance(role, str) or role not in PLATFORM_GROUPS for role in roles) or
                len(set(roles)) != len(roles)):
            raise APIError("Papéis de plataforma inválidos")
        if creating and not roles:
            raise APIError("Selecione pelo menos um papel de plataforma")
        values["roles"] = roles
    if "active" in data:
        if type(data["active"]) is not bool:
            raise APIError("Estado inválido")
        values["active"] = data["active"]
    return values


@endpoint
def platform_users(request):
    method(request, "GET", "POST")
    actor = platform_user(request, superuser=True)
    User = get_user_model()
    if request.method == "GET":
        # Include the protected root account so the administrator can see every
        # platform login. It is returned as read-only in the UI and cannot be
        # changed through the staff endpoint.
        rows = (User.objects.filter(Q(is_staff=True) | Q(pdv_memberships__role="owner",
                                                           pdv_memberships__active=True)).distinct()
                .prefetch_related("groups", "pdv_memberships__tenant").order_by("email", "pk"))
        return page(request, rows, _platform_staff)
    data = payload(request)
    fields(data, ("name", "email", "password", "roles", "active"),
           ("name", "email", "password", "roles"))
    values = _staff_values(data, creating=True)
    if User.objects.filter(email__iexact=values["email"]).exists():
        raise APIError("E-mail já cadastrado", 409)
    try:
        with transaction.atomic():
            row = User.objects.create_user(username=values["email"], email=values["email"],
                                           password=values["password"], first_name=values["name"],
                                           is_staff=True, is_active=values.get("active", True))
            row.groups.set(Group.objects.filter(name__in=values["roles"]))
            audit(None, actor, "platform.staff_created", row, {"roles": values["roles"]})
    except IntegrityError as exc:
        raise APIError("E-mail já cadastrado", 409) from exc
    return JsonResponse(_platform_staff(row), status=201)


@endpoint
def platform_user_detail(request, pk):
    method(request, "GET", "PATCH")
    actor = platform_user(request, superuser=True)
    User = get_user_model()
    row = User.objects.filter(pk=pk, is_superuser=False).prefetch_related(
        "groups", "pdv_memberships__tenant").first()
    if row is None:
        protected = User.objects.filter(pk=pk, is_superuser=True).prefetch_related(
            "groups", "pdv_memberships__tenant").first()
        if protected is not None and request.method == "GET":
            return JsonResponse(_platform_staff(protected))
        if protected is not None:
            raise APIError("A conta principal do SaaS é protegida e não pode ser alterada aqui", 409)
    if row is None:
        raise APIError("Usuário de plataforma não encontrado", 404)
    if request.method == "GET":
        return JsonResponse(_platform_staff(row))
    data = payload(request)
    fields(data, ("name", "email", "password", "roles", "active"))
    if not data:
        raise APIError("Informe ao menos um campo")
    values = _staff_values(data)
    if ("roles" in values and values["roles"] and not row.is_staff and
            not Membership.objects.filter(user=row, role="owner", active=True).exists()):
        raise APIError("Somente o login proprietário da empresa pode ser promovido ao SaaS", 403)
    if "email" in values and User.objects.filter(email__iexact=values["email"]).exclude(pk=row.pk).exists():
        raise APIError("E-mail já cadastrado", 409)
    try:
        with transaction.atomic():
            row = User.objects.select_for_update().get(pk=row.pk)
            if "name" in values:
                row.first_name = values["name"]
            if "email" in values:
                row.email = row.username = values["email"]
            if "password" in values:
                row.set_password(values["password"])
            if "active" in values:
                row.is_active = values["active"]
            if "roles" in values:
                row.is_staff = bool(values["roles"])
            row.save()
            if "roles" in values:
                row.groups.set(Group.objects.filter(name__in=values["roles"]))
            audit(None, actor, "platform.staff_updated", row, {"fields": list(values)})
    except IntegrityError as exc:
        raise APIError("E-mail já cadastrado", 409) from exc
    return JsonResponse(_platform_staff(row))


def _tenant(row):
    sub = Subscription.objects.select_related("plan").filter(tenant=row).first()
    return {"id": str(row.id), "name": row.name, "document": row.document,
            "active": row.active, "created_at": row.created_at.isoformat(),
            "subscription": {"id": str(sub.id), "status": sub.status,
                             "license_source": sub.license_source,
                             "plan": plan_data(sub.plan)} if sub else None}


def _subscription(row):
    return {"id": str(row.id), "tenant_id": str(row.tenant_id), "tenant_name": row.tenant.name,
            "status": row.status, "license_source": row.license_source,
            "license_hint": row.license_key.replace("-", "")[-4:],
            "plan": plan_data(row.plan),
            "trial_ends_at": row.trial_ends_at.isoformat() if row.trial_ends_at else None,
            "current_period_end": row.current_period_end.isoformat() if row.current_period_end else None}


@endpoint
def tenants(request):
    method(request, "GET")
    platform_user(request)
    rows = Tenant.objects.all()
    q = request.GET.get("q", "").strip()
    if len(q) > 120:
        raise APIError("Busca muito longa")
    if q:
        rows = rows.filter(name__icontains=q)
    return page(request, rows.order_by("name", "id"), _tenant)


@endpoint
def tenant_detail(request, pk):
    method(request, "GET", "PATCH")
    user = platform_user(request, superuser=request.method == "PATCH")
    row = Tenant.objects.filter(pk=uuid_value(pk)).first()
    if not row:
        raise APIError("Empresa não encontrada", 404)
    if request.method == "GET":
        return JsonResponse(_tenant(row))
    data = payload(request)
    fields(data, ("name", "document", "active"))
    if not data:
        raise APIError("Informe ao menos um campo")
    if "name" in data:
        data["name"] = text(data["name"], "Empresa", 160)
    if "document" in data:
        data["document"] = text(data["document"], "Documento", 30, optional=True)
    if "active" in data and type(data["active"]) is not bool:
        raise APIError("Estado inválido")
    with transaction.atomic():
        for key, value in data.items():
            setattr(row, key, value)
        row.save()
        audit(row, user, "platform.tenant_updated", row, {"fields": list(data)})
    return JsonResponse(_tenant(row))


@endpoint
def platform_plans(request):
    method(request, "GET", "POST")
    user = platform_user(request, roles={"platform_billing"})
    if request.method == "GET":
        return page(request, Plan.objects.all().order_by("price_month_cents", "id"), _plan)
    data = payload(request)
    required = ("slug", "name", "price_month_cents", "price_year_cents", "terminal_limit", "shop_limit", "features")
    fields(data, required + ("active", "offline_days", "max_stations"), required)
    values = _plan_values(data)
    try:
        with transaction.atomic():
            row = Plan.objects.create(**values)
            audit(None, user, "platform.plan_created", row)
    except IntegrityError:
        raise APIError("Plano duplicado", 409)
    return JsonResponse(_plan(row), status=201)


def _plan(row):
    return {**plan_data(row), "offline_days": row.offline_days}


def _plan_values(data):
    values = {}
    for key, value in data.items():
        if key == "slug":
            value = text(value, "Código do plano", 60).lower()
            if not all(c.isalnum() or c == "-" for c in value):
                raise APIError("Código do plano inválido")
        elif key == "name":
            value = text(value, "Nome", 100)
        elif key in ("price_month_cents", "price_year_cents"):
            value = money(value, key)
        elif key in ("terminal_limit", "shop_limit"):
            if type(value) is not int or not 1 <= value <= 1000:
                raise APIError("Limite inválido")
        elif key == "offline_days":
            if type(value) is not int or not 1 <= value <= 45:
                raise APIError("Dias sem internet deve ficar entre 1 e 45")
        elif key == "max_stations":
            if type(value) is not int or not 0 <= value <= 50:
                raise APIError("Caixas adicionais por loja deve ficar entre 0 e 50")
        elif key == "features":
            if not isinstance(value, list) or len(value) > 100 or any(not isinstance(x, str) or len(x) > 80 for x in value):
                raise APIError("Recursos inválidos")
        elif key == "active":
            if type(value) is not bool:
                raise APIError("Estado inválido")
        values[key] = value
    return values


@endpoint
def platform_plan_detail(request, pk):
    method(request, "GET", "PATCH")
    user = platform_user(request, roles={"platform_billing"})
    row = Plan.objects.filter(pk=uuid_value(pk)).first()
    if not row:
        raise APIError("Plano não encontrado", 404)
    if request.method == "GET":
        return JsonResponse(_plan(row))
    data = payload(request)
    fields(data, ("name", "price_month_cents", "price_year_cents", "terminal_limit", "shop_limit", "offline_days", "max_stations", "features", "active"))
    if not data:
        raise APIError("Informe ao menos um campo")
    values = _plan_values(data)
    with transaction.atomic():
        if "terminal_limit" in values and (Terminal.objects.filter(
                tenant__subscription__plan=row, revoked=False).values("tenant_id")
                .annotate(used=Count("id")).filter(used__gt=values["terminal_limit"]).exists()):
            raise APIError("Há empresas acima do novo limite de terminais", 409)
        if "shop_limit" in values and (Shop.objects.filter(
                tenant__subscription__plan=row, active=True).values("tenant_id")
                .annotate(used=Count("id")).filter(used__gt=values["shop_limit"]).exists()):
            raise APIError("Há empresas acima do novo limite de lojas", 409)
        for key, value in values.items():
            setattr(row, key, value)
        row.save()
        audit(None, user, "platform.plan_updated", row, {"fields": list(values)})
    return JsonResponse(_plan(row))


@endpoint
def subscriptions(request):
    method(request, "GET")
    platform_user(request, roles={"platform_billing", "platform_support"})
    rows = Subscription.objects.select_related("tenant", "plan").all()
    if request.GET.get("status"):
        rows = rows.filter(status=request.GET["status"])
    return page(request, rows.order_by("tenant__name", "id"), _subscription)


@endpoint
def subscription_detail(request, pk):
    method(request, "GET", "PATCH")
    user = platform_user(request, roles={"platform_billing"} if request.method == "PATCH" else {"platform_billing", "platform_support"})
    row = Subscription.objects.select_related("tenant", "plan").filter(pk=uuid_value(pk)).first()
    if not row:
        raise APIError("Assinatura não encontrada", 404)
    if request.method == "GET":
        return JsonResponse(_subscription(row))
    data = payload(request)
    fields(data, ("status", "plan_id", "trial_ends_at", "current_period_end"))
    if not data:
        raise APIError("Informe ao menos um campo")
    if "status" in data and data["status"] not in {x[0] for x in Subscription.STATUS}:
        raise APIError("Estado inválido")
    if "plan_id" in data:
        plan = Plan.objects.filter(pk=uuid_value(data["plan_id"]), active=True).first()
        if not plan:
            raise APIError("Plano não encontrado", 404)
        data["plan"] = plan
        del data["plan_id"]
    for key in ("trial_ends_at", "current_period_end"):
        if key in data:
            value = data[key]
            if value is not None:
                value = parse_datetime(value) if isinstance(value, str) else None
                if value is None or timezone.is_naive(value):
                    raise APIError("Data inválida")
            data[key] = value
    with transaction.atomic():
        Tenant.objects.select_for_update().get(pk=row.tenant_id)
        row = Subscription.objects.select_for_update().select_related("tenant", "plan").get(pk=row.pk)
        destination_plan = data.get("plan", row.plan)
        if (Shop.objects.filter(tenant=row.tenant, active=True).count() > destination_plan.shop_limit or
            Terminal.objects.filter(tenant=row.tenant, revoked=False).count() > destination_plan.terminal_limit):
            raise APIError("Uso atual excede limites do novo plano", 409)
        resulting_status = data.get("status", row.status)
        resulting_period = data.get("current_period_end", row.current_period_end)
        if resulting_status in ("active", "past_due") and resulting_period is None:
            raise APIError("Informe o fim do período de cobrança para ativar a assinatura")
        for key, value in data.items():
            setattr(row, key, value)
        # Manual entitlement changes are not evidence of a settled payment.
        # An ordinary downgrade or cancellation also clears a VIP grant.
        if {"plan", "status", "current_period_end"}.intersection(data):
            row.license_source = "trial" if row.status == "trial" else "manual"
        row.save()
        audit(row.tenant, user, "platform.subscription_updated", row, {"fields": list(data)})
    return JsonResponse(_subscription(row))


@endpoint
def subscription_vip(request, pk):
    """Grant a time-bound complimentary plan to a company, with a full audit."""
    method(request, "POST")
    actor = platform_user(request, superuser=True)
    data = payload(request)
    fields(data, ("plan_id", "current_period_end", "reason"),
           ("plan_id", "current_period_end"))
    plan = Plan.objects.filter(pk=uuid_value(data["plan_id"]), active=True).first()
    if plan is None:
        raise APIError("Plano não encontrado", 404)
    end = parse_datetime(data["current_period_end"]) if isinstance(data["current_period_end"], str) else None
    if end is None or timezone.is_naive(end) or end <= timezone.now():
        raise APIError("Informe um fim de vigência futuro para o VIP")
    reason = text(data.get("reason", ""), "Motivo", 240, optional=True)
    subscription_row = Subscription.objects.filter(pk=uuid_value(pk)).first()
    if subscription_row is None:
        raise APIError("Assinatura não encontrada", 404)
    pending_kinds = ("billing.subscribe", "billing.recover", "billing.recover_customer")
    if ProviderJob.objects.filter(tenant_id=subscription_row.tenant_id, kind__in=pending_kinds,
                                  status__in=("queued", "running", "needs_review")).exists():
        raise APIError("Há uma contratação em processamento ou revisão; concilie antes de conceder VIP", 409)
    # A company may become VIP after its recurring charge is paused at Asaas.
    # Verify the provider state instead of trusting an administrative checkbox.
    existing_binding = BillingBinding.objects.filter(tenant_id=subscription_row.tenant_id, active=True).first()
    verified_binding_id = None
    verified_status = None
    if existing_binding:
        try:
            remote = account("asaas").get_subscription(existing_binding.external_id)
        except (ProviderError, ValueError) as exc:
            raise APIError("Não foi possível verificar a assinatura no Asaas; tente novamente", 503) from exc
        if (remote.get("id") != existing_binding.external_id or
                remote.get("customer") != existing_binding.customer_id or
                remote.get("status") not in ("INACTIVE", "EXPIRED")):
            raise APIError("Pause a cobrança recorrente no Asaas e tente novamente", 409)
        verified_binding_id = existing_binding.pk
        verified_status = remote["status"]
    with transaction.atomic():
        Tenant.objects.select_for_update().get(pk=subscription_row.tenant_id)
        row = Subscription.objects.select_for_update().select_related("tenant", "plan").get(pk=subscription_row.pk)
        if ProviderJob.objects.filter(tenant=row.tenant, kind__in=pending_kinds,
                                      status__in=("queued", "running", "needs_review")).exists():
            raise APIError("Há uma contratação em processamento ou revisão; concilie antes de conceder VIP", 409)
        active_bindings = list(BillingBinding.objects.select_for_update().filter(tenant=row.tenant, active=True))
        if active_bindings:
            if len(active_bindings) != 1 or active_bindings[0].pk != verified_binding_id:
                raise APIError("A cobrança mudou; verifique novamente antes de conceder VIP", 409)
            active_bindings[0].active = False
            active_bindings[0].save(update_fields=["active"])
            audit(row.tenant, actor, "platform.billing_paused_verified", active_bindings[0],
                  {"provider_status": verified_status})
        if (Shop.objects.filter(tenant=row.tenant, active=True).count() > plan.shop_limit or
                Terminal.objects.filter(tenant=row.tenant, revoked=False).count() > plan.terminal_limit):
            raise APIError("Uso atual excede limites do plano VIP", 409)
        changed = (row.plan_id != plan.id or row.status != "active" or
                   row.current_period_end != end or row.license_source != "vip")
        if changed:
            row.plan = plan
            row.status = "active"
            row.current_period_end = end
            row.license_source = "vip"
            # An older processor subscription must not revoke the complimentary
            # entitlement when a delayed payment or refund webhook arrives.
            row.external_id = ""
            row.save(update_fields=["plan", "status", "current_period_end",
                                    "license_source", "external_id", "updated_at"])
            audit(row.tenant, actor, "platform.vip_granted", row,
                  {"plan_id": str(plan.id), "current_period_end": end.isoformat(),
                   "reason": reason})
    return JsonResponse(_subscription(row))


@endpoint
def platform_terminals(request):
    method(request, "GET")
    platform_user(request, roles={"platform_support", "platform_ops"})
    rows = Terminal.objects.select_related("tenant", "shop").all()
    if request.GET.get("tenant_id"):
        rows = rows.filter(tenant_id=uuid_value(request.GET["tenant_id"]))
    return page(request, rows.order_by("-created_at", "-id"), lambda row: {
        **terminal_data(row), "tenant_id": str(row.tenant_id), "tenant_name": row.tenant.name,
        "shop_name": row.shop.name})


@endpoint
def platform_terminal_detail(request, pk):
    method(request, "PATCH")
    user = platform_user(request, roles={"platform_ops"})
    row = Terminal.objects.select_related("tenant").filter(pk=uuid_value(pk)).first()
    if not row:
        raise APIError("Terminal não encontrado", 404)
    data = payload(request)
    fields(data, ("revoked", "reason"), ("revoked", "reason"))
    if type(data["revoked"]) is not bool:
        raise APIError("Estado inválido")
    reason = text(data["reason"], "Motivo", 240)
    with transaction.atomic():
        Tenant.objects.select_for_update().get(pk=row.tenant_id)
        row = Terminal.objects.select_for_update().select_related("tenant").get(pk=row.pk)
        if row.revoked and not data["revoked"]:
            sub, _, _ = subscription_window(row.tenant)
            if Terminal.objects.filter(tenant=row.tenant, revoked=False).count() >= sub.plan.terminal_limit:
                raise APIError("Limite de terminais do plano atingido", 409)
        row.revoked = data["revoked"]
        row.save(update_fields=["revoked"])
        audit(row.tenant, user, "platform.terminal_access_changed", row,
              {"revoked": row.revoked, "reason": reason})
    return JsonResponse(terminal_data(row))


@endpoint
def platform_audit(request):
    method(request, "GET")
    platform_user(request, roles={"platform_support", "platform_ops"})
    rows = AuditEvent.objects.select_related("actor").all()
    if request.GET.get("tenant_id"):
        rows = rows.filter(tenant_id=uuid_value(request.GET["tenant_id"]))
    return page(request, rows.order_by("-created_at", "-id"), lambda row: {
        "id": str(row.id), "tenant_id": str(row.tenant_id) if row.tenant_id else None,
        "action": row.action, "subject_type": row.subject_type,
        "subject_id": row.subject_id, "actor_email": row.actor.email if row.actor else None,
        "created_at": row.created_at.isoformat()})


@endpoint
def invoices(request):
    method(request, "GET")
    platform_user(request, roles={"platform_billing"})
    rows = BillingInvoice.objects.select_related("tenant").all()
    return page(request, rows.order_by("-created_at", "-id"), lambda row: {
        "id": str(row.id), "tenant_id": str(row.tenant_id), "tenant_name": row.tenant.name,
        "provider": row.provider, "external_id": row.external_id,
        "amount_cents": row.amount_cents, "due_date": row.due_date.isoformat(),
        "status": row.status, "checkout_url": row.checkout_url})
