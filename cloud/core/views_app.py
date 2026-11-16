"""API of the PDV2 Gestão app (/api/v1/app/*): read-only management for phones.

Contract: docs/APP-GESTAO.md. Only Bearer device tokens are accepted here (no
session, no cookie), so the views are CSRF exempt. Nothing here depends on
the plan or the subscription: the SaaS administrator's release is the only gate.
"""
from collections import defaultdict
from datetime import timedelta

from django.conf import settings
from django.contrib.auth import authenticate
from django.db.models import Count, F, Min, Q, Sum
from django.http import JsonResponse
from django.utils import timezone

from . import monitoring as mon
from .api_common import APIError, audit, client_ip, payload, reset_rate
from .app_access import (DELETION_MESSAGE, access_dict, access_dicts, access_options, access_queryset,
                         filtered_accesses, filtered_devices, grant_access, only_keys, platform_device_dict,
                         request_deletion, revoke_access, revoke_any_device, search_text, shop_ref, tenant_ref,
                         update_access, user_ref, expires_on)
from .app_auth import (ALL_PERMISSIONS, MFA_CHALLENGE_TTL, REFRESH_REUSE_GRACE, app_context, app_endpoint, app_rate,
                       bearer_token, cleanup_pending_devices, enforce_device_limit, fail,
                       issue_tokens, new_mfa_challenge, platform_eligible, platform_in_session, platform_roles,
                       rate_error, revoke_device, tenant_accesses, token_hash, token_shape)
from .cash_logic import session_summary
from .models import (AppDevice, AuditEvent, CashMovement, CashSession, CreditReceipt, PlatformMFA, Sale, SalePayment,
                     SaleReturn, Shop, StockBalance, Subscription, SyncConflict, Tenant, Terminal)
from .views_auth import check_platform_mfa
from .views_terminal import _effective_license_status

PAYMENT_LABELS = {"cash": "Dinheiro", "pix": "Pix", "card_credit": "Cartão de crédito",
                  "card_debit": "Cartão de débito", "credit_account": "Fiado", "store_credit": "Crédito da loja",
                  "card": "Cartão"}
REFUND_LABELS = {"cash": "Dinheiro", "store_credit": "Crédito da loja", "account": "Abatido no fiado",
                 "external": "Fora do PDV"}
MOVEMENT_LABELS = {"withdrawn": "Sangria", "supplied": "Suprimento"}
CONFLICT_STATUS_TEXT = {"open": "Em aberto", "acknowledged": "Em análise", "resolved": "Resolvido",
                        "ignored": "Ignorado"}
# Same texts as web/assets/core.js (conflictReasons).
CONFLICT_REASONS = {
    "rejected_event": "Evento recusado pela nuvem (em revisão)",
    "discount_over_limit": "Desconto acima do limite atual",
    "unit_changed": "Unidade do produto alterada",
    "stock_control_changed": "Controle de estoque alterado",
    "product_inactive": "Produto desativado",
    "payment_method_inactive": "Forma de pagamento desativada",
    "promotion_changed": "Promoção alterada ou encerrada",
    "cash_expected_negative": "Caixa fechado com esperado negativo",
    "cash_closed_twice": "Caixa fechado duas vezes (dois computadores)",
    "void_already_reversed": "Cancelamento no caixa de venda já revertida no painel",
    "void_after_return": "Cancelamento no caixa após devolução",
    "fiscal_cancel_required": "Cancelar a NFC-e da venda estornada no caixa",
    "return_on_reversed_sale": "Devolução no caixa de venda já revertida no painel",
    "customer_inactive": "Fiado recebido de cliente desativado",
    "negative_stock": "Estoque negativo",
    "return_exceeds_sold": "Devolução acima do vendido",
}
SUBSCRIPTION_TEXT = {"trial": "Em teste", "active": "Ativa", "past_due": "Pagamento pendente",
                     "suspended": "Suspensa", "cancelled": "Cancelada", "expired": "Expirada"}
HEALTH_TEXT = {"ok": "Em dia", "attention": "Precisa de atenção", "inactive": "Inativa"}
PLATFORM_VIEW_EVERY = timedelta(minutes=30)


def conflict_reason_text(row):
    message = (row.detail or {}).get("message") if isinstance(row.detail, dict) else None
    return (CONFLICT_REASONS.get(row.reason) or (message.strip() if isinstance(message, str) and message.strip()
                                                 else "") or row.reason or "—")


def payment_label(method, configured=None):
    if method == "other":
        return configured.name if configured is not None else "Outra forma"
    return PAYMENT_LABELS.get(method, "Outra forma")


def require_method(request, *allowed):
    if request.method not in allowed:
        fail("method_not_allowed")


def body(request):
    """JSON object body; an empty body counts as {}."""
    if not request.body:
        return {}
    return payload(request)


