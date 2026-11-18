"""Device pairing, signed offline grants, bootstrap and per-event outbox intake."""
import hashlib
import json
import logging
import re
import secrets
import time
from datetime import timedelta

from django.db import IntegrityError, InterfaceError, OperationalError, transaction
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt

from shared.licensing import MAX_LEASE_SECONDS, LicenseError, load_private_key, load_public_key, payload_digest, sign_lease
from shared.releases import load_release_public_key
from .api_common import APIError, audit, client_ip, digest, endpoint, fields, membership_context, method, money, page, payload, quantity, reset_rate, scoped, subscription_window, take_rate, text, uuid_value
from .fiscal import FiscalContext, issuer_profile_extras
from .models import Customer, Membership, Product, Shop, StockMovement, Subscription, SyncConflict, SyncEvent, Tenant, Terminal
from .models import CashSession, TerminalFormerToken
from .backup_key import tenant_backup_key
from .cash_logic import CASH_EVENTS, apply_cash_event
from .credit_logic import apply_credit_receipt, apply_return, balances as credit_balances
from .payment_methods import ensure as ensure_payment_methods, method_data
from .views_promotions import for_terminal as promotions_for_terminal
from .stock_balance import balances as stock_balances
from .sale_logic import event_time, open_conflict, persist_sale, void_from_terminal
from .serializers import plan as plan_data, sync_conflict as sync_conflict_data, terminal as terminal_data
from .stock_balance import balance
from . import restore_logic
from .station_logic import effective_hub_id, is_hub, valid_stations

logger = logging.getLogger(__name__)

DEVICE_ROLES = {"owner", "admin"}
LONG_LEASE_CAPABILITY = "long-lease"
LEGACY_LEASE_SECONDS = 72 * 3600
EVENT_TYPES = {"sale.completed", "sale.voided", "sale.returned", "credit.received", "stock.adjusted", "cash.opened", "cash.closed",
               "cash.withdrawn", "cash.supplied", "terminal.config"}
# "Trocar de computador" (fase 2). The cloud announces what it supports and the
# terminal only uses what was announced.
SEND_ONLY_DAYS = 7  # minimum; see _send_only_days
SEND_ONLY_SLACK_DAYS = 1
RESTORE_HISTORY_DAYS = 90
RECOVERY_CODE_MINUTES = 30
RESTORE_PAGE_DEFAULT = 200
RESTORE_PAGE_MAX = 500
RESTORE_PAGE_BYTES = 1_000_000
# "stations" (fase 3): events may carry the top-level "station" label, terminal.config may
# carry "stations" and the lease carries the claims "max_stations" and "hub".
CLOUD_CAPABILITIES = ["restore", "send-only", "terminal-config", "mirror-key", "stations"]
RECOVERY_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
RECOVERY_CODE_SHAPE = re.compile(r"[A-Za-z2-9]{4}-?[A-Za-z2-9]{4}-?[A-Za-z2-9]{4}")
TRANSFERRED_MESSAGE = "Este caixa foi transferido para outro computador."


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
        # Fiscal identity for the NFC-e (CRT, CNAE, IBGE...); old terminals ignore these keys.
        **issuer_profile_extras(tenant),
    }


def _long_lease(request):
    """Terminals that accept leases above the legacy ceiling declare it per request."""
    declared = request.headers.get("X-PDV-Capabilities", "")
    return LONG_LEASE_CAPABILITY in {item.strip().lower() for item in declared[:400].split(",")}


def _release_public_key():
    """Pinned by the terminal to verify release manifests; never blocks a lease."""
    try:
        return load_release_public_key()
    except LicenseError:
        logger.error("Chave de versões indisponível; resposta segue sem release_public_key")
        return None


def _lease(terminal, operators=None, long_lease=False):
    sub, effective_status, deadline = _subscription_can_issue(terminal.tenant)
    operators = _operators(terminal.tenant) if operators is None else operators
    now = int(time.time())
    seconds = LEGACY_LEASE_SECONDS
    if long_lease:
        seconds = min(max(sub.plan.offline_days, 1) * 86400, MAX_LEASE_SECONDS)
    if effective_status == "past_due":
        seconds = min(seconds, 24 * 3600)
    expires = min(now + seconds, int(deadline.timestamp()))
    if expires <= now:
        raise APIError("Assinatura sem prazo de licença", 403)
    # Additional registers are per shop and belong to its main terminal only.
    hub = is_hub(terminal)
    claims = {"schema": 1, "tenant_id": str(terminal.tenant_id),
              "shop_id": str(terminal.shop_id), "terminal_id": str(terminal.id),
              "device_id": terminal.device_id, "issued_at": now, "expires_at": expires,
              "features": sub.plan.features, "status": effective_status,
              "max_discount_bps": terminal.tenant.max_discount_bps,
              "business_hours": terminal.tenant.business_hours or {},
              "roles_version": terminal.tenant.roles_version,
              "operators_hash": payload_digest(operators),
              "max_stations": sub.plan.max_stations if hub else 0, "hub": hub}
    try:
        return sign_lease(claims, load_private_key()), load_public_key()
    except LicenseError as exc:
        raise APIError("Chaves de licença indisponíveis", 503) from exc


