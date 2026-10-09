"""Device pairing, signed offline grants, bootstrap and atomic outbox intake."""
import hashlib
import re
import secrets
import time
from datetime import timedelta

from django.db import IntegrityError, transaction
from django.db.models import Sum
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt

from shared.licensing import LicenseError, load_private_key, load_public_key, payload_digest, sign_lease
from .api_common import APIError, audit, client_ip, digest, endpoint, fields, membership_context, method, money, page, payload, product_quantity, reset_rate, scoped, subscription_window, take_rate, text, uuid_value
from .models import Membership, Product, Sale, Shop, StockMovement, Subscription, SyncConflict, SyncEvent, Tenant, Terminal
from .sale_logic import event_time, persist_sale, reverse_sale
from .serializers import plan as plan_data, sync_conflict as sync_conflict_data, terminal as terminal_data
from .views_stock import balance

DEVICE_ROLES = {"owner", "admin"}
EVENT_TYPES = {"sale.completed", "sale.voided", "stock.adjusted", "cash.opened", "cash.closed",
               "cash.withdrawn", "cash.supplied"}


def _hash(value):
    return hashlib.sha256(value.encode()).hexdigest()


def _subscription_can_issue(tenant):
    return subscription_window(tenant)


def _effective_license_status(row):
    """Present the same activation window enforced when issuing terminal leases."""
    now = timezone.now()
    if row.status == "trial":
        return "trial" if row.trial_ends_at and row.trial_ends_at > now else "expired"
    if row.status in ("suspended", "cancelled"):
        return row.status
    if row.status in ("active", "past_due") and row.current_period_end:
        deadline = (row.current_period_end if row.license_source == "vip" else
                    row.current_period_end + timedelta(hours=72))
        if deadline <= now:
            return "expired"
        if row.status == "active" and row.current_period_end > now:
            return "active"
        return "past_due"
    return "expired"


def _operators(tenant):
    return [{"id": str(m.id), "name": m.name, "role": m.role, "pin_hash": m.pin_hash}
            for m in Membership.objects.filter(tenant=tenant, active=True).exclude(pin_hash="").order_by("id")]


def _issuer_profile(tenant):
    """Only describe fiscal identity configured by the company.

    Authorization fields are intentionally absent until a real NFC-e provider
    returns them; a local sale must never manufacture a key or QR code.
    """
    return {
        "legal_name": tenant.name,
        "trade_name": tenant.trade_name,
        "document": tenant.document,
        "state_registration": tenant.state_registration,
        "municipal_registration": tenant.municipal_registration,
        "address": {"line": tenant.address_line, "number": tenant.address_number,
                     "complement": tenant.address_complement, "neighborhood": tenant.address_neighborhood,
                     "city": tenant.address_city, "state": tenant.address_state, "zip": tenant.address_zip},
        "tax_regime": tenant.tax_regime,
        "fiscal_environment": tenant.fiscal_environment,
        "nfce_series": tenant.nfce_series,
    }


def _lease(terminal, operators=None):
    sub, effective_status, deadline = _subscription_can_issue(terminal.tenant)
    operators = _operators(terminal.tenant) if operators is None else operators
    now = int(time.time())
    seconds = 24 * 3600 if effective_status == "past_due" else 72 * 3600
    expires = min(now + seconds, int(deadline.timestamp()))
    if expires <= now:
        raise APIError("Assinatura sem prazo de licença", 403)
    claims = {"schema": 1, "tenant_id": str(terminal.tenant_id),
              "shop_id": str(terminal.shop_id), "terminal_id": str(terminal.id),
              "device_id": terminal.device_id, "issued_at": now, "expires_at": expires,
              "features": sub.plan.features, "status": effective_status,
              "max_discount_bps": terminal.tenant.max_discount_bps,
              "roles_version": terminal.tenant.roles_version,
              "operators_hash": payload_digest(operators)}
    try:
        return sign_lease(claims, load_private_key()), load_public_key()
    except LicenseError as exc:
        raise APIError("Chaves de licença indisponíveis", 503) from exc