def paging(request):
    try:
        limit = int(request.GET.get("limit", "50"))
        offset = int(request.GET.get("cursor", "0") or "0")
    except ValueError:
        fail("invalid_request", "Paginação inválida.")
    if not 1 <= limit <= 100 or not 0 <= offset <= 10**7:
        fail("invalid_request", "Paginação inválida.")
    return limit, offset


def paged(request, rows, serialize_page, **extra):
    """Slice `rows` (queryset or list); `serialize_page` gets the whole page at once."""
    limit, offset = paging(request)
    chunk = list(rows[offset:offset + limit + 1])
    return JsonResponse({**extra, "results": serialize_page(chunk[:limit]),
                         "next_cursor": str(offset + limit) if len(chunk) > limit else None})


def option(request, name, allowed, default):
    value = request.GET.get(name, "") or default
    if value not in allowed:
        fail("invalid_request")
    return value


def links():
    base = settings.PDV_PUBLIC_URL.rstrip("/")
    return {"privacy_url": f"{base}/privacidade/", "support_url": f"{base}/suporte/",
            "account_deletion_url": f"{base}/excluir-conta/"}


def device_dict(device, current_id=None):
    return {"id": str(device.id), "platform": device.platform, "name": device.name, "model": device.model,
            "os_version": device.os_version, "app_version": device.app_version,
            "created_at": mon.local_iso(device.created_at), "last_seen_at": mon.local_iso(device.last_seen_at),
            "current": device.id == current_id}


def all_shops_of(tenant_ids):
    shops = defaultdict(list)
    for shop in Shop.objects.filter(tenant_id__in=tenant_ids, active=True).order_by("name", "id"):
        shops[shop.tenant_id].append(shop)
    return shops


def tenant_access_dict(access, active_shops):
    chosen = sorted((shop for shop in access.shops.all() if shop.active), key=lambda s: (s.name, str(s.id)))
    restricted = bool(access.shops.all())
    return {"id": str(access.tenant_id), "name": access.tenant.name, "all_shops": not restricted,
            "shops": [shop_ref(shop) for shop in (chosen if restricted else active_shops.get(access.tenant_id, []))],
            "permissions": [value for value in ALL_PERMISSIONS if value in (access.permissions or [])],
            "access_expires_on": expires_on(access.expires_at)}


def platform_tenant_access(tenant, active_shops):
    return {"id": str(tenant.id), "name": tenant.name, "all_shops": True,
            "shops": [shop_ref(shop) for shop in active_shops.get(tenant.id, [])],
            "permissions": list(ALL_PERMISSIONS), "access_expires_on": None}


def me_dict(device):
    user = device.user
    accesses = list(tenant_accesses(user).prefetch_related("shops"))
    shops = all_shops_of([access.tenant_id for access in accesses])
    platform = None
    if platform_in_session(device, user):
        platform = {"enabled": True, "superuser": user.is_superuser, "can_manage_access": user.is_superuser,
                    "roles": platform_roles(user)}
    return {"user": user_ref(user), "device": device_dict(device, device.id), "platform": platform,
            "tenants": [tenant_access_dict(access, shops) for access in accesses],
            "links": links(), "server_time": mon.local_iso(timezone.now())}


def signed_in(device, tokens):
    return JsonResponse({"status": "ok", "tokens": tokens, "me": me_dict(device)})


# Authentication --------------------------------------------------------------

def _device_fields(value):
    if not isinstance(value, dict):
        fail("invalid_request", "Dados do aparelho inválidos.")
    only_keys(value, ("platform", "name", "model", "os_version", "app_version"), ("platform", "name", "app_version"))

    def field(key, maximum, required):
        item = value.get(key, "")
        if item is None and not required:
            item = ""
        if not isinstance(item, str) or len(item.strip()) > maximum or (required and not item.strip()):
            fail("invalid_request", "Dados do aparelho inválidos.")
        return item.strip()
    if value["platform"] not in ("ios", "android"):
        fail("invalid_request", "Dados do aparelho inválidos.")
    return {"platform": value["platform"], "name": field("name", 80, True), "model": field("model", 60, False),
            "os_version": field("os_version", 40, False), "app_version": field("app_version", 40, True)}


def _activate(device, request):
    """First token pair of a device: count it against the per-person limit and audit the sign-in."""
    tokens = issue_tokens(device)
    enforce_device_limit(device)
    AppDevice.objects.filter(pk=device.pk).update(last_seen_at=timezone.now(), last_ip=client_ip(request)[:64])
    device.refresh_from_db()
    audit(None, device.user, "app.login", device, {"platform": device.platform,
                                                    "mfa": device.mfa_verified_at is not None})
    return tokens


