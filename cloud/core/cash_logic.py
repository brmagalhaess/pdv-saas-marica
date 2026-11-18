"""Register shifts materialized from terminal cash.* events.

Events are validated by the sync endpoint before they reach this module. The
same functions rebuild history from stored SyncEvent payloads, so they must be
idempotent and must never reject an event that was already accepted.
"""
import uuid
from collections import defaultdict
from datetime import datetime, timezone as utc_timezone

from .api_common import APIError
from .models import CashMovement, CashPause, CashSession, CreditReceipt, Membership, Sale, SalePayment, SaleReturn
from .sale_logic import open_conflict
from .station_logic import station_of

CASH_EVENTS = ("cash.opened", "cash.closed", "cash.withdrawn", "cash.supplied", "cash.paused", "cash.resumed")
TOTAL_KEYS = ("sales_count", "gross_cents", "discount_cents", "net_cents", "returns_cents", "voided_count",
              "supplied_cents", "withdrawn_cents")


def validate_closing_totals(totals):
    """Totals of the operator report sent with cash.closed (terminals with "cash_shifts")."""
    if not isinstance(totals, dict) or set(totals) - set(TOTAL_KEYS) - {"by_method"}:
        raise APIError("Totais do fechamento inválidos")
    for key in TOTAL_KEYS:
        if key in totals and (type(totals[key]) is not int or abs(totals[key]) > 10**12):
            raise APIError("Totais do fechamento inválidos")
    methods = totals.get("by_method", {})
    if (not isinstance(methods, dict) or len(methods) > 40
            or any(not isinstance(key, str) or not 0 < len(key) <= 80 or type(value) is not int
                   or abs(value) > 10**12 for key, value in methods.items())):
        raise APIError("Totais do fechamento inválidos")


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
    if session.tenant_id != terminal.tenant_id or session.terminal_id != terminal.id:
        # The id is chosen by the terminal: without this check a terminal of one company could
        # rewrite (or close) the cashier of another one by sending its id. Goes to the quarantine.
        raise APIError("Caixa já recebido de outro terminal", 409)
    return session


