"""Rebuild register shifts and sale→shift links from already received events."""
import uuid
from datetime import datetime, timezone

from django.db import migrations


def _when(value):
    return datetime.fromtimestamp(value / 1000, tz=timezone.utc)


def backfill(apps, schema_editor):
    SyncEvent = apps.get_model("core", "SyncEvent")
    Sale = apps.get_model("core", "Sale")
    CashSession = apps.get_model("core", "CashSession")
    CashMovement = apps.get_model("core", "CashMovement")
    Membership = apps.get_model("core", "Membership")

    def member(tenant_id, pk):
        try:
            return Membership.objects.filter(tenant_id=tenant_id, pk=uuid.UUID(str(pk))).first() if pk else None
        except ValueError:
            return None

    def session(row, session_id, when):
        obj, _ = CashSession.objects.get_or_create(
            pk=uuid.UUID(str(session_id)),
            defaults={"tenant_id": row.tenant_id, "shop_id": row.shop_id,
                      "terminal_id": row.terminal_id, "opened_at": when, "incomplete": True})
        return obj

    events = SyncEvent.objects.filter(event_type__in=(
        "cash.opened", "cash.closed", "cash.withdrawn", "cash.supplied", "sale.completed")).order_by("received_at")
    for row in events.iterator():
        data = row.payload.get("data", {})
        if row.event_type == "sale.completed":
            Sale.objects.filter(pk=row.aggregate_id, cash_session_id="").update(
                cash_session_id=data.get("cash_session_id", ""))
            continue
        when = _when(data["created_at"])
        if row.event_type == "cash.opened":
            obj = session(row, row.aggregate_id, when)
            obj.operator = member(row.tenant_id, data.get("operator_id"))
            obj.opening_cents, obj.opened_at, obj.incomplete = data["opening_cents"], when, False
            obj.save()
        elif row.event_type == "cash.closed":
            obj = session(row, row.aggregate_id, when)
            obj.closed_at, obj.counted_cents = when, data["counted_cents"]
            obj.expected_cents, obj.difference_cents = data["expected_cents"], data["difference_cents"]
            obj.save()
        else:
            obj = session(row, data["cash_session_id"], when)
            CashMovement.objects.get_or_create(
                pk=uuid.UUID(str(row.aggregate_id)),
                defaults={"session": obj, "kind": row.event_type.split(".", 1)[1],
                          "amount_cents": data["amount_cents"], "reason": data["reason"].strip(),
                          "operator": member(row.tenant_id, data.get("operator_id")), "created_at": when})


class Migration(migrations.Migration):
    dependencies = [("core", "0023_cash_sessions")]
    operations = [migrations.RunPython(backfill, migrations.RunPython.noop)]