@app_endpoint
def login(request):
    require_method(request, "POST")
    data = payload(request)
    only_keys(data, ("email", "password", "device"), ("email", "password", "device"))
    if not isinstance(data["email"], str) or not data["email"].strip() or len(data["email"]) > 254:
        fail("invalid_request", "Informe o e-mail.")
    if not isinstance(data["password"], str) or not data["password"] or len(data["password"]) > 128:
        fail("invalid_request", "Informe a senha.")
    info = _device_fields(data["device"])
    email = data["email"].strip().lower()
    ip = client_ip(request)
    app_rate(f"app-login-ip:{ip}", 30, 900)
    key = app_rate(f"app-login:{ip}:{email}", 5, 900)
    user = authenticate(None, username=email, password=data["password"])
    if user is None or not user.is_active:
        fail("invalid_credentials")
    reset_rate(key)
    cleanup_pending_devices()
    if platform_eligible(user):
        mfa = PlatformMFA.objects.filter(user=user, enabled=True).first()
        if mfa is None:
            fail("mfa_setup_required")
        challenge = new_mfa_challenge()
        expires = timezone.now() + MFA_CHALLENGE_TTL
        AppDevice.objects.create(user=user, mfa_challenge_hash=token_hash(challenge),
                                 mfa_challenge_expires_at=expires, **info)
        return JsonResponse({"status": "mfa_required", "mfa_token": challenge,
                             "mfa_expires_at": mon.local_iso(expires),
                             "user": {"name": user.get_full_name() or user.username, "email": user.email}})
    if not tenant_accesses(user).exists():
        audit(None, user, "app.login_denied", user, {"reason": "no_access"})
        fail("app_access_denied")
    device = AppDevice.objects.create(user=user, **info)
    return signed_in(device, _activate(device, request))


@app_endpoint
def mfa(request):
    require_method(request, "POST")
    data = payload(request)
    only_keys(data, ("mfa_token", "code", "recovery_code"), ("mfa_token",))
    if ("code" in data) == ("recovery_code" in data):
        fail("invalid_request", "Informe o código do autenticador ou um código de recuperação.")
    if not token_shape(data["mfa_token"], "pm_"):
        fail("mfa_expired")
    device = AppDevice.objects.select_related("user").filter(mfa_challenge_hash=token_hash(data["mfa_token"])).first()
    now = timezone.now()
    if (device is None or device.revoked_at is not None or device.mfa_challenge_expires_at is None or
            device.mfa_challenge_expires_at <= now):
        fail("mfa_expired")
    user = device.user
    if not platform_eligible(user):
        device.delete()
        fail("app_access_denied")
    raw = data.get("code", data.get("recovery_code"))
    if not isinstance(raw, str) or len(raw) > 40:
        fail("invalid_request", "Código inválido.")
    code = raw.replace(" ", "") if "code" in data else ""
    recovery = raw.strip().upper() if "recovery_code" in data else ""
    rate_key = f"app-mfa:{client_ip(request)}:{user.pk}"
    try:
        used_recovery = check_platform_mfa(user, code, recovery, rate_key)
    except APIError as exc:
        if exc.code in ("mfa_invalid", "mfa_locked", "mfa_setup_required"):
            fail(exc.code)
        if exc.status == 429:
            rate_error(rate_key, 900)
        raise
    device.mfa_verified_at, device.mfa_challenge_hash, device.mfa_challenge_expires_at = now, None, None
    device.save(update_fields=["mfa_verified_at", "mfa_challenge_hash", "mfa_challenge_expires_at"])
    audit(None, user, "app.mfa_verified", device, {"recovery_code": used_recovery})
    return signed_in(device, _activate(device, request))


@app_endpoint
def refresh(request):
    require_method(request, "POST")
    data = payload(request)
    only_keys(data, ("refresh_token",), ("refresh_token",))
    app_rate(f"app-refresh:{client_ip(request)}", 120, 900)
    if not token_shape(data["refresh_token"], "pr_"):
        fail("refresh_invalid")
    hashed = token_hash(data["refresh_token"])
    now = timezone.now()
    device = AppDevice.objects.select_related("user").filter(refresh_hash=hashed).first()
    reused = None
    if device is None:
        reused = AppDevice.objects.select_related("user").filter(previous_refresh_hash=hashed).first()
        if reused is None:
            fail("refresh_invalid")
        device = reused
    if device.revoked_at is not None:
        fail("device_revoked")
    if not device.user.is_active:
        revoke_device(device, "user_inactive")
        fail("device_revoked")
    if reused is not None and not (device.refresh_rotated_at and
                                   now - device.refresh_rotated_at <= REFRESH_REUSE_GRACE):
        revoke_device(device, "refresh_reuse")
        audit(None, device.user, "app.refresh_reuse_detected", device, {})
        fail("device_revoked")
    if device.refresh_expires_at is None or device.refresh_expires_at <= now:
        fail("refresh_invalid")
    if not platform_in_session(device, device.user) and not tenant_accesses(device.user).exists():
        revoke_device(device, "access_ended")
        fail("app_access_denied")
    if reused is None:
        changed = AppDevice.objects.filter(pk=device.pk, refresh_hash=hashed).update(refresh_hash=None)
        rotate_from = hashed
    else:
        # The answer of the previous refresh may have been lost (grace of 30 s). The pair being
        # replaced (R2) becomes the "previous" one: if whoever holds it uses it later, that is a
        # reuse and the device is revoked. Without this, a stolen R1 used in the grace window
        # would silently take the session while the real app only got "refresh_invalid".
        current = device.refresh_hash
        changed = (current is not None and
                   AppDevice.objects.filter(pk=device.pk, refresh_hash=current, previous_refresh_hash=hashed)
                   .update(refresh_hash=None))
        rotate_from = current
    if not changed:
        fail("refresh_invalid")
    return JsonResponse({"tokens": issue_tokens(device, now, rotate_from=rotate_from)})


