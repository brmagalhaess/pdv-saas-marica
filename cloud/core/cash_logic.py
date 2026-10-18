"""Register shifts materialized from terminal cash.* events.

Events are validated by the sync endpoint before they reach this module. The
same functions rebuild history from stored SyncEvent payloads, so they must be
idempotent and must never reject an event that was already accepted.
"""
import uuid
from collections import defaultdict
from datetime import datetime, timezone as utc_timezone

from .models import CashMovement, CashSession, Membership, Sale, SalePayment

CASH_EVENTS = ("cash.opened", "cash.closed", "cash.withdrawn", "cash.supplied")


def _when(value):
    return datetime.fromtimestamp(value / 1000, tz=utc_timezone.utc)


def _member(tenant, pk):
    if not pk:
        return None
    try:
        return Membership.objects.filter(tenant=tenant, pk=uuid.UUID(str(pk))).first()
    except ValueError:
        return None


def _session(terminal, session_id, when):
    session, _ = CashSession.objects.get_or_create(
        pk=uuid.UUID(str(session_id)),
        defaults={"tenant": terminal.tenant, "shop": terminal.shop, "terminal": terminal,
                  "opened_at": when, "incomplete": True})
    return session


def apply_cash_event(terminal, event):
    data, kind = event["data"], event["type"]
    tenant, when = terminal.tenant, _when(data["created_at"])
    if kind == "cash.opened":
        session = _session(terminal, event["aggregate_id"], when)
        session.operator = _member(tenant, data.get("operator_id"))
        session.opening_cents = data["opening_cents"]
        session.opened_at = when
        session.incomplete = False
        session.save(update_fields=["operator", "opening_cents", "opened_at", "incomplete"])
    elif kind == "cash.closed":
        session = _session(terminal, event["aggregate_id"], when)
        session.closed_at = when
        session.counted_cents = data["counted_cents"]
        session.expected_cents = data["expected_cents"]
        session.difference_cents = data["difference_cents"]
        session.closed_by = _member(tenant, data.get("operator_id"))
        session.supervisor = _member(tenant, data.get("supervisor_id"))
        session.save(update_fields=["closed_at", "counted_cents", "expected_cents",
                                    "difference_cents", "closed_by", "supervisor"])
    else:
        session = _session(terminal, data["cash_session_id"], when)
        CashMovement.objects.get_or_create(
            pk=uuid.UUID(str(event["aggregate_id"])),
            defaults={"session": session, "kind": kind.split(".", 1)[1],
                      "amount_cents": data["amount_cents"], "reason": data["reason"].strip(),
                      "operator": _member(tenant, data.get("operator_id")), "created_at": when})
    return session


def session_summary(session):
    """Totals by payment method for completed sales of one shift."""
    sales = Sale.objects.filter(tenant=session.tenant, terminal=session.terminal,
                                cash_session_id__in=(str(session.id), session.id.hex), status="completed")
    by_method = defaultdict(int)
    for method, amount in SalePayment.objects.filter(sale__in=sales).values_list("method", "amount_cents"):
        by_method[method] += amount
    change = sum(sales.values_list("change_cents", flat=True))
    by_method["cash"] = by_method.get("cash", 0) - change
    movements = defaultdict(int)
    for kind, amount in session.movements.values_list("kind", "amount_cents"):
        movements[kind] += amount
    return {"sales_count": sales.count(),
            "sales_total_cents": sum(sales.values_list("total_cents", flat=True)),
            "by_method": {key: value for key, value in by_method.items() if value},
            "supplied_cents": movements["supplied"], "withdrawn_cents": movements["withdrawn"]}
