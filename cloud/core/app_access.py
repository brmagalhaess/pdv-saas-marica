"""Granting and revoking PDV2 Gestão access: rules and JSON shared by the web panel and the app."""
import logging
import secrets
import uuid
from datetime import date, timedelta

from django.contrib.auth import get_user_model
from django.core.mail import send_mail
from django.db import IntegrityError, transaction
from django.db.models import Count, F, Q
from django.utils import timezone

from .api_common import PLATFORM_GROUPS, APIError, audit, text
from .app_auth import (ALL_PERMISSIONS, active_access_q, fail, platform_eligible, revoke_device,
                       revoke_user_devices, tenant_accesses)
from .models import AccountDeletionRequest, AppAccess, AppDevice, Membership, PlatformMFA, Shop, Tenant
from .monitoring import _midnight, local_iso

logger = logging.getLogger(__name__)

PERMISSION_TEXT = {"sales": "Vendas", "cash": "Caixa", "stock": "Estoque", "credit": "Fiado",
                   "terminals": "Terminais e conflitos"}
ACCESS_STATUS_TEXT = {"active": "Ativo", "expired": "Expirado", "revoked": "Revogado"}
DELETION_STATUS_TEXT = {"pending": "Pendente", "done": "Atendido", "rejected": "Recusado"}
ROLE_TEXT = {"owner": "Proprietário", "admin": "Administrador", "manager": "Gerente",
             "cashier": "Operador de caixa", "inventory": "Estoque", "finance": "Financeiro", "viewer": "Consulta"}
NOT_STAFF_MESSAGE = ("Essa pessoa não é da equipe CrownTech. Dê um papel de plataforma em "
                     "Usuários e permissões primeiro.")
# The e-mail sent when the request is done is best effort (SMTP may be down), so it is not promised here.
DELETION_MESSAGE = ("Recebemos seu pedido. Seu login será desativado e seus dados pessoais apagados em até "
                    "15 dias.")
WEB_IDENTITY_MESSAGE = ("Pedido feito pelo site, sem login: confirme com a pessoa pelo e-mail da conta "
                        "que o pedido é dela antes de atender.")
DELETED_NAME = "Conta excluída"
DELETED_DEVICE_NAME = "Aparelho removido"


def user_ref(user):
    if user is None:
        return None
    return {"id": user.pk, "name": user.get_full_name() or user.username, "email": user.email}


def shop_ref(shop):
    return {"id": str(shop.id), "name": shop.name}


def tenant_ref(tenant):
    return {"id": str(tenant.id), "name": tenant.name}


def access_status(row, now=None):
    if row.revoked_at is not None:
        return "revoked"
    if row.expires_at is not None and row.expires_at <= (now or timezone.now()):
        return "expired"
    return "active"


def expires_on(expires_at):
    """`expires_at` is the midnight after the last valid day ("valid until the end of the day")."""
    if expires_at is None:
        return None
    return (timezone.localtime(expires_at) - timedelta(days=1)).date().isoformat()


def expires_at_from(day):
    return None if day is None else _midnight(day + timedelta(days=1))


def active_device_counts(user_ids):
    return dict(AppDevice.objects.filter(user_id__in=user_ids, revoked_at__isnull=True, activated_at__isnull=False)
                .values_list("user_id").annotate(v=Count("id")))


def access_dict(row, devices_active=None):
    platform = row.scope == "platform"
    shops = [] if platform else sorted(row.shops.all(), key=lambda shop: (shop.name, str(shop.id)))
    status = access_status(row)
    if devices_active is None:
        devices_active = active_device_counts([row.user_id]).get(row.user_id, 0)
    return {"id": str(row.id), "scope": row.scope, "user": user_ref(row.user),
            "tenant": None if platform else tenant_ref(row.tenant),
            "all_shops": platform or not shops, "shops": [shop_ref(shop) for shop in shops],
            "permissions": list(ALL_PERMISSIONS) if platform else
            [value for value in ALL_PERMISSIONS if value in (row.permissions or [])],
            "reason": row.reason, "granted_by": user_ref(row.granted_by), "granted_at": local_iso(row.granted_at),
            "expires_on": expires_on(row.expires_at), "status": status, "status_text": ACCESS_STATUS_TEXT[status],
            "revoked_at": local_iso(row.revoked_at), "revoked_by": user_ref(row.revoked_by),
            "revoke_reason": row.revoke_reason, "devices_active": devices_active}