@app_endpoint
def logout(request):
    require_method(request, "POST")
    data = body(request)
    only_keys(data, ("refresh_token",))
    device = None
    token = bearer_token(request)
    if token:
        device = AppDevice.objects.select_related("user").filter(access_hash=token_hash(token)).first()
    value = data.get("refresh_token")
    if device is None and token_shape(value, "pr_"):
        hashed = token_hash(value)
        device = AppDevice.objects.select_related("user").filter(
            Q(refresh_hash=hashed) | Q(previous_refresh_hash=hashed)).first()
    if device is not None and revoke_device(device, "logout", device.user):
        audit(None, device.user, "app.logout", device, {})
    return JsonResponse({"ok": True})


@app_endpoint
def me(request):
    require_method(request, "GET")
    ctx = app_context(request, tenant_required=False)
    return JsonResponse(me_dict(ctx.device))


@app_endpoint
def tenants(request):
    require_method(request, "GET")
    ctx = app_context(request, tenant_required=False)
    if ctx.platform:
        rows = Tenant.objects.all()
        q = search_text(request.GET)
        if q:
            rows = rows.filter(name__icontains=q)

        def serialize(chunk):
            shops = all_shops_of([row.id for row in chunk])
            return [platform_tenant_access(row, shops) for row in chunk]
        return paged(request, rows.order_by("name", "id"), serialize)
    q = search_text(request.GET).lower()
    accesses = [row for row in ctx.accesses if not q or q in row.tenant.name.lower()]

    def serialize(chunk):
        shops = all_shops_of([row.tenant_id for row in chunk])
        return [tenant_access_dict(row, shops) for row in chunk]
    return paged(request, accesses, serialize)


# Management data ---------------------------------------------------------------

@app_endpoint
def dashboard(request):
    require_method(request, "GET")
    ctx = app_context(request)
    window = mon.period_window(request.GET.get("period"))
    start, end, shops, tenant = window["start"], window["end"], ctx.shop_filter, ctx.tenant
    result = {"generated_at": mon.local_iso(timezone.now()), "tenant": tenant_ref(tenant),
              "shop": shop_ref(ctx.shop) if ctx.shop else None, "shops": [shop_ref(shop) for shop in ctx.shops],
              "period": mon.period_dict(window), "permissions": ctx.permissions,
              "sales": None, "payments": None, "series": None, "cash": None, "stock": None, "credit": None,
              "terminals": None}
    if ctx.can("sales"):
        result["sales"] = mon.sales_section(tenant, shops, start, end)
        result["payments"] = mon.payments_section(tenant, shops, start, end)
        result["series"] = mon.series_section(tenant, shops, window)
    if ctx.can("cash"):
        result["cash"] = mon.cash_section(tenant, shops, start, end)
    if ctx.can("stock"):
        result["stock"] = mon.stock_section(tenant, ctx.scope_shop_ids)
    if ctx.can("credit"):
        result["credit"] = mon.credit_section(tenant, shops)
    if ctx.can("terminals"):
        result["terminals"] = mon.terminals_section(tenant, shops)
    if ctx.platform and not AuditEvent.objects.filter(
            tenant=tenant, actor=ctx.user, action="app.platform_tenant_viewed",
            created_at__gte=timezone.now() - PLATFORM_VIEW_EVERY).exists():
        audit(tenant, ctx.user, "app.platform_tenant_viewed", tenant, {"device_id": str(ctx.device.id)})
    return JsonResponse(result)


def _sale_base(row):
    return {"id": str(row.id), "created_at": mon.local_iso(row.created_at), "shop": shop_ref(row.shop),
            "terminal_name": row.terminal.name if row.terminal else "", "station_name": row.station_name,
            "operator_name": row.operator_name_snapshot, "customer_name": row.customer_name_snapshot,
            "total_cents": row.total_cents, "status": row.status}