def _recovery_code():
    """Easy to type on the new computer: XXXX-XXXX-XXXX, no 0/O/1/I."""
    letters = "".join(secrets.choice(RECOVERY_ALPHABET) for _ in range(12))
    return f"{letters[:4]}-{letters[4:8]}-{letters[8:]}"


def _pairing_code_hashes(code):
    """Hashes to look up. A typed recovery code is accepted in any case, with
    or without hyphens; the code exactly as typed keeps working as before."""
    compact = re.sub(r"\s+", "", code)
    hashes = [_hash(code)]
    if RECOVERY_CODE_SHAPE.fullmatch(compact):
        letters = compact.replace("-", "").upper()
        hashes.append(_hash(f"{letters[:4]}-{letters[4:8]}-{letters[8:]}"))
    return hashes


def _send_only_until(terminal, now=None):
    """Latest deadline while some replaced computer may still send its queue, else None."""
    return (TerminalFormerToken.objects.filter(terminal=terminal, expires_at__gt=now or timezone.now())
            .order_by("-expires_at").values_list("expires_at", flat=True).first())


def _mirror_former_token(terminal):
    """Terminal.previous_* show the newest hand-over to the panel; the list is the truth."""
    newest = TerminalFormerToken.objects.filter(terminal=terminal).order_by("-transferred_at", "-expires_at").first()
    terminal.previous_token_hash = newest.token_hash if newest else None
    terminal.previous_token_expires_at = newest.expires_at if newest else None
    terminal.previous_device_id = newest.device_id if newest else ""
    terminal.transferred_at = newest.transferred_at if newest else None


def _bearer_terminal(request, send_only_ok=False):
    authorization = request.headers.get("Authorization", "")
    if not authorization.startswith("Bearer "):
        raise APIError("Terminal não autenticado", 401)
    token = authorization[7:]
    if not 32 <= len(token) <= 160:
        raise APIError("Terminal não autenticado", 401)
    hashed = _hash(token)
    terminal = Terminal.objects.select_related("tenant", "shop").filter(token_hash=hashed, revoked=False).first()
    if terminal:
        if not terminal.device_id or not terminal.tenant.active:
            raise APIError("Terminal não autenticado", 401)
        Terminal.objects.filter(pk=terminal.pk).update(last_seen_at=timezone.now())
        terminal.send_only = False
        return terminal
    # Token of a computer replaced by "Trocar de computador": it may only
    # send what was left in its queue, until its own deadline. It never counts
    # as presence (last_seen_at belongs to the current computer). Every replaced
    # computer keeps its own row, so a second hand-over does not lock out the first.
    former = (TerminalFormerToken.objects.select_related("terminal__tenant", "terminal__shop")
              .filter(token_hash=hashed, terminal__revoked=False).first())
    if not former or not former.terminal.tenant.active:
        raise APIError("Terminal não autenticado", 401)
    terminal = former.terminal
    deadline = former.expires_at if former.expires_at > timezone.now() else None
    if deadline and send_only_ok:
        terminal.send_only = True
        terminal.send_only_until = deadline
        return terminal
    raise APIError(TRANSFERRED_MESSAGE, 403, code="terminal_transferred",
                   extra={"send_only_until": deadline.isoformat() if deadline else None,
                          "transferred_at": former.transferred_at.isoformat()})


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
                                         "max_stations": row.plan.max_stations,
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
    fields(data, ("name", "revoked", "reissue_pairing", "end_send_only", "hub"))
    if not data:
        raise APIError("Informe ao menos um campo")
    if "hub" in data and data["hub"] is not True:
        raise APIError("Para trocar o caixa principal da loja, escolha o outro terminal")
    if "end_send_only" in data and type(data["end_send_only"]) is not bool:
        raise APIError("Encerramento inválido")
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
        ended = bool(data.get("end_send_only")) and _send_only_until(row) is not None
        if ended:
            # The rows stay: the old computers keep hearing "transferido", not "não autenticado".
            now = timezone.now()
            TerminalFormerToken.objects.filter(terminal=row, expires_at__gt=now).update(expires_at=now)
            _mirror_former_token(row)
        row.save()
        hub_change = None
        shop = Shop.objects.select_for_update().get(pk=row.shop_id)
        if row.revoked and shop.hub_terminal_id == row.id:
            # A revoked terminal is no longer the shop's main register.
            shop.hub_terminal = None
            shop.save(update_fields=["hub_terminal"])
        if data.get("hub"):
            if row.revoked:
                raise APIError("Terminal revogado", 409)
            if not row.token_hash:
                raise APIError("Conecte este terminal a um computador antes de torná-lo o caixa principal.", 409,
                               code="terminal_not_paired")
            previous_hub = effective_hub_id(shop)
            if shop.hub_terminal_id != row.id:
                shop.hub_terminal = row
                shop.save(update_fields=["hub_terminal"])
            if previous_hub != row.id:
                hub_change = {"shop_id": str(shop.id),
                              "previous_terminal_id": str(previous_hub) if previous_hub else None}
        audit(member.tenant, member.user, "terminal.updated", row, {"fields": list(data)})
        if hub_change is not None:
            audit(member.tenant, member.user, "terminal.hub_set", row, hub_change)
        if ended:
            audit(member.tenant, member.user, "terminal.send_only_ended", row,
                  {"previous_device_id": row.previous_device_id})
    response = terminal_data(row)
    if code:
        response.update(pairing_code=code, pairing_expires_at=row.pairing_expires_at.isoformat())
    return JsonResponse(response)


