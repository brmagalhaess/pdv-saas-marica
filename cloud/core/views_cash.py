from datetime import date

from django.http import JsonResponse
from django.utils import timezone

from .api_common import APIError, endpoint, iso, membership_context, method, page, scoped, uuid_value
from .cash_logic import session_summary
from .models import CashSession, Shop, Terminal
from .station_logic import MAIN_STATION_ID, STATION_ID

READ = {"owner", "admin", "manager", "finance", "viewer"}


def _name(member):
    return member.name if member else ""


def cash_session_data(row, detail=False):
    result = {"id": str(row.id), "shop_id": str(row.shop_id), "terminal_id": str(row.terminal_id),
              "terminal_name": row.terminal.name, "operator_name": _name(row.operator),
              "station_id": row.station_id, "station_name": row.station_name,
              "opening_cents": row.opening_cents, "opened_at": iso(row.opened_at),
              "closed_at": iso(row.closed_at), "status": "closed" if row.closed_at else "open",
              "closed_by_name": _name(row.closed_by), "supervisor_name": _name(row.supervisor),
              "counted_cents": row.counted_cents, "expected_cents": row.expected_cents,
              "difference_cents": row.difference_cents, "incomplete": row.incomplete,
              "paused_ms": row.paused_ms, "closing_totals": row.closing_totals or {}}
    pauses = list(row.pauses.select_related("operator", "resumed_by").order_by("started_at", "id"))
    result["pauses_count"] = len(pauses)
    result["paused_now"] = any(pause.ended_at is None for pause in pauses)
    if not row.paused_ms and pauses:
        # Open shift (or older closing without the total): add up what arrived.
        end = row.closed_at
        result["paused_ms"] = sum(int(((pause.ended_at or end or timezone.now()) - pause.started_at)
                                      .total_seconds() * 1000) for pause in pauses)
    result.update(session_summary(row))
    if detail:
        result["pauses"] = [{"id": str(p.id), "reason": p.reason, "operator_name": _name(p.operator),
                             "resumed_by_name": _name(p.resumed_by), "started_at": iso(p.started_at),
                             "ended_at": iso(p.ended_at)} for p in pauses]
        result["movements"] = [{"id": str(m.id), "kind": m.kind, "amount_cents": m.amount_cents,
                                "reason": m.reason, "operator_name": _name(m.operator),
                                "created_at": iso(m.created_at)}
                               for m in row.movements.select_related("operator").order_by("created_at")]
    return result


def _date(value, label):
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        raise APIError(f"{label} inválida")


@endpoint
def cash_sessions(request):
    method(request, "GET")
    member = membership_context(request, roles=READ)
    rows = (CashSession.objects.select_related("terminal", "operator", "closed_by", "supervisor")
            .filter(tenant=member.tenant))
    if request.GET.get("shop_id"):
        rows = rows.filter(shop=scoped(Shop, member.tenant, request.GET["shop_id"]))
    if request.GET.get("terminal_id"):
        rows = rows.filter(terminal=scoped(Terminal, member.tenant, request.GET["terminal_id"]))
    station = request.GET.get("station_id")
    if station:
        if not STATION_ID.fullmatch(station):
            raise APIError("Caixa inválido")
        # Shifts of the main register sent by terminals older than "stations" carry no label.
        rows = rows.filter(station_id__in=(station, "") if station == MAIN_STATION_ID else (station,))
    status = request.GET.get("status")
    if status == "open":
        rows = rows.filter(closed_at__isnull=True)
    elif status == "closed":
        rows = rows.filter(closed_at__isnull=False)
    elif status == "shortage":
        rows = rows.filter(difference_cents__lt=0)
    elif status:
        raise APIError("Filtro de estado inválido")
    if request.GET.get("from"):
        rows = rows.filter(opened_at__date__gte=_date(request.GET["from"], "Data inicial"))
    if request.GET.get("to"):
        rows = rows.filter(opened_at__date__lte=_date(request.GET["to"], "Data final"))
    return page(request, rows.order_by("-opened_at", "-id"), cash_session_data)


@endpoint
def cash_session_detail(request, pk):
    method(request, "GET")
    member = membership_context(request, roles=READ)
    row = (CashSession.objects.select_related("terminal", "operator", "closed_by", "supervisor")
           .filter(tenant=member.tenant, pk=uuid_value(pk, "Caixa")).first())
    if row is None:
        raise APIError("Caixa não encontrado", 404)
    return JsonResponse(cash_session_data(row, detail=True))