@app_endpoint
def sales(request):
    require_method(request, "GET")
    ctx = app_context(request, permission="sales")
    window = mon.period_window(request.GET.get("period"))
    status = option(request, "status", ("all", "completed", "cancelled"), "all")
    rows = mon.by_shop(Sale.objects.filter(tenant=ctx.tenant, created_at__gte=window["start"],
                                           created_at__lt=window["end"]), ctx.shop_filter)
    if status == "completed":
        rows = rows.filter(status="completed")
    elif status == "cancelled":
        rows = rows.filter(status__in=("cancelled", "refunded"))
    rows = rows.select_related("shop", "terminal").order_by("-created_at", "-id")

    def serialize(chunk):
        ids = [row.id for row in chunk]
        items = dict(Sale.objects.filter(pk__in=ids).values_list("id").annotate(v=Count("items")))
        methods = defaultdict(list)
        for sale_id, method_name in (SalePayment.objects.filter(sale_id__in=ids)
                                     .order_by("sale_id", "line_number").values_list("sale_id", "method")):
            if method_name not in methods[sale_id]:
                methods[sale_id].append(method_name)
        return [{**_sale_base(row), "items_count": items.get(row.id, 0), "payment_methods": methods[row.id]}
                for row in chunk]
    return paged(request, rows, serialize)


def _scoped_object(ctx, queryset, pk):
    row = mon.by_shop(queryset.filter(tenant=ctx.tenant, pk=pk), ctx.shop_filter).first()
    if row is None:
        fail("not_found")
    return row


@app_endpoint
def sale_detail(request, pk):
    require_method(request, "GET")
    ctx = app_context(request, permission="sales")
    row = _scoped_object(ctx, Sale.objects.select_related("shop", "terminal"), pk)
    payments = row.payments.select_related("payment_method").order_by("line_number")
    return JsonResponse({
        **{key: value for key, value in _sale_base(row).items() if key != "total_cents" and key != "status"},
        "received_at": mon.local_iso(row.received_at), "status": row.status,
        "seller_name": row.seller_name_snapshot, "gross_cents": row.gross_cents,
        "promo_discount_cents": row.promo_discount_cents, "discount_cents": row.discount_cents,
        "total_cents": row.total_cents, "change_cents": row.change_cents,
        "items": [{"line": item.line_number, "name": item.name, "unit": item.unit,
                   "quantity_milli": item.quantity_milli, "unit_price_cents": item.unit_price_cents,
                   "gross_cents": item.gross_cents, "promo_discount_cents": item.promo_discount_cents,
                   "discount_cents": item.discount_cents, "line_total_cents": item.line_total_cents}
                  for item in row.items.order_by("line_number")],
        "payments": [{"method": payment.method, "label": payment_label(payment.method, payment.payment_method),
                      "amount_cents": payment.amount_cents} for payment in payments],
        "returns": [{"id": str(item.id), "created_at": mon.local_iso(item.created_at), "total_cents": item.total_cents,
                     "refund_method": item.refund_method,
                     "refund_label": REFUND_LABELS.get(item.refund_method, item.refund_method), "reason": item.reason}
                    for item in row.returns.order_by("created_at", "id")],
        "cancelled_at": mon.local_iso(row.cancelled_at), "cancellation_reason": row.cancellation_reason})


def _member_name(member):
    return member.name if member is not None else ""


def session_totals(sessions):
    """Sales, cash and movements of a page of shifts in a few aggregate queries.

    Sales point to their shift by `cash_session_id` written as str(uuid) or uuid.hex.
    """
    keys = {}
    for row in sessions:
        keys[str(row.id)] = keys[row.id.hex] = row
    totals = {row.id: {"sales_count": 0, "sales_total_cents": 0, "cash": 0, "change": 0, "refunds": 0,
                       "supplied_cents": 0, "withdrawn_cents": 0} for row in sessions}
    if not sessions:
        return totals
    tenant_ids = {row.tenant_id for row in sessions}
    completed = Sale.objects.filter(tenant_id__in=tenant_ids, cash_session_id__in=list(keys), status="completed")
    for key, terminal_id, count, total, change in (completed.values_list("cash_session_id", "terminal_id")
                                                   .annotate(c=Count("id"), t=Sum("total_cents"),
                                                             ch=Sum("change_cents"))):
        session = keys[key]
        if terminal_id == session.terminal_id:
            bucket = totals[session.id]
            bucket["sales_count"] += count
            bucket["sales_total_cents"] += total or 0
            bucket["change"] += change or 0
    for key, terminal_id, amount in (SalePayment.objects.filter(sale__in=completed, method="cash")
                                     .values_list("sale__cash_session_id", "sale__terminal_id")
                                     .annotate(v=Sum("amount_cents"))):
        session = keys[key]
        if terminal_id == session.terminal_id:
            totals[session.id]["cash"] += amount or 0
    for key, amount in (SaleReturn.objects.filter(tenant_id__in=tenant_ids, refund_method="cash",
                                                  cash_session_id__in=list(keys))
                        .values_list("cash_session_id").annotate(v=Sum("total_cents"))):
        totals[keys[key].id]["refunds"] += amount or 0
    for session_id, kind, amount in (CashMovement.objects
                                     .filter(session_id__in=[row.id for row in sessions])
                                     .values_list("session_id", "kind").annotate(v=Sum("amount_cents"))):
        totals[session_id][kind + "_cents"] += amount or 0
    return totals