def _send_only_days(tenant):
    """Days the replaced computer may keep uploading its queue.

    It may still hold a signed lease valid for the plan's offline days and go
    on selling without internet, so the send-only window covers that lease
    (plus a slack to upload) and is never shorter than SEND_ONLY_DAYS.
    """
    offline = (Subscription.objects.filter(tenant=tenant)
               .values_list("plan__offline_days", flat=True).first()) or 0
    lease_days = min(max(offline, 1) * 86400, MAX_LEASE_SECONDS) // 86400
    return max(SEND_ONLY_DAYS, lease_days + SEND_ONLY_SLACK_DAYS)


@endpoint
def terminal_transfer(request, pk):
    """"Trocar de computador": a recovery code that pairs a new computer to the
    same terminal. Nothing changes for the old one until the code is used."""
    method(request, "POST")
    member = membership_context(request, roles=DEVICE_ROLES, write=True)
    fields(payload(request), ())
    with transaction.atomic():
        Tenant.objects.select_for_update().get(pk=member.tenant_id)
        row = scoped(Terminal, member.tenant, pk)
        if row.revoked:
            raise APIError("Terminal revogado", 409)
        if not row.token_hash:
            raise APIError('Este terminal ainda não foi conectado a um computador. Use "Novo código".', 409,
                           code="terminal_not_paired")
        code = _recovery_code()
        row.pairing_code_hash = _hash(code)
        row.pairing_expires_at = timezone.now() + timedelta(minutes=RECOVERY_CODE_MINUTES)
        row.save(update_fields=["pairing_code_hash", "pairing_expires_at"])
        audit(member.tenant, member.user, "terminal.transfer_started", row,
              {"expires_at": row.pairing_expires_at.isoformat()})
    license_key = Subscription.objects.filter(tenant=member.tenant).values_list("license_key", flat=True).first()
    response = JsonResponse({
        **terminal_data(row), "recovery_code": code, "recovery_expires_at": row.pairing_expires_at.isoformat(),
        "license_key": license_key, "pending_events": row.heartbeat_pending_events,
        "open_cash_session": bool(restore_logic.current_open_session_ids(row)),
        "send_only_days": _send_only_days(row.tenant), "history_days": RESTORE_HISTORY_DAYS})
    response["Cache-Control"] = "private, no-store"
    return response