def _bearer_terminal(request):
    authorization = request.headers.get("Authorization", "")
    if not authorization.startswith("Bearer "):
        raise APIError("Terminal não autenticado", 401)
    token = authorization[7:]
    if not 32 <= len(token) <= 160:
        raise APIError("Terminal não autenticado", 401)
    terminal = Terminal.objects.select_related("tenant", "shop").filter(token_hash=_hash(token), revoked=False).first()
    if not terminal or not terminal.device_id or not terminal.tenant.active:
        raise APIError("Terminal não autenticado", 401)
    Terminal.objects.filter(pk=terminal.pk).update(last_seen_at=timezone.now())
    return terminal


@endpoint
def subscription(request):
    method(request, "GET")
    member = membership_context(request)
    row = Subscription.objects.select_related("plan").filter(tenant=member.tenant).first()
    if row is None:
        raise APIError("Assinatura não encontrada", 404)
    used = Terminal.objects.filter(tenant=member.tenant, revoked=False).count()
    limit = row.plan.terminal_limit
    effective_status = _effective_license_status(row)
    response = JsonResponse({"id": str(row.id), "status": row.status, "plan": plan_data(row.plan),
                             "trial_ends_at": row.trial_ends_at.isoformat() if row.trial_ends_at else None,
                             "current_period_end": row.current_period_end.isoformat() if row.current_period_end else None,
                             "license": {"key": row.license_key if member.role in DEVICE_ROLES else None,
                                         "source": row.license_source, "status": effective_status,
                                         "can_activate": effective_status in ("trial", "active", "past_due"),
                                         "terminal_limit": limit, "terminal_used": used,
                                         "terminal_available": max(0, limit - used)}})
    response["Cache-Control"] = "private, no-store"
    return response


@endpoint
def terminals(request):
    method(request, "GET", "POST")
    member = membership_context(request, roles=DEVICE_ROLES, write=request.method == "POST")
    tenant = member.tenant
    if request.method == "GET":
        return page(request, Terminal.objects.filter(tenant=tenant).order_by("-created_at", "-id"), terminal_data)
    data = payload(request)
    fields(data, ("shop_id", "name"), ("shop_id", "name"))
    shop = scoped(Shop, tenant, data["shop_id"])
    if not shop.active:
        raise APIError("Loja inativa", 409)
    name = text(data["name"], "Terminal", 120)
    with transaction.atomic():
        Tenant.objects.select_for_update().get(pk=tenant.pk)
        sub, _, _ = _subscription_can_issue(tenant)
        if Terminal.objects.filter(tenant=tenant, revoked=False).count() >= sub.plan.terminal_limit:
            raise APIError("Limite de terminais do plano atingido", 409)
        code = secrets.token_urlsafe(12)
        row = Terminal.objects.create(tenant=tenant, shop=shop, name=name,
                                      pairing_code_hash=_hash(code),
                                      pairing_expires_at=timezone.now() + timedelta(minutes=10))
        audit(tenant, member.user, "terminal.created", row)
    return JsonResponse({**terminal_data(row), "pairing_code": code,
                         "pairing_expires_at": row.pairing_expires_at.isoformat()}, status=201)