def _session_dict(row, total):
    return {"id": str(row.id), "shop": shop_ref(row.shop), "terminal_name": row.terminal.name,
            "station_name": row.station_name, "operator_name": _member_name(row.operator),
            "status": "open" if row.closed_at is None else "closed", "opened_at": mon.local_iso(row.opened_at),
            "closed_at": mon.local_iso(row.closed_at), "opening_cents": row.opening_cents,
            "sales_count": total["sales_count"], "sales_total_cents": total["sales_total_cents"],
            "cash_net_cents": total["cash"] - total["change"] - total["refunds"],
            "supplied_cents": total["supplied_cents"], "withdrawn_cents": total["withdrawn_cents"],
            "expected_cents": row.expected_cents, "counted_cents": row.counted_cents,
            "difference_cents": row.difference_cents, "incomplete": row.incomplete}


SESSION_RELATED = ("shop", "terminal", "operator", "closed_by", "supervisor")


@app_endpoint
def cash_sessions(request):
    require_method(request, "GET")
    ctx = app_context(request, permission="cash")
    status = option(request, "status", ("open", "closed"), "open")
    rows = mon.by_shop(CashSession.objects.filter(tenant=ctx.tenant), ctx.shop_filter).select_related(*SESSION_RELATED)
    if status == "open":
        rows = rows.filter(closed_at__isnull=True).order_by("-opened_at", "-id")
    else:
        window = mon.period_window(request.GET.get("period"))
        rows = rows.filter(closed_at__gte=window["start"], closed_at__lt=window["end"]).order_by("-closed_at", "-id")

    def serialize(chunk):
        totals = session_totals(chunk)
        return [_session_dict(row, totals[row.id]) for row in chunk]
    return paged(request, rows, serialize)


@app_endpoint
def cash_session_detail(request, pk):
    require_method(request, "GET")
    ctx = app_context(request, permission="cash")
    row = _scoped_object(ctx, CashSession.objects.select_related(*SESSION_RELATED), pk)
    result = _session_dict(row, session_totals([row])[row.id])
    summary = session_summary(row)
    order = ["cash", "pix", "card_credit", "card_debit", "credit_account", "store_credit", "card", "other"]
    by_method = sorted(summary["by_method"].items(), key=lambda item: (order.index(item[0]) if item[0] in order
                                                                          else len(order), item[0]))
    keys = (str(row.id), row.id.hex)
    credit = CreditReceipt.objects.filter(tenant=row.tenant, terminal=row.terminal, cash_session_id__in=keys)
    result.update({
        "closed_by_name": _member_name(row.closed_by), "supervisor_name": _member_name(row.supervisor),
        "credit_received_cents": credit.aggregate(v=Sum("amount_cents"))["v"] or 0,
        "by_method": [{"method": key, "label": payment_label(key), "amount_cents": value} for key, value in by_method],
        "movements": [{"id": str(item.id), "kind": item.kind, "label": MOVEMENT_LABELS.get(item.kind, item.kind),
                       "amount_cents": item.amount_cents, "reason": item.reason,
                       "operator_name": _member_name(item.operator), "created_at": mon.local_iso(item.created_at)}
                      for item in row.movements.select_related("operator").order_by("created_at", "id")]})
    return JsonResponse(result)


def stock_alert_rows(tenant, shop_ids):
    """Every (shop, product) pair that is zeroed or below its minimum."""
    shops = {shop.id: shop for shop in Shop.objects.filter(pk__in=shop_ids)}
    products = mon.stock_products(tenant)
    rows = []
    balances = (StockBalance.objects.filter(tenant=tenant, shop_id__in=shop_ids, product__in=products)
                .filter(Q(quantity_milli__lte=0) |
                        Q(product__stock_minimum_milli__gt=0, quantity_milli__lt=F("product__stock_minimum_milli")))
                .select_related("product"))
    for balance in balances:
        rows.append((shops[balance.shop_id], balance.product, balance.quantity_milli))
    # Products with a minimum that never had a balance row in the shop count as zeroed.
    for shop_id, shop in shops.items():
        missing = products.filter(stock_minimum_milli__gt=0).exclude(balances__shop_id=shop_id)
        rows.extend((shop, product, 0) for product in missing)
    result = []
    for shop, product, quantity in rows:
        status = "zeroed" if quantity <= 0 else "below_minimum"
        result.append({"shop": shop_ref(shop), "product_id": str(product.id), "name": product.name,
                       "sku": product.sku, "unit": product.unit, "quantity_milli": quantity,
                       "minimum_milli": product.stock_minimum_milli, "status": status})
    result.sort(key=lambda row: (row["status"] != "zeroed", row["name"].lower(), row["shop"]["name"].lower(),
                                 row["product_id"], row["shop"]["id"]))
    return result