@csrf_exempt
@endpoint
def pair(request):
    method(request, "POST")
    data = payload(request)
    fields(data, ("license_key", "code", "device_id", "version"),
           ("license_key", "code", "device_id", "version"))
    license_key = text(data["license_key"], "Licença", 40).upper()
    code_hashes = _pairing_code_hashes(text(data["code"], "Código", 80))
    device_id = text(data["device_id"], "Dispositivo", 160)
    version = text(data["version"], "Versão", 80)
    ip = client_ip(request)
    limit_key = take_rate(f"pair:{ip}", 15, 600)
    try:
        with transaction.atomic():
            row = Terminal.objects.select_for_update().select_related("tenant", "shop").filter(
                pairing_code_hash__in=code_hashes, revoked=False,
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
            now = timezone.now()
            transferred = bool(row.token_hash) and row.device_id != device_id
            if transferred:
                # The replaced computer keeps a send-only token so the sales
                # still in its queue reach the cloud. Earlier hand-overs keep theirs.
                TerminalFormerToken.objects.create(
                    terminal=row, token_hash=row.token_hash, device_id=row.device_id, transferred_at=now,
                    expires_at=now + timedelta(days=_send_only_days(row.tenant)))
                _mirror_former_token(row)
            token = secrets.token_urlsafe(48)
            row.device_id = device_id
            row.version = version
            row.token_hash = _hash(token)
            row.pairing_code_hash = None
            row.pairing_expires_at = None
            row.last_seen_at = timezone.now()
            row.save()
            lease, public_key = _lease(row, long_lease=_long_lease(request))
            audit(row.tenant, None, "terminal.paired", row, {"device_id": device_id})
            if transferred:
                audit(row.tenant, None, "terminal.transferred", row,
                      {"device_id": device_id, "previous_device_id": row.previous_device_id,
                       "send_only_until": row.previous_token_expires_at.isoformat()})
            # A replaced computer or history in the cloud: the terminal starts in recovery
            # mode. A cashier that never synchronized still has its backup copy to read.
            recovery = transferred or SyncEvent.objects.filter(terminal=row).exists()
    except IntegrityError as exc:
        raise APIError("Dispositivo já vinculado ou código já utilizado", 409) from exc
    reset_rate(limit_key)
    return JsonResponse({"token": token, "terminal_id": str(row.id),
                         "recovery": recovery, "history_days": RESTORE_HISTORY_DAYS,
                         "capabilities": CLOUD_CAPABILITIES, "mirror_key": tenant_backup_key(row.tenant),
                         "tenant_id": str(row.tenant_id), "shop_id": str(row.shop_id),
                         "license_hint": row.tenant.subscription.license_key.replace("-", "")[-4:],
                         "tenant": {"id": str(row.tenant_id), "name": row.tenant.name},
                         "shop": {"id": str(row.shop_id), "name": row.shop.name},
                         "terminal": terminal_data(row), "issuer": _issuer_profile(row.tenant),
                         "lease": lease, "public_key": public_key,
                         "release_public_key": _release_public_key(), "server_time": int(time.time())})


@csrf_exempt
@endpoint
def release(request):
    """"Desistir da recuperação": the computer that just took the terminal gives it back.

    Only while the computer replaced last may still send: its token and device become
    the current ones again and the hand-over is forgotten. Otherwise nothing changes.
    """
    method(request, "POST")
    terminal = _bearer_terminal(request)
    fields(payload(request), ())
    with transaction.atomic():
        row = Terminal.objects.select_for_update().select_related("tenant", "shop").get(pk=terminal.pk)
        former = (TerminalFormerToken.objects.select_for_update().filter(terminal=row)
                  .order_by("-transferred_at", "-expires_at").first())
        undone = bool(
            former and former.expires_at > timezone.now() and former.device_id
            and not Terminal.objects.filter(tenant=row.tenant, device_id=former.device_id).exclude(pk=row.pk).exists()
            and not Terminal.objects.filter(token_hash=former.token_hash).exists())
        if undone:
            given_up = row.device_id
            row.token_hash, row.device_id = former.token_hash, former.device_id
            row.last_seen_at = None
            former.delete()
            _mirror_former_token(row)
            row.save()
            audit(row.tenant, None, "terminal.transfer_undone", row,
                  {"device_id": row.device_id, "given_up_device_id": given_up})
    return JsonResponse({"released": undone, "terminal_id": str(row.id)})


@csrf_exempt
@endpoint
def lease(request):
    """Renew the offline lease without touching the catalog."""
    method(request, "POST")
    terminal = _bearer_terminal(request)
    take_rate(f"lease:{terminal.pk}", 30, 600)
    operators = _operators(terminal.tenant)
    envelope, public_key = _lease(terminal, operators, _long_lease(request))
    response = JsonResponse({"lease": envelope, "public_key": public_key, "operators": operators,
                             "server_time": int(time.time()), "release_public_key": _release_public_key(),
                             "capabilities": CLOUD_CAPABILITIES, "mirror_key": tenant_backup_key(terminal.tenant)})
    response["Cache-Control"] = "private, no-store"
    return response


@csrf_exempt
@endpoint
def bootstrap(request):
    method(request, "GET")
    terminal = _bearer_terminal(request)
    operators = _operators(terminal.tenant)
    lease, public_key = _lease(terminal, operators, _long_lease(request))
    try:
        limit = int(request.GET.get("limit", "500"))
        offset = int(request.GET.get("cursor", "0"))
    except ValueError:
        raise APIError("Paginação inválida")
    if not 1 <= limit <= 1000 or not 0 <= offset <= 10**7:
        raise APIError("Paginação inválida")
    rows = list(Product.objects.filter(tenant=terminal.tenant).select_related("category", "group", "tax_profile")
                .order_by("id")[offset:offset + limit + 1])
    fiscal_context = FiscalContext(terminal.tenant)
    shown = rows[:limit]
    by_product = stock_balances(terminal.tenant, terminal.shop, [p.id for p in shown])
    products = [{"id": str(p.id), "sku": p.sku, "barcode": p.barcode, "ncm": p.ncm, "scale_code": p.scale_code,
                 "name": p.name, "unit": p.unit, "price_cents": p.price_cents,
                 "cost_cents": p.cost_cents, "image_url": p.image_url, "active": p.active,
                 "stock_controlled": p.stock_controlled,
                 "quantity_milli": by_product.get(p.id, 0),
                 "category": p.category.name if p.category else None,
                 "group_id": str(p.group_id) if p.group_id else None,
                 "group_name": p.group.name if p.group_id else None, "variant": p.variant,
                 # Resolved tax profile and NFC-e readiness (docs/FISCAL.md); ignored by old terminals.
                 "fiscal": fiscal_context.terminal_payload(p)}
            for p in shown]
    customers = payment_config = promotion_rules = None
    if offset == 0:  # non-catalog data travels on the first page only
        payment_config = [method_data(row) for row in ensure_payment_methods(terminal.tenant)]
        promotion_rules = promotions_for_terminal(terminal.tenant)
        rows_c = list(Customer.objects.filter(tenant=terminal.tenant, active=True).order_by("name", "id")[:20000])
        totals = credit_balances(terminal.tenant, [c.id for c in rows_c])
        customers = [{"id": str(c.id), "name": c.name, "document": c.document,
                      "credit_limit_cents": c.credit_limit_cents,
                      "open_credit_cents": totals.get(c.id, (0, 0))[0],
                      "store_credit_cents": totals.get(c.id, (0, 0))[1]} for c in rows_c]
    return JsonResponse({"tenant": {"id": str(terminal.tenant_id), "name": terminal.tenant.name},
                         "shop": {"id": str(terminal.shop_id), "name": terminal.shop.name},
                         "customers": customers, "payment_methods": payment_config, "promotions": promotion_rules,
                         "terminal": terminal_data(terminal), "issuer": _issuer_profile(terminal.tenant), "products": products,
                         "next_cursor": str(offset + limit) if len(rows) > limit else None,
                         "operators": operators, "lease": lease, "public_key": public_key,
                         "release_public_key": _release_public_key(), "server_time": int(time.time()),
                         "capabilities": CLOUD_CAPABILITIES,
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
    return open_conflict(terminal, event, "negative_stock",
                         "Venda no caixa deixou o saldo central negativo. Confira a contagem física.",
                         items=details, sale_id=str(sale.id))


def _event_identity(event, terminal):
    """The only per-event checks that still refuse the whole request: an event
    that cannot be named back to the terminal, or that claims another identity."""
    if not isinstance(event, dict) or not isinstance(event.get("event_id"), str) or not 1 <= len(event["event_id"]) <= 64:
        raise APIError("Evento sem identificador válido")
    if event.get("identity") != {"tenant_id": str(terminal.tenant_id), "shop_id": str(terminal.shop_id),
                                 "terminal_id": str(terminal.id)}:
        raise APIError("Identidade do evento não corresponde ao terminal", 403)


def _validate_event(event):
    """Shape of one event. Any error here quarantines the event alone."""
    # "station" (optional, any type) labels the register of the shop that produced the
    # event. Its content is never judged here: station_logic.station_of reads a
    # malformed label as empty instead of refusing what happened at the counter.
    fields(event, ("event_id", "type", "aggregate_id", "identity", "data", "station"),
           ("event_id", "type", "aggregate_id", "identity", "data"))
    if not re.fullmatch(r"[0-9a-f]{32}", event["event_id"]):
        raise APIError("ID do evento inválido")
    if not isinstance(event["type"], str) or event["type"] not in EVENT_TYPES:
        raise APIError("Tipo de evento inválido")
    aggregate_id = text(event["aggregate_id"], "Agregado", 160)
    if not isinstance(event["data"], dict):
        raise APIError("Dados do evento inválidos")
    if event["type"] == "credit.received":
        data = event["data"]
        fields(data, ("customer_id", "amount_cents", "method", "operator_id", "cash_session_id", "created_at"),
               ("customer_id", "amount_cents", "method", "operator_id", "created_at"))
        uuid_value(aggregate_id, "Recebimento")
        uuid_value(data["customer_id"], "Cliente")
        money(data["amount_cents"], "Valor recebido", allow_zero=False)
        if data["method"] not in ("cash", "pix", "card_credit", "card_debit"):
            raise APIError("Forma de recebimento inválida")
        event_time(data["created_at"])
    elif event["type"] == "sale.returned":
        # Full validation needs the original sale, which may arrive earlier in
        # this same batch; credit_logic.apply_return validates inside the transaction.
        fields(event["data"], ("sale_id", "items", "total_cents", "refund_method", "reason", "operator_id",
                               "created_at", "customer_id", "supervisor_id", "cash_session_id"),
               ("sale_id", "items", "total_cents", "refund_method", "reason", "operator_id", "created_at"))
        uuid_value(aggregate_id, "Devolução")
        event_time(event["data"]["created_at"])
    elif event["type"] == "sale.voided":
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
        # The product is looked up when the adjustment is applied: its unit or
        # stock control may have changed since the count at the counter.
        uuid_value(data["product_id"], "Produto")
        quantity(data["delta_milli"], allow_negative=True)
        text(data["reason"], "Motivo", 240)
        event_time(data["created_at"])
    elif event["type"] == "cash.opened":
        data = event["data"]
        fields(data, ("opening_cents", "operator_id", "created_at"),
               ("opening_cents", "operator_id", "created_at"))
        uuid_value(aggregate_id, "Sessão de caixa")
        money(data["opening_cents"], "Abertura")
        uuid_value(data["operator_id"], "Operador")
        event_time(data["created_at"])
    elif event["type"] == "cash.closed":
        data = event["data"]
        fields(data, ("counted_cents", "expected_cents", "difference_cents", "created_at",
                      "operator_id", "supervisor_id"),
               ("counted_cents", "expected_cents", "difference_cents", "created_at"))
        uuid_value(aggregate_id, "Sessão de caixa")
        for key, label in (("operator_id", "Operador"), ("supervisor_id", "Supervisor")):
            if data.get(key):
                uuid_value(data[key], label)
        counted = money(data["counted_cents"], "Valor contado")
        # Signed: a cash sale cancelled after a withdrawal leaves the drawer owing.
        expected = data["expected_cents"]
        if type(expected) is not int or abs(expected) > 10**12:
            raise APIError("Valor esperado inválido")
        if type(data["difference_cents"]) is not int or data["difference_cents"] != counted - expected:
            raise APIError("Diferença de caixa inválida")
        event_time(data["created_at"])
    elif event["type"] in ("cash.withdrawn", "cash.supplied"):
        data = event["data"]
        fields(data, ("cash_session_id", "amount_cents", "reason", "operator_id", "created_at"),
               ("cash_session_id", "amount_cents", "reason", "operator_id", "created_at"))
        uuid_value(aggregate_id, "Movimento de caixa")
        uuid_value(data["cash_session_id"], "Sessão de caixa")
        money(data["amount_cents"], "Movimento de caixa", allow_zero=False)
        text(data["reason"], "Motivo", 240)
        uuid_value(data["operator_id"], "Operador")
        event_time(data["created_at"])
    elif event["type"] == "terminal.config":
        data = event["data"]
        fields(data, ("version", "print_settings", "mirror", "app_version", "stations", "updated_at"),
               ("version", "print_settings", "updated_at"))
        uuid_value(aggregate_id, "Terminal")
        if type(data["version"]) is not int or data["version"] != 1:
            raise APIError("Versão da configuração inválida")
        printers = data["print_settings"]
        if not isinstance(printers, list) or len(printers) > 4:
            raise APIError("Impressoras inválidas")
        for printer in printers:
            if not isinstance(printer, dict):
                raise APIError("Impressora inválida")
            fields(printer, ("kind", "driver", "target", "format"), ("kind", "driver", "target", "format"))
            if (printer["kind"] not in ("receipt", "label") or
                    printer["driver"] not in ("tcp", "windows_raw", "cups", "file") or
                    printer["format"] not in ("escpos58", "escpos80", "zpl", "epl") or
                    not isinstance(printer["target"], str) or len(printer["target"]) > 260):
                raise APIError("Impressora inválida")
        if "mirror" in data and (not isinstance(data["mirror"], dict) or set(data["mirror"]) != {"enabled"} or
                                 type(data["mirror"]["enabled"]) is not bool):
            raise APIError("Cópia de segurança inválida")
        if "app_version" in data and (not isinstance(data["app_version"], str) or len(data["app_version"]) > 80):
            raise APIError("Versão inválida")
        if "stations" in data and not valid_stations(data["stations"]):
            raise APIError("Caixas adicionais inválidos")
        event_time(data["updated_at"])


def _persist_config(terminal, event):
    """Keep the newest configuration; it returns to a replaced computer on restore.
    No conflict and no stock effect: an older event is stored but does not win."""
    data = event["data"]
    current = Terminal.objects.filter(pk=terminal.pk).values_list("config", flat=True).first()
    latest = current.get("updated_at") if isinstance(current, dict) else 0
    if data["updated_at"] >= (latest if type(latest) is int else 0):
        Terminal.objects.filter(pk=terminal.pk).update(config=data)
        terminal.config = data
    if data.get("stations"):
        # The first terminal of a shop to report additional registers is its main one.
        claimed = Shop.objects.filter(pk=terminal.shop_id, hub_terminal__isnull=True).update(hub_terminal=terminal)
        if claimed:
            audit(terminal.tenant, None, "terminal.hub_set", terminal,
                  {"shop_id": str(terminal.shop_id), "previous_terminal_id": None, "source": "terminal.config"})


def _persist_adjustment(terminal, event, conflicts):
    """The count at the counter already happened: a product whose registration
    changed since is flagged for the administrator instead of refused."""
    data = event["data"]
    product_id = uuid_value(data["product_id"], "Produto")
    key = f"adjustment:{terminal.id}:{event['aggregate_id']}"
    previous = StockMovement.objects.filter(tenant=terminal.tenant, source="terminal_adjustment",
                                            idempotency_key=key).first()
    if previous:
        if (previous.product_id != product_id or previous.delta_milli != data["delta_milli"] or
            previous.reason != data["reason"].strip()):
            raise APIError("Ajuste já recebido com conteúdo diferente", 409)
        return
    product = Product.objects.filter(tenant=terminal.tenant, pk=product_id).first()
    if product is None:
        raise APIError("Produto do ajuste de estoque não encontrado", 404)
    line = {"product_id": str(product.id), "product_name": product.name, "delta_milli": data["delta_milli"]}
    if not product.stock_controlled:
        conflicts.append(open_conflict(
            terminal, event, "stock_control_changed",
            "Ajuste de estoque feito no caixa para produto que não controla mais estoque. O ajuste não foi aplicado.",
            items=[line], applied=False))
        return
    row = StockMovement.objects.create(tenant=terminal.tenant, shop=terminal.shop,
                                       product=product, delta_milli=data["delta_milli"],
                                       source="terminal_adjustment", reason=data["reason"].strip(),
                                       reference=event["aggregate_id"], terminal=terminal,
                                       idempotency_key=key)
    audit(terminal.tenant, None, "stock.terminal_adjusted", row,
          {"terminal_id": str(terminal.id)})
    if product.unit == "UN" and data["delta_milli"] % 1000:
        conflicts.append(open_conflict(
            terminal, event, "unit_changed",
            "Ajuste de estoque fracionado para produto que hoje é vendido por unidade. Confira o estoque.",
            items=[{**line, "current_unit": product.unit}]))
    if not product.active:
        conflicts.append(open_conflict(
            terminal, event, "product_inactive",
            "Ajuste de estoque feito no caixa para produto desativado no cadastro.", items=[line]))


def _event_hash(event):
    try:
        return digest(event)
    except ValueError:  # NaN/Infinity: not canonical JSON, still needs a stable identity
        return hashlib.sha256(json.dumps(event, sort_keys=True).encode()).hexdigest()


def _intake(terminal, event, conflicts):
    """Apply one event. Runs inside its own savepoint; any error quarantines the event."""
    event_hash = _event_hash(event)
    previous = SyncEvent.objects.filter(event_id=event["event_id"]).first()
    if previous:
        if (previous.tenant_id != terminal.tenant_id or previous.terminal_id != terminal.id or
                previous.payload_hash != event_hash):
            raise APIError("Evento já recebido com conteúdo diferente", 409)
        # Already received: acknowledged again without judging it a second time.
        conflicts.extend(SyncConflict.objects.filter(terminal=terminal, event_id=event["event_id"])
                         .order_by("created_at", "id"))
        return
    _validate_event(event)
    if event["type"] == "terminal.config" and uuid_value(event["aggregate_id"], "Terminal") != terminal.id:
        raise APIError("Configuração de outro terminal")
    SyncEvent.objects.create(event_id=event["event_id"], tenant=terminal.tenant,
                             shop=terminal.shop, terminal=terminal,
                             event_type=event["type"], aggregate_id=event["aggregate_id"],
                             payload=event, payload_hash=event_hash)
    if event["type"] == "sale.completed":
        sale = persist_sale(terminal, event, conflicts)
        conflict = _negative_stock_conflict(terminal, event, sale)
        if conflict:
            conflicts.append(conflict)
    elif event["type"] == "sale.voided":
        void_from_terminal(terminal, event, conflicts)
    elif event["type"] == "stock.adjusted":
        _persist_adjustment(terminal, event, conflicts)
    elif event["type"] in CASH_EVENTS:
        apply_cash_event(terminal, event, conflicts)
    elif event["type"] == "sale.returned":
        apply_return(terminal, event, conflicts)
    elif event["type"] == "credit.received":
        apply_credit_receipt(terminal, event, conflicts)
    elif event["type"] == "terminal.config" and not getattr(terminal, "send_only", False):
        # A replaced computer no longer speaks for the terminal: its setup is kept
        # as an event but never replaces the configuration of the current computer.
        _persist_config(terminal, event)


# PostgreSQL refuses NUL and lone surrogates in text and jsonb (and NaN/Infinity in jsonb).
_UNSTORABLE = re.compile("[\x00\ud800-\udfff]")


def _clean(value, limit):
    return _UNSTORABLE.sub("\ufffd", str(value))[:limit]


def _storable(event):
    try:
        dumped = json.dumps(event, ensure_ascii=False, allow_nan=False)
    except ValueError:
        return False
    return "\\u0000" not in dumped and not _UNSTORABLE.search(dumped)


def _quarantine(terminal, event, error, status):
    """Keep an event that could not be applied (raw SyncEvent plus a conflict
    for the administrator) so the terminal can drop it and move on. Runs in its
    own savepoint: a payload the database refuses falls back to escaped text
    instead of aborting the whole batch."""
    event_id = event["event_id"]
    if _UNSTORABLE.search(event_id):
        event_id = "x" + hashlib.sha256(event_id.encode("utf-8", "backslashreplace")).hexdigest()[:63]
    header = {"event_id": event_id, "type": _clean(event.get("type"), 40),
              "aggregate_id": _clean(event.get("aggregate_id"), 160)}
    error = _clean(error, 500)
    escaped = {"unserializable": json.dumps(event, sort_keys=True)}
    attempts = ([event] if _storable(event) else []) + [escaped]
    for stored in attempts:
        try:
            with transaction.atomic():
                if not SyncEvent.objects.filter(event_id=event_id).exists():
                    SyncEvent.objects.create(event_id=event_id, tenant=terminal.tenant, shop=terminal.shop,
                                             terminal=terminal, event_type=header["type"],
                                             aggregate_id=header["aggregate_id"],
                                             payload=stored, payload_hash=_event_hash(event))
                return open_conflict(terminal, header, "rejected_event",
                                     f"Evento do caixa recusado pela nuvem: {error}",
                                     type=header["type"], error=error, status=status, payload=stored)
        except (OperationalError, InterfaceError):
            raise
        except Exception:
            if stored is escaped:
                raise
            logger.exception("Quarentena do evento %s não pôde guardar o payload original", event_id)


def _conflict_response(conflict):
    data = sync_conflict_data(conflict)
    # The terminal already holds the payload it sent.
    data["detail"] = {key: value for key, value in conflict.detail.items() if key != "payload"}
    return data


@csrf_exempt
@endpoint
def sync(request):
    """Outbox intake. What happened at the counter is a fact: every event is
    acknowledged, either applied (divergences become conflicts) or quarantined."""
    method(request, "POST")
    # The only route a replaced computer may still use ("send-only").
    terminal = _bearer_terminal(request, send_only_ok=True)
    data = payload(request)
    fields(data, ("events",), ("events",))
    events = data["events"]
    if not isinstance(events, list) or not 1 <= len(events) <= 50:
        raise APIError("Envie de 1 a 50 eventos")
    for event in events:
        _event_identity(event, terminal)
    accepted, conflicts, quarantined = [], [], []
    with transaction.atomic():
        Terminal.objects.select_for_update().get(pk=terminal.pk)
        for event in events:
            found = []
            try:
                with transaction.atomic():
                    _intake(terminal, event, found)
            except APIError as exc:
                found = [_quarantine(terminal, event, exc.message, exc.status)]
            except (OperationalError, InterfaceError):
                raise  # database unavailable: the terminal retries the whole batch
            except Exception:
                logger.exception("Evento %s do terminal %s falhou e foi para a quarentena",
                                 event["event_id"], terminal.id)
                found = [_quarantine(terminal, event, "Erro interno ao processar o evento", 500)]
            accepted.append(event["event_id"])
            for conflict in found:
                conflicts.append(_conflict_response(conflict))
                if conflict.reason == "rejected_event":
                    quarantined.append({"event_id": event["event_id"], "reason": conflict.reason,
                                        "message": conflict.detail.get("error", "")})
    body = {"accepted_event_ids": accepted, "conflicts": conflicts, "quarantined": quarantined,
            "send_only": terminal.send_only}
    if terminal.send_only:
        body["send_only_until"] = terminal.send_only_until.isoformat()
    return JsonResponse(body)


@csrf_exempt
@endpoint
def restore(request):
    """History of this terminal for a replaced computer, paged and resumable.
    Independent of the subscription state: the shop's own data always comes back."""
    method(request, "GET")
    terminal = _bearer_terminal(request)
    take_rate(f"restore:{terminal.pk}", 1200, 600)
    try:
        limit = int(request.GET.get("limit", str(RESTORE_PAGE_DEFAULT)))
    except ValueError:
        raise APIError("Paginação inválida")
    if not 1 <= limit <= RESTORE_PAGE_MAX:
        raise APIError("Paginação inválida")
    response = JsonResponse(restore_logic.restore_page(
        terminal, request.GET.get("cursor", ""), limit,
        history_days=RESTORE_HISTORY_DAYS, max_bytes=RESTORE_PAGE_BYTES))
    response["Cache-Control"] = "private, no-store"
    return response


@endpoint
def backup_key(request):
    """Whether the company's replica key exists and was already shown."""
    method(request, "GET")
    member = membership_context(request, roles=DEVICE_ROLES)
    tenant = member.tenant
    response = JsonResponse({"exists": bool(tenant.backup_key_encrypted), "key_id": tenant.backup_key_id,
                             "revealed_at": tenant.backup_key_revealed_at.isoformat()
                             if tenant.backup_key_revealed_at else None,
                             "can_reveal": member.role == "owner" and tenant.backup_key_revealed_at is None})
    response["Cache-Control"] = "private, no-store"
    return response


@endpoint
def backup_key_reveal(request):
    """Show the replica key to the owner, once."""
    method(request, "POST")
    member = membership_context(request, roles={"owner"}, write=True)
    data = payload(request)
    fields(data, ("confirm",), ("confirm",))
    if data["confirm"] is not True:
        raise APIError("Confirme para ver a chave")
    with transaction.atomic():
        tenant = Tenant.objects.select_for_update().get(pk=member.tenant_id)
        if tenant.backup_key_revealed_at is not None:
            raise APIError("A chave da cópia de segurança já foi exibida uma vez.", 409,
                           code="backup_key_already_revealed")
        key = tenant_backup_key(tenant)
        if key is None:
            raise APIError("A chave da cópia de segurança está indisponível no momento. Tente mais tarde.", 503,
                           code="backup_key_unavailable")
        tenant.backup_key_revealed_at = timezone.now()
        tenant.save(update_fields=["backup_key_revealed_at"])
        audit(tenant, member.user, "backup_key.revealed", tenant, {"key_id": key["key_id"]})
    response = JsonResponse({"key_id": key["key_id"], "key": key["key"],
                             "revealed_at": tenant.backup_key_revealed_at.isoformat()})
    response["Cache-Control"] = "private, no-store"
    return response


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