def access_dicts(rows):
    rows = list(rows)
    counts = active_device_counts({row.user_id for row in rows})
    return [access_dict(row, counts.get(row.user_id, 0)) for row in rows]


def access_queryset():
    return AppAccess.objects.select_related("user", "tenant", "granted_by", "revoked_by").prefetch_related("shops")


def device_status(row):
    return "revoked" if row.revoked_at is not None else "active"


def platform_device_dict(row):
    return {"id": str(row.id), "platform": row.platform, "name": row.name, "model": row.model,
            "os_version": row.os_version, "app_version": row.app_version, "created_at": local_iso(row.created_at),
            "last_seen_at": local_iso(row.last_seen_at), "last_ip": row.last_ip, "user": user_ref(row.user),
            "status": device_status(row), "revoked_at": local_iso(row.revoked_at), "revoke_reason": row.revoke_reason}


def deletion_dict(row):
    return {"id": str(row.id), "email": row.email, "user": user_ref(row.user), "source": row.source,
            "note": row.note, "status": row.status, "status_text": DELETION_STATUS_TEXT[row.status],
            "created_at": local_iso(row.created_at), "handled_at": local_iso(row.handled_at),
            "handled_by": user_ref(row.handled_by), "handled_note": row.handled_note}


# Filters of the listings -----------------------------------------------------

def search_text(params):
    value = params.get("q", "").strip()
    if len(value) > 120:
        fail("invalid_request", "Busca muito longa.")
    return value


def choice(params, name, allowed, default):
    value = params.get(name, default) or default
    if value not in allowed:
        fail("invalid_request")
    return value


def filtered_accesses(params):
    rows = access_queryset()
    if choice(params, "status", ("active", "all"), "active") == "active":
        rows = rows.filter(active_access_q())
    if params.get("tenant_id"):
        try:
            rows = rows.filter(tenant_id=parse_uuid(params["tenant_id"]))
        except APIError:
            fail("invalid_request", "Empresa inválida.")
    q = search_text(params)
    if q:
        rows = rows.filter(Q(user__email__icontains=q) | Q(user__first_name__icontains=q) |
                           Q(user__last_name__icontains=q) | Q(tenant__name__icontains=q))
    return rows.order_by("-granted_at", "-id")


def filtered_devices(params):
    rows = AppDevice.objects.select_related("user").filter(activated_at__isnull=False)
    if choice(params, "status", ("active", "all"), "active") == "active":
        rows = rows.filter(revoked_at__isnull=True)
    q = search_text(params)
    if q:
        rows = rows.filter(Q(user__email__icontains=q) | Q(user__first_name__icontains=q) |
                           Q(user__last_name__icontains=q) | Q(name__icontains=q) | Q(model__icontains=q))
    return rows.order_by(F("last_seen_at").desc(nulls_last=True), "-created_at", "-id")


def filtered_deletions(params):
    rows = AccountDeletionRequest.objects.select_related("user", "handled_by")
    if choice(params, "status", ("pending", "all"), "pending") == "pending":
        rows = rows.filter(status="pending")
    return rows.order_by("-created_at", "-id")


# Validation ------------------------------------------------------------------

def parse_uuid(value):
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        fail("invalid_request")


def only_keys(data, allowed, required=()):
    if set(data) - set(allowed) or any(key not in data for key in required):
        fail("invalid_request")


def parse_reason(value, name="Motivo"):
    if not isinstance(value, str) or not 3 <= len(value.strip()) <= 240:
        fail("invalid_request", f"{name}: escreva de 3 a 240 caracteres.")
    return value.strip()


def parse_permissions(value):
    if (not isinstance(value, list) or not value or len(set(map(str, value))) != len(value) or
            any(not isinstance(item, str) or item not in ALL_PERMISSIONS for item in value)):
        fail("invalid_request", "Escolha ao menos uma permissão válida, sem repetir.")
    return [item for item in ALL_PERMISSIONS if item in value]


def parse_expires_on(value):
    if value is None:
        return None
    try:
        day = date.fromisoformat(value) if isinstance(value, str) and len(value) == 10 else None
    except ValueError:
        day = None
    if day is None or day < timezone.localdate():
        fail("invalid_request", "Validade inválida: use uma data de hoje em diante.")
    return day