@app_endpoint
def stock_alerts(request):
    require_method(request, "GET")
    ctx = app_context(request, permission="stock")
    status = option(request, "status", ("all", "zeroed", "below_minimum"), "all")
    rows = stock_alert_rows(ctx.tenant, ctx.scope_shop_ids)
    counts = {"zeroed": sum(row["status"] == "zeroed" for row in rows),
              "below_minimum": sum(row["status"] == "below_minimum" for row in rows)}
    if status != "all":
        rows = [row for row in rows if row["status"] == status]
    return paged(request, rows, lambda chunk: chunk, counts=counts)


@app_endpoint
def credit(request):
    require_method(request, "GET")
    ctx = app_context(request, permission="credit")
    status = option(request, "status", ("open", "overdue"), "open")
    today = timezone.localdate()
    entries = mon.credit_entries(ctx.tenant, ctx.shop_filter).filter(customer__isnull=False)
    grouped = (entries.values("customer_id", "customer__name", "customer__phone")
               .annotate(open_cents=Sum("amount_cents"),
                         overdue_cents=Sum("amount_cents", filter=Q(due_date__lt=today)),
                         oldest=Min("due_date"), entries_count=Count("id")))
    totals = entries.aggregate(open=Sum("amount_cents"), overdue=Sum("amount_cents", filter=Q(due_date__lt=today)))
    summary = {"open_cents": totals["open"] or 0, "overdue_cents": totals["overdue"] or 0,
               "customers": entries.values("customer_id").distinct().count(),
               "overdue_customers": entries.filter(due_date__lt=today).values("customer_id").distinct().count()}
    if status == "overdue":
        grouped = grouped.filter(overdue_cents__gt=0)
    grouped = grouped.order_by(F("overdue_cents").desc(nulls_last=True), "-open_cents", "customer__name",
                               "customer_id")
    return paged(request, grouped, lambda chunk: [
        {"customer_id": str(row["customer_id"]), "name": row["customer__name"], "phone": row["customer__phone"],
         "open_cents": row["open_cents"] or 0, "overdue_cents": row["overdue_cents"] or 0,
         "oldest_due_date": row["oldest"].isoformat() if row["oldest"] else None,
         "entries_count": row["entries_count"]} for row in chunk], totals=summary)


@app_endpoint
def terminals(request):
    require_method(request, "GET")
    ctx = app_context(request, permission="terminals")
    online_after = timezone.now() - mon.ONLINE_WINDOW
    rows = (mon.by_shop(Terminal.objects.filter(tenant=ctx.tenant, revoked=False), ctx.shop_filter)
            .select_related("shop").order_by("shop__name", "name", "id"))
    return JsonResponse({"online_window_seconds": int(mon.ONLINE_WINDOW.total_seconds()), "results": [
        {"id": str(row.id), "name": row.name, "shop": shop_ref(row.shop),
         "status": mon.terminal_status(row, online_after), "version": row.version,
         "last_seen_at": mon.local_iso(row.last_seen_at), "pending_events": row.heartbeat_pending_events,
         "error": row.heartbeat_error} for row in rows], "next_cursor": None})


@app_endpoint
def conflicts(request):
    require_method(request, "GET")
    ctx = app_context(request, permission="terminals")
    status = option(request, "status", ("open", "all"), "open")
    rows = mon.by_shop(SyncConflict.objects.filter(tenant=ctx.tenant), ctx.shop_filter)
    if status == "open":
        rows = rows.filter(status="open")
    rows = rows.select_related("shop", "terminal").order_by("-created_at", "-id")
    return paged(request, rows, lambda chunk: [
        {"id": str(row.id), "created_at": mon.local_iso(row.created_at), "shop": shop_ref(row.shop),
         "terminal_name": row.terminal.name, "reason": row.reason, "reason_text": conflict_reason_text(row),
         "status": row.status, "status_text": CONFLICT_STATUS_TEXT.get(row.status, row.status)} for row in chunk])


# Account -----------------------------------------------------------------------

@app_endpoint
def devices(request):
    require_method(request, "GET")
    ctx = app_context(request, tenant_required=False)
    rows = (AppDevice.objects.filter(user=ctx.user, revoked_at__isnull=True, activated_at__isnull=False)
            .order_by(F("last_seen_at").desc(nulls_last=True), "-created_at"))
    return JsonResponse({"results": [device_dict(row, ctx.device.id) for row in rows], "next_cursor": None})


@app_endpoint
def device_detail(request, pk):
    require_method(request, "DELETE")
    ctx = app_context(request, tenant_required=False)
    row = AppDevice.objects.filter(pk=pk, user=ctx.user, activated_at__isnull=False).first()
    if row is None:
        fail("not_found")
    if revoke_device(row, "user", ctx.user):
        audit(None, ctx.user, "app.device_revoked", row, {"by": "user", "current": row.id == ctx.device.id})
    return JsonResponse({"ok": True})