@endpoint
def terminal_detail(request, pk):
    method(request, "GET", "PATCH")
    member = membership_context(request, roles=DEVICE_ROLES, write=request.method == "PATCH")
    row = scoped(Terminal, member.tenant, pk)
    if request.method == "GET":
        return JsonResponse(terminal_data(row))
    data = payload(request)
    fields(data, ("name", "revoked", "reissue_pairing"))
    if not data:
        raise APIError("Informe ao menos um campo")
    if "revoked" in data and type(data["revoked"]) is not bool:
        raise APIError("Estado inválido")
    if "reissue_pairing" in data and type(data["reissue_pairing"]) is not bool:
        raise APIError("Reemissão inválida")
    code = None
    with transaction.atomic():
        Tenant.objects.select_for_update().get(pk=member.tenant_id)
        row = scoped(Terminal, member.tenant, pk)
        if "revoked" in data and row.revoked and not data["revoked"]:
            sub, _, _ = _subscription_can_issue(member.tenant)
            if Terminal.objects.filter(tenant=member.tenant, revoked=False).count() >= sub.plan.terminal_limit:
                raise APIError("Limite de terminais do plano atingido", 409)
        if "name" in data:
            row.name = text(data["name"], "Terminal", 120)
        if "revoked" in data:
            row.revoked = data["revoked"]
        if data.get("reissue_pairing"):
            if row.revoked:
                raise APIError("Terminal revogado", 409)
            code = secrets.token_urlsafe(12)
            row.pairing_code_hash = _hash(code)
            row.pairing_expires_at = timezone.now() + timedelta(minutes=10)
        row.save()
        audit(member.tenant, member.user, "terminal.updated", row, {"fields": list(data)})
    response = terminal_data(row)
    if code:
        response.update(pairing_code=code, pairing_expires_at=row.pairing_expires_at.isoformat())
    return JsonResponse(response)


@csrf_exempt
@endpoint
def pair(request):
    method(request, "POST")
    data = payload(request)
    fields(data, ("license_key", "code", "device_id", "version"),
           ("license_key", "code", "device_id", "version"))
    license_key = text(data["license_key"], "Licença", 40).upper()
    code = text(data["code"], "Código", 80)
    device_id = text(data["device_id"], "Dispositivo", 160)
    version = text(data["version"], "Versão", 80)
    ip = client_ip(request)
    limit_key = take_rate(f"pair:{ip}", 15, 600)
    try:
        with transaction.atomic():
            row = Terminal.objects.select_for_update().select_related("tenant", "shop").filter(
                pairing_code_hash=_hash(code), revoked=False,
                tenant__subscription__license_key=license_key,
                pairing_expires_at__gt=timezone.now()).first()
            if not row:
                raise APIError("Pareamento inválido ou expirado", 401)
            _subscription_can_issue(row.tenant)
            previous = (Terminal.objects.select_for_update()
                        .filter(tenant=row.tenant, device_id=device_id)
                        .exclude(pk=row.pk).first())
            if previous and not previous.revoked:
                raise APIError("Dispositivo já vinculado; revogue o terminal antigo no painel", 409)
            if previous:
                # A revoked terminal no longer owns the local device. Release
                # its identity so an administrator can transfer this machine
                # to the newly-created terminal without touching sales history.
                previous.device_id = ""
                previous.token_hash = None
                previous.version = ""
                previous.last_seen_at = None
                previous.save(update_fields=["device_id", "token_hash", "version", "last_seen_at"])
            token = secrets.token_urlsafe(48)
            row.device_id = device_id
            row.version = version
            row.token_hash = _hash(token)
            row.pairing_code_hash = None
            row.pairing_expires_at = None
            row.last_seen_at = timezone.now()
            row.save()
            lease, public_key = _lease(row)
            audit(row.tenant, None, "terminal.paired", row, {"device_id": device_id})
    except IntegrityError as exc:
        raise APIError("Dispositivo já vinculado ou código já utilizado", 409) from exc
    reset_rate(limit_key)
    return JsonResponse({"token": token, "terminal_id": str(row.id),
                         "tenant_id": str(row.tenant_id), "shop_id": str(row.shop_id),
                         "license_hint": row.tenant.subscription.license_key.replace("-", "")[-4:],
                         "tenant": {"id": str(row.tenant_id), "name": row.tenant.name},
                         "shop": {"id": str(row.shop_id), "name": row.shop.name},
                         "terminal": terminal_data(row), "issuer": _issuer_profile(row.tenant),
                         "lease": lease, "public_key": public_key})