def apply_cash_event(terminal, event, conflicts=None):
    data, kind = event["data"], event["type"]
    tenant, when = terminal.tenant, _when(data["created_at"])
    if kind == "cash.opened":
        session = _session(terminal, event["aggregate_id"], when)
        session.operator = _member(tenant, data.get("operator_id"))
        session.opening_cents = data["opening_cents"]
        session.opened_at = when
        session.incomplete = False
        session.station_id, session.station_name = station_of(event)
        session.save(update_fields=["operator", "opening_cents", "opened_at", "incomplete",
                                    "station_id", "station_name"])
    elif kind == "cash.closed":
        session = _session(terminal, event["aggregate_id"], when)
        previous = (session.closed_at, session.counted_cents, session.expected_cents)
        twice = previous[0] is not None and previous != (when, data["counted_cents"], data["expected_cents"])
        if twice and conflicts is not None:
            # After "Trocar de computador" the same shift may be closed on both computers.
            # Both counts are kept for the administrator; the latest closing is the one shown.
            conflicts.append(open_conflict(
                terminal, event, "cash_closed_twice",
                "Este caixa foi fechado duas vezes, em computadores diferentes, com valores diferentes. "
                "Vale o fechamento mais recente; confira o dinheiro da gaveta.",
                cash_session_id=str(session.id),
                first={"closed_at": previous[0].isoformat(), "counted_cents": previous[1],
                       "expected_cents": previous[2]},
                second={"closed_at": when.isoformat(), "counted_cents": data["counted_cents"],
                        "expected_cents": data["expected_cents"]}))
        if not twice or when >= previous[0]:
            # The latest closing wins whatever the order of arrival (also when history is rebuilt).
            session.closed_at = when
            session.counted_cents = data["counted_cents"]
            session.expected_cents = data["expected_cents"]
            session.difference_cents = data["difference_cents"]
            session.closed_by = _member(tenant, data.get("operator_id"))
            session.supervisor = _member(tenant, data.get("supervisor_id"))
            update = ["closed_at", "counted_cents", "expected_cents", "difference_cents", "closed_by", "supervisor"]
            if "paused_ms" in data:
                session.paused_ms = data["paused_ms"]
                update.append("paused_ms")
            if "totals" in data:
                session.closing_totals = data["totals"]
                update.append("closing_totals")
            if data.get("shift_operator_id") and session.operator_id is None:
                session.operator = _member(tenant, data["shift_operator_id"])
                update.append("operator")
            session.save(update_fields=update)
        # A pause still open when the shift closed ends with it.
        session.pauses.filter(ended_at__isnull=True).update(ended_at=when)
        if data["expected_cents"] < 0 and conflicts is not None:
            # Accepted as counted; the usual cause is a cash sale cancelled after a withdrawal.
            conflicts.append(open_conflict(
                terminal, event, "cash_expected_negative",
                "Caixa fechado com valor esperado negativo (venda em dinheiro cancelada depois de uma sangria). "
                "Confira o dinheiro da gaveta.",
                cash_session_id=str(session.id), expected_cents=data["expected_cents"],
                counted_cents=data["counted_cents"], difference_cents=data["difference_cents"]))
    elif kind in ("cash.paused", "cash.resumed"):
        session = _session(terminal, data["cash_session_id"], when)
        pause_id = uuid.UUID(str(event["aggregate_id"]))
        pause = CashPause.objects.filter(pk=pause_id).first()
        if pause is not None and pause.session_id != session.id:
            raise APIError("Pausa de caixa já recebida de outro caixa", 409)
        if kind == "cash.paused":
            if pause is None:
                CashPause.objects.create(id=pause_id, session=session, operator=_member(tenant, data["operator_id"]),
                                         reason=str(data.get("reason") or "").strip()[:120], started_at=when)
            elif pause.operator_id is None and pause.started_at == pause.ended_at and when <= pause.ended_at:
                # The resumption arrived first (history rebuilt in another order): now the start is known.
                pause.started_at, pause.operator = when, _member(tenant, data["operator_id"])
                pause.reason = str(data.get("reason") or "").strip()[:120]
                pause.save(update_fields=["started_at", "operator", "reason"])
        elif pause is None:
            # The pause itself never arrived (older copy, quarantine): keep the resumption as a point in time.
            CashPause.objects.create(id=pause_id, session=session, started_at=when, ended_at=when,
                                     resumed_by=_member(tenant, data["operator_id"]))
        elif pause.ended_at is None or pause.ended_at > when:
            pause.ended_at = when
            pause.resumed_by = _member(tenant, data["operator_id"])
            pause.save(update_fields=["ended_at", "resumed_by"])
    else:
        session = _session(terminal, data["cash_session_id"], when)
        existing = CashMovement.objects.filter(pk=uuid.UUID(str(event["aggregate_id"]))).select_related("session").first()
        if existing is not None and existing.session_id != session.id:
            raise APIError("Movimento de caixa já recebido de outro caixa", 409)
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
    refunds = sum(SaleReturn.objects.filter(tenant=session.tenant, refund_method="cash",
                                            cash_session_id__in=(str(session.id), session.id.hex))
                  .values_list("total_cents", flat=True))
    by_method["cash"] = by_method.get("cash", 0) - change - refunds
    credit_received = defaultdict(int)
    for method, amount in (CreditReceipt.objects.filter(tenant=session.tenant, terminal=session.terminal,
                                                        cash_session_id__in=(str(session.id), session.id.hex))
                           .values_list("method", "amount_cents")):
        credit_received[method] += amount
    movements = defaultdict(int)
    for kind, amount in session.movements.values_list("kind", "amount_cents"):
        movements[kind] += amount
    return {"sales_count": sales.count(),
            "sales_total_cents": sum(sales.values_list("total_cents", flat=True)),
            "by_method": {key: value for key, value in by_method.items() if value},
            "supplied_cents": movements["supplied"], "withdrawn_cents": movements["withdrawn"],
            "cash_refunds_cents": refunds, "credit_received": dict(credit_received)}