@app_endpoint
def deletion_request(request):
    require_method(request, "POST")
    ctx = app_context(request, tenant_required=False)
    data = body(request)
    only_keys(data, ("note",))
    note = data.get("note") or ""
    if not isinstance(note, str) or len(note) > 500:
        fail("invalid_request", "Motivo: até 500 caracteres.")
    app_rate(f"app-delete:{ctx.user.pk}", 3, 86400)
    request_deletion(ctx.user, ctx.user.email, "app", note.strip(), client_ip(request))
    return JsonResponse({"status": "received", "message": DELETION_MESSAGE}, status=202)


# Platform (CrownTech staff, TOTP done in this sign-in) ----------------------------

def platform_context(request, manage=False):
    ctx = app_context(request, tenant_required=False)
    if not ctx.platform or (manage and not ctx.user.is_superuser):
        fail("platform_forbidden")
    return ctx


def tenant_health(tenants):
    """Status and health of a list of companies, aggregated per company in a few queries."""
    ids = [row.id for row in tenants]
    online_after = timezone.now() - mon.ONLINE_WINDOW
    paired = Terminal.objects.filter(tenant_id__in=ids, revoked=False, token_hash__isnull=False)
    totals = dict(paired.values_list("tenant_id").annotate(v=Count("id")))
    online = dict(paired.filter(last_seen_at__gte=online_after).values_list("tenant_id").annotate(v=Count("id")))
    shops = dict(Shop.objects.filter(tenant_id__in=ids, active=True).values_list("tenant_id").annotate(v=Count("id")))
    conflicts_open = dict(SyncConflict.objects.filter(tenant_id__in=ids, status="open").values_list("tenant_id")
                          .annotate(v=Count("id")))
    subscriptions = {row.tenant_id: row for row in Subscription.objects.filter(tenant_id__in=ids)}
    activity = mon.last_activity(ids)
    result = []
    for tenant in tenants:
        sub = subscriptions.get(tenant.id)
        status = _effective_license_status(sub) if sub else "expired"
        total = totals.get(tenant.id, 0)
        offline = total - online.get(tenant.id, 0)
        conflicts_count = conflicts_open.get(tenant.id, 0)
        if not tenant.active or status in ("suspended", "cancelled", "expired"):
            health = "inactive"
        elif offline > 0 or conflicts_count > 0 or status == "past_due":
            health = "attention"
        else:
            health = "ok"
        last_sale, last_seen = activity[tenant.id]
        result.append({"id": str(tenant.id), "name": tenant.name, "active": tenant.active,
                       "subscription_status": status, "subscription_status_text": SUBSCRIPTION_TEXT[status],
                       "shops_count": shops.get(tenant.id, 0), "terminals_total": total, "terminals_offline": offline,
                       "conflicts_open": conflicts_count, "last_sale_at": mon.local_iso(last_sale),
                       "last_seen_at": mon.local_iso(last_seen), "health": health,
                       "health_text": HEALTH_TEXT[health]})
    return result


@app_endpoint
def platform_tenants(request):
    require_method(request, "GET")
    platform_context(request)
    health = option(request, "health", ("all", "attention"), "all")
    rows = Tenant.objects.all()
    q = search_text(request.GET)
    if q:
        rows = rows.filter(name__icontains=q)
    rows = rows.order_by("name", "id")
    if health == "attention":
        attention = [row for row in tenant_health(list(rows)) if row["health"] == "attention"]
        return paged(request, attention, lambda chunk: chunk)
    return paged(request, rows, tenant_health)


@app_endpoint
def platform_access(request):
    require_method(request, "GET", "POST")
    ctx = platform_context(request, manage=True)
    if request.method == "GET":
        return paged(request, filtered_accesses(request.GET), access_dicts)
    row = grant_access(ctx.user, payload(request))
    return JsonResponse(access_dict(row), status=201)


def _access_row(pk):
    row = access_queryset().filter(pk=pk).first()
    if row is None:
        fail("not_found")
    return row


@app_endpoint
def platform_access_detail(request, pk):
    require_method(request, "PATCH")
    ctx = platform_context(request, manage=True)
    return JsonResponse(access_dict(update_access(ctx.user, _access_row(pk), payload(request))))


@app_endpoint
def platform_access_revoke(request, pk):
    require_method(request, "POST")
    ctx = platform_context(request, manage=True)
    return JsonResponse(access_dict(revoke_access(ctx.user, _access_row(pk), payload(request))))


@app_endpoint
def platform_access_options(request):
    require_method(request, "GET")
    platform_context(request, manage=True)
    return JsonResponse(access_options(request.GET.get("tenant_id", "").strip()))


@app_endpoint
def platform_devices(request):
    require_method(request, "GET")
    platform_context(request, manage=True)
    return paged(request, filtered_devices(request.GET), lambda chunk: [platform_device_dict(row) for row in chunk])


@app_endpoint
def platform_device_revoke(request, pk):
    require_method(request, "POST")
    ctx = platform_context(request, manage=True)
    row = AppDevice.objects.select_related("user").filter(pk=pk, activated_at__isnull=False).first()
    if row is None:
        fail("not_found")
    return JsonResponse(platform_device_dict(revoke_any_device(ctx.user, row, payload(request))))