@csrf_exempt
@endpoint
def bootstrap(request):
    method(request, "GET")
    terminal = _bearer_terminal(request)
    operators = _operators(terminal.tenant)
    lease, public_key = _lease(terminal, operators)
    try:
        limit = int(request.GET.get("limit", "500"))
        offset = int(request.GET.get("cursor", "0"))
    except ValueError:
        raise APIError("Paginação inválida")
    if not 1 <= limit <= 1000 or not 0 <= offset <= 10**7:
        raise APIError("Paginação inválida")
    rows = list(Product.objects.filter(tenant=terminal.tenant).select_related("category")
                .order_by("id")[offset:offset + limit + 1])
    shown = rows[:limit]
    balances = (StockMovement.objects.filter(tenant=terminal.tenant, shop=terminal.shop,
                                             product_id__in=[p.id for p in shown])
                .values("product_id").annotate(q=Sum("delta_milli")))
    by_product = {row["product_id"]: row["q"] for row in balances}
    products = [{"id": str(p.id), "sku": p.sku, "barcode": p.barcode, "ncm": p.ncm, "scale_code": p.scale_code,
                 "name": p.name, "unit": p.unit, "price_cents": p.price_cents,
                 "cost_cents": p.cost_cents, "image_url": p.image_url, "active": p.active,
                 "quantity_milli": by_product.get(p.id, 0),
                 "category": p.category.name if p.category else None}
            for p in shown]
    return JsonResponse({"tenant": {"id": str(terminal.tenant_id), "name": terminal.tenant.name},
                         "shop": {"id": str(terminal.shop_id), "name": terminal.shop.name},
                         "terminal": terminal_data(terminal), "issuer": _issuer_profile(terminal.tenant), "products": products,
                         "next_cursor": str(offset + limit) if len(rows) > limit else None,
                         "operators": operators, "lease": lease, "public_key": public_key,
                         "license_hint": terminal.tenant.subscription.license_key.replace("-", "")[-4:]})


@csrf_exempt
@endpoint
def heartbeat(request):
    """Keep terminal presence current even when there are no sales to sync."""
    method(request, "POST")
    terminal = _bearer_terminal(request)
    now = timezone.now()
    data = payload(request)
    fields(data, ("version", "pending_events", "last_error", "client_time"))
    if "version" in data:
        terminal.version = text(data["version"], "Versão", 80)
    if "pending_events" in data:
        if type(data["pending_events"]) is not int or not 0 <= data["pending_events"] <= 10**7:
            raise APIError("Fila pendente inválida")
        terminal.heartbeat_pending_events = data["pending_events"]
    if "last_error" in data and data["last_error"] is not None:
        terminal.heartbeat_error = text(data["last_error"], "Erro de sincronização", 300, optional=True)
    update_fields = []
    if "version" in data: update_fields.append("version")
    if "pending_events" in data: update_fields.append("heartbeat_pending_events")
    if "last_error" in data: update_fields.append("heartbeat_error")
    if update_fields:
        terminal.save(update_fields=update_fields)
    return JsonResponse({"ok": True, "server_time": int(time.time()),
                         "terminal_id": str(terminal.id), "last_seen_at": now.isoformat()})


def _negative_stock_conflict(terminal, event, sale):
    if sale.stock_status != "negative":
        return None
    details = []
    for item in sale.items.select_related("product").all():
        available = balance(terminal.tenant, terminal.shop, item.product)
        if available < 0:
            details.append({"product_id": str(item.product_id), "product_name": item.name,
                            "requested_milli": item.quantity_milli,
                            "central_balance_milli": available})
    if not details:
        return None
    conflict, _ = SyncConflict.objects.get_or_create(
        event_id=event["event_id"],
        defaults={"tenant": terminal.tenant, "shop": terminal.shop, "terminal": terminal,
                  "event_type": event["type"], "aggregate_id": event["aggregate_id"],
                  "reason": "negative_stock", "detail":{"items": details}})
    return conflict