def parse_shops(tenant, value):
    if not isinstance(value, list) or len(value) > 500:
        fail("invalid_request", "Lojas inválidas.")
    ids = {parse_uuid(item) for item in value}
    shops = list(Shop.objects.filter(tenant=tenant, active=True, pk__in=ids))
    if len(shops) != len(ids):
        fail("invalid_request", "Escolha apenas lojas ativas desta empresa.")
    return shops


def _person(user_id):
    if type(user_id) is not int:
        fail("invalid_request", "Pessoa inválida.")
    user = get_user_model().objects.filter(pk=user_id).first()
    if user is None:
        fail("not_found", "Pessoa não encontrada.")
    if not user.is_active:
        fail("conflict", "O login dessa pessoa está desativado.")
    return user


def _replace_expired(actor, existing):
    """An expired (not revoked) access is closed before a new release for the same person."""
    if existing is None:
        return
    if access_status(existing) == "active":
        fail("access_exists", access_id=str(existing.id))
    existing.revoked_at, existing.revoked_by = timezone.now(), actor
    existing.revoke_reason = "Substituído por nova liberação"
    existing.save(update_fields=["revoked_at", "revoked_by", "revoke_reason", "updated_at"])


def grant_access(actor, data):
    if not isinstance(data.get("scope"), str) or data["scope"] not in ("tenant", "platform"):
        fail("invalid_request", "Tipo de acesso inválido.")
    if data["scope"] == "platform":
        only_keys(data, ("scope", "user_id", "expires_on", "reason"), ("scope", "user_id", "reason"))
    else:
        only_keys(data, ("scope", "user_id", "tenant_id", "shop_ids", "permissions", "expires_on", "reason"),
                  ("scope", "user_id", "tenant_id", "permissions", "reason"))
    reason = parse_reason(data["reason"])
    day = parse_expires_on(data.get("expires_on"))
    user = _person(data["user_id"])
    try:
        with transaction.atomic():
            if data["scope"] == "platform":
                if user.is_superuser:
                    fail("superuser_implicit")
                if not (user.is_staff and user.groups.filter(name__in=PLATFORM_GROUPS).exists()):
                    fail("member_required", NOT_STAFF_MESSAGE)
                _replace_expired(actor, AppAccess.objects.select_for_update().filter(
                    user=user, scope="platform", revoked_at__isnull=True).first())
                row = AppAccess.objects.create(user=user, scope="platform", permissions=list(ALL_PERMISSIONS),
                                               granted_by=actor, reason=reason, expires_at=expires_at_from(day))
                tenant, detail = None, {"scope": "platform", "user_id": user.pk, "reason": reason,
                                        "expires_on": day.isoformat() if day else None}
            else:
                tenant = Tenant.objects.filter(pk=parse_uuid(data["tenant_id"])).first()
                if tenant is None:
                    fail("not_found", "Empresa não encontrada.")
                permissions = parse_permissions(data["permissions"])
                shops = parse_shops(tenant, data.get("shop_ids", []))
                if not Membership.objects.filter(user=user, tenant=tenant, active=True).exists():
                    fail("member_required")
                _replace_expired(actor, AppAccess.objects.select_for_update().filter(
                    user=user, tenant=tenant, scope="tenant", revoked_at__isnull=True).first())
                row = AppAccess.objects.create(user=user, scope="tenant", tenant=tenant, permissions=permissions,
                                               granted_by=actor, reason=reason, expires_at=expires_at_from(day))
                row.shops.set(shops)
                detail = {"scope": "tenant", "user_id": user.pk, "reason": reason, "permissions": permissions,
                          "shop_ids": sorted(str(shop.id) for shop in shops),
                          "expires_on": day.isoformat() if day else None}
            audit(tenant, actor, "app.access_granted", row, detail)
    except IntegrityError:
        existing = AppAccess.objects.filter(user=user, revoked_at__isnull=True, scope=data["scope"],
                                            tenant_id=None if data["scope"] == "platform" else data.get("tenant_id")).first()
        fail("access_exists", **({"access_id": str(existing.id)} if existing else {}))
    return access_queryset().get(pk=row.pk)