def _validate_event(event, terminal):
    if not isinstance(event, dict):
        raise APIError("Evento inválido")
    fields(event, ("event_id", "type", "aggregate_id", "identity", "data"),
           ("event_id", "type", "aggregate_id", "identity", "data"))
    event_id = event["event_id"]
    if not isinstance(event_id, str) or not re.fullmatch(r"[0-9a-f]{32}", event_id):
        raise APIError("ID do evento inválido")
    if not isinstance(event["type"], str) or event["type"] not in EVENT_TYPES:
        raise APIError("Tipo de evento inválido")
    aggregate_id = text(event["aggregate_id"], "Agregado", 160)
    identity = event["identity"]
    if not isinstance(identity, dict) or identity != {
        "tenant_id": str(terminal.tenant_id), "shop_id": str(terminal.shop_id),
        "terminal_id": str(terminal.id)}:
        raise APIError("Identidade do evento não corresponde ao terminal", 403)
    if not isinstance(event["data"], dict):
        raise APIError("Dados do evento inválidos")
    if event["type"] == "sale.voided":
        data = event["data"]
        fields(data, ("sale_id", "reason", "created_at"), ("sale_id", "reason", "created_at"))
        if uuid_value(data["sale_id"]) != uuid_value(aggregate_id):
            raise APIError("Venda do estorno não confere")
        text(data["reason"], "Motivo", 240)
        event_time(data["created_at"])
    elif event["type"] == "stock.adjusted":
        data = event["data"]
        fields(data, ("product_id", "delta_milli", "reason", "created_at"),
               ("product_id", "delta_milli", "reason", "created_at"))
        product = scoped(Product, terminal.tenant, data["product_id"])
        product_quantity(product, data["delta_milli"], allow_negative=True)
        text(data["reason"], "Motivo", 240)
        event_time(data["created_at"])
    elif event["type"] == "cash.opened":
        data = event["data"]
        fields(data, ("opening_cents", "operator_id", "created_at"),
               ("opening_cents", "operator_id", "created_at"))
        money(data["opening_cents"], "Abertura")
        uuid_value(data["operator_id"], "Operador")
        event_time(data["created_at"])
    elif event["type"] == "cash.closed":
        data = event["data"]
        fields(data, ("counted_cents", "expected_cents", "difference_cents", "created_at"),
               ("counted_cents", "expected_cents", "difference_cents", "created_at"))
        counted = money(data["counted_cents"], "Valor contado")
        expected = money(data["expected_cents"], "Valor esperado")
        if type(data["difference_cents"]) is not int or data["difference_cents"] != counted - expected:
            raise APIError("Diferença de caixa inválida")
        event_time(data["created_at"])
    elif event["type"] in ("cash.withdrawn", "cash.supplied"):
        data = event["data"]
        fields(data, ("cash_session_id", "amount_cents", "reason", "operator_id", "created_at"),
               ("cash_session_id", "amount_cents", "reason", "operator_id", "created_at"))
        uuid_value(data["cash_session_id"], "Sessão de caixa")
        money(data["amount_cents"], "Movimento de caixa", allow_zero=False)
        text(data["reason"], "Motivo", 240)
        uuid_value(data["operator_id"], "Operador")
        event_time(data["created_at"])


def _persist_adjustment(terminal, event):
    data = event["data"]
    product = scoped(Product, terminal.tenant, data["product_id"])
    key = f"adjustment:{terminal.id}:{event['aggregate_id']}"
    previous = StockMovement.objects.filter(tenant=terminal.tenant, source="terminal_adjustment",
                                            idempotency_key=key).first()
    if previous:
        if (previous.product_id != product.id or previous.delta_milli != data["delta_milli"] or
            previous.reason != data["reason"].strip()):
            raise APIError("Ajuste já recebido com conteúdo diferente", 409)
        return
    row = StockMovement.objects.create(tenant=terminal.tenant, shop=terminal.shop,
                                       product=product, delta_milli=data["delta_milli"],
                                       source="terminal_adjustment", reason=data["reason"].strip(),
                                       reference=event["aggregate_id"], terminal=terminal,
                                       idempotency_key=key)
    audit(terminal.tenant, None, "stock.terminal_adjusted", row,
          {"terminal_id": str(terminal.id)})