def update_access(actor, row, data):
    if row.scope == "platform":
        only_keys(data, ("expires_on", "reason"))
    else:
        only_keys(data, ("shop_ids", "permissions", "expires_on", "reason"))
    if row.revoked_at is not None:
        fail("conflict", "Este acesso foi revogado. Libere de novo, se precisar.")
    changed = {}
    with transaction.atomic():
        if "reason" in data:
            row.reason = parse_reason(data["reason"])
            changed["reason"] = row.reason
        if "expires_on" in data:
            day = parse_expires_on(data["expires_on"])
            row.expires_at = expires_at_from(day)
            changed["expires_on"] = day.isoformat() if day else None
        if "permissions" in data:
            row.permissions = parse_permissions(data["permissions"])
            changed["permissions"] = row.permissions
        row.save()
        if "shop_ids" in data:
            shops = parse_shops(row.tenant, data["shop_ids"])
            row.shops.set(shops)
            changed["shop_ids"] = sorted(str(shop.id) for shop in shops)
        audit(row.tenant, actor, "app.access_updated", row, changed)
    return access_queryset().get(pk=row.pk)


def still_eligible(user):
    return platform_eligible(user) or tenant_accesses(user).exists()


def revoke_access(actor, row, data):
    only_keys(data, ("reason",), ("reason",))
    reason = parse_reason(data["reason"])
    with transaction.atomic():
        row = AppAccess.objects.select_for_update().get(pk=row.pk)
        if row.revoked_at is not None:
            fail("conflict", "Este acesso já estava revogado.")
        row.revoked_at, row.revoked_by, row.revoke_reason = timezone.now(), actor, reason
        row.save(update_fields=["revoked_at", "revoked_by", "revoke_reason", "updated_at"])
        devices = 0
        if not still_eligible(row.user):
            devices = revoke_user_devices(row.user, "access_revoked", actor)
        audit(row.tenant, actor, "app.access_revoked", row, {"reason": reason, "devices_revoked": devices})
    return access_queryset().get(pk=row.pk)


def revoke_any_device(actor, device, data):
    only_keys(data, ("reason",), ("reason",))
    reason = parse_reason(data["reason"])
    if device.revoked_at is not None:
        fail("conflict", "Este aparelho já estava desconectado.")
    revoke_device(device, reason, actor)
    audit(None, actor, "app.device_revoked", device, {"reason": reason, "user_id": device.user_id, "by": "platform"})
    device.refresh_from_db()
    return device


def access_options(tenant_id):
    User = get_user_model()
    if tenant_id:
        tenant = Tenant.objects.filter(pk=parse_uuid(tenant_id)).first()
        if tenant is None:
            fail("not_found", "Empresa não encontrada.")
        active_users = set(AppAccess.objects.filter(active_access_q(), scope="tenant", tenant=tenant)
                           .values_list("user_id", flat=True))
        members = (Membership.objects.select_related("user")
                   .filter(tenant=tenant, active=True, user__is_active=True).order_by("name", "id"))
        return {"tenant": tenant_ref(tenant),
                "shops": [shop_ref(shop) for shop in Shop.objects.filter(tenant=tenant, active=True).order_by("name", "id")],
                "members": [{"user_id": member.user_id, "name": member.name or member.user.get_full_name() or member.user.email,
                             "email": member.user.email, "role": member.role,
                             "role_text": ROLE_TEXT.get(member.role, member.role),
                             "has_active_access": member.user_id in active_users} for member in members]}
    active_users = set(AppAccess.objects.filter(active_access_q(), scope="platform").values_list("user_id", flat=True))
    staff = (User.objects.filter(is_active=True, is_staff=True, is_superuser=False, groups__name__in=PLATFORM_GROUPS)
             .distinct().order_by("first_name", "email", "pk"))
    return {"tenant": None, "shops": [],
            "members": [{"user_id": user.pk, "name": user.get_full_name() or user.email, "email": user.email,
                         "role": "platform", "role_text": "Equipe CrownTech",
                         "has_active_access": user.pk in active_users} for user in staff]}


# Account deletion ------------------------------------------------------------

def request_deletion(user, email, source, note, ip):
    """Register the request once (a pending one is reused); from the app, also sign out every phone."""
    if user is not None:
        if source == "app":
            # Only the signed-in person signs out every phone; the public form cannot.
            revoke_user_devices(user, "account_deletion", user)
        pending = AccountDeletionRequest.objects.filter(user=user, status="pending").first()
    else:
        pending = AccountDeletionRequest.objects.filter(email__iexact=email, status="pending").first()
    if pending is None:
        pending = AccountDeletionRequest.objects.create(user=user, email=email[:254], source=source,
                                                        note=note[:500], ip=ip[:64])
    audit(None, user, "app.account_deletion_requested", pending, {"source": source})
    return pending


def mask_email(email):
    """Keep only enough of the address to recognize the request later ("a***@dominio.com")."""
    local, _, domain = (email or "").partition("@")
    return f"{local[:1]}***@{domain}" if domain else "***"


def erase_personal_data(user, actor):
    """Deletion of the person's account (LGPD): disable the login and wipe what identifies them.

    The user row itself stays (sales, cash sessions and audit events point to it with PROTECT and
    are company records the law keeps), but it no longer holds a name, e-mail or usable password.
    Devices keep only their token hashes, so a phone still signed in gets "device_revoked".
    """
    now = timezone.now()
    placeholder = f"excluido-{secrets.token_hex(8)}@conta-excluida.invalid"
    user.username = user.email = placeholder
    user.first_name = user.last_name = ""
    user.is_active = False
    user.set_unusable_password()
    user.save(update_fields=["username", "email", "first_name", "last_name", "is_active", "password"])
    AppAccess.objects.filter(user=user, revoked_at__isnull=True).update(
        revoked_at=now, revoked_by=actor, revoke_reason="Exclusão de conta")
    revoke_user_devices(user, "account_deletion", actor)
    AppDevice.objects.filter(user=user).update(
        name=DELETED_DEVICE_NAME, model="", os_version="", last_ip="", push_provider="", push_token=None,
        mfa_challenge_hash=None, mfa_challenge_expires_at=None)
    memberships = Membership.objects.filter(user=user).update(active=False, name=DELETED_NAME, pin_hash="")
    PlatformMFA.objects.filter(user=user).delete()
    return {"memberships_disabled": memberships}


def _send_deletion_done_email(email):
    """Best effort: the deletion is done even when the mail server is down."""
    try:
        send_mail("PDV2: sua conta foi excluída",
                  "Olá. Atendemos seu pedido: seu login no PDV2 (aplicativo e painel) foi desativado e seus "
                  "dados pessoais foram apagados. Os dados da empresa, como vendas e notas, continuam com a "
                  "empresa, como exige a lei.\n\nPDV2 · CrownTech",
                  None, [email], fail_silently=False)
    except Exception:  # noqa: BLE001 - never undo a finished deletion because of e-mail
        logger.warning("account deletion e-mail not sent", exc_info=True)


def resolve_deletion(actor, row, data):
    only_keys(data, ("status", "note", "identity_confirmed"), ("status",))
    if data["status"] not in ("done", "rejected"):
        fail("invalid_request", "Situação inválida.")
    confirmed = data.get("identity_confirmed", False)
    if not isinstance(confirmed, bool):
        fail("invalid_request", "Confirmação inválida.")
    note = text(data.get("note", ""), "Observação", 500, optional=True)
    notify = None
    with transaction.atomic():
        row = AccountDeletionRequest.objects.select_for_update(of=("self",)).select_related("user").get(pk=row.pk)
        if row.status != "pending":
            fail("conflict", "Este pedido já foi atendido.")
        user = row.user
        detail = {"status": data["status"], "user_id": user.pk if user else None, "source": row.source}
        if data["status"] == "done":
            if user is not None:
                if user.is_superuser:
                    fail("conflict", "A conta principal do SaaS não pode ser desativada por aqui.")
                # Anyone can type an e-mail in the public form: only the signed-in app request proves
                # who asked. A web request is done only after the admin confirmed it with the owner.
                if row.source == "web" and not confirmed:
                    fail("conflict", WEB_IDENTITY_MESSAGE)
                notify = user.email
                detail.update(erase_personal_data(user, actor))
            detail["identity_confirmed"] = row.source == "app" or confirmed
            # Every request of this person keeps no readable e-mail, note or IP.
            same = AccountDeletionRequest.objects.filter(Q(pk=row.pk) | (Q(user=user) if user else Q(pk=row.pk)))
            for other in same:
                other.email, other.note, other.ip = mask_email(other.email), "", ""
                other.save(update_fields=["email", "note", "ip"])
            row.refresh_from_db()
        row.status, row.handled_by, row.handled_at, row.handled_note = data["status"], actor, timezone.now(), note
        row.save(update_fields=["status", "handled_by", "handled_at", "handled_note"])
        audit(None, actor, "app.account_deletion_resolved", row, detail)
        if notify:
            transaction.on_commit(lambda: _send_deletion_done_email(notify))
    return AccountDeletionRequest.objects.select_related("user", "handled_by").get(pk=row.pk)