@csrf_exempt
@endpoint
def sync(request):
    method(request, "POST")
    terminal = _bearer_terminal(request)
    data = payload(request)
    fields(data, ("events",), ("events",))
    events = data["events"]
    if not isinstance(events, list) or not 1 <= len(events) <= 50:
        raise APIError("Envie de 1 a 50 eventos")
    for event in events:
        _validate_event(event, terminal)
    accepted = []
    conflicts = []
    with transaction.atomic():
        Terminal.objects.select_for_update().get(pk=terminal.pk)
        for event in events:
            event_hash = digest(event)
            previous = SyncEvent.objects.filter(event_id=event["event_id"]).first()
            if previous:
                if previous.tenant_id != terminal.tenant_id or previous.terminal_id != terminal.id or previous.payload_hash != event_hash:
                    raise APIError("Evento já recebido com conteúdo diferente", 409)
            else:
                SyncEvent.objects.create(event_id=event["event_id"], tenant=terminal.tenant,
                                         shop=terminal.shop, terminal=terminal,
                                         event_type=event["type"], aggregate_id=event["aggregate_id"],
                                         payload=event, payload_hash=event_hash)
            if event["type"] == "sale.completed":
                sale = persist_sale(terminal, event)
                conflict = _negative_stock_conflict(terminal, event, sale)
                if conflict:
                    conflicts.append(sync_conflict_data(conflict))
            elif event["type"] == "sale.voided":
                row = Sale.objects.select_for_update().filter(
                    tenant=terminal.tenant, shop=terminal.shop,
                    terminal=terminal, pk=uuid_value(event["aggregate_id"])).first()
                if row is None:
                    raise APIError("Venda original do estorno não encontrada", 404)
                reverse_sale(row, event["data"]["reason"], "cancelled", terminal=terminal,
                             when=event_time(event["data"]["created_at"]))
            elif event["type"] == "stock.adjusted":
                _persist_adjustment(terminal, event)
            accepted.append(event["event_id"])
    return JsonResponse({"accepted_event_ids": accepted, "conflicts": conflicts})


@endpoint
def conflicts(request):
    method(request, "GET")
    member = membership_context(request, roles={"owner", "admin", "manager"})
    rows = (SyncConflict.objects.select_related("terminal", "shop", "resolved_by")
            .filter(tenant=member.tenant))
    status = request.GET.get("status")
    if status:
        if status not in {choice[0] for choice in SyncConflict.STATUS}:
            raise APIError("Estado de conflito inválido")
        rows = rows.filter(status=status)
    return page(request, rows.order_by("-created_at", "-id"), sync_conflict_data)


@endpoint
def conflict_detail(request, pk):
    method(request, "PATCH")
    member = membership_context(request, roles={"owner", "admin", "manager"}, write=True)
    row = SyncConflict.objects.filter(tenant=member.tenant, pk=uuid_value(pk)).first()
    if row is None:
        raise APIError("Conflito não encontrado", 404)
    data = payload(request)
    fields(data, ("status", "resolution"), ("status",))
    status = text(data["status"], "Estado", 20)
    if status not in {"acknowledged", "resolved", "ignored"}:
        raise APIError("Estado de resolução inválido")
    resolution = text(data.get("resolution", ""), "Resolução", 1000, optional=True)
    row.status, row.resolution = status, resolution
    row.resolved_by = member.user
    row.resolved_at = timezone.now()
    row.save(update_fields=["status", "resolution", "resolved_by", "resolved_at"])
    audit(member.tenant, member.user, "sync.conflict_resolved", row,
          {"status": status, "resolution": resolution})
    return JsonResponse(sync_conflict_data(row))
