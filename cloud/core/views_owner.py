"""Read-only owner monitoring API used by the PDV2 Monitor iOS app.

The endpoint deliberately returns one tenant-scoped snapshot.  It is kept
separate from the operational dashboard so the mobile client never receives
write-capable controls or an overly broad admin payload.
"""

from django.db.models import Count, Max, Sum
from django.http import JsonResponse
from django.utils import timezone

from .api_common import endpoint, iso, membership_context, method, require_feature
from .models import FinanceEntry, Sale, Terminal
from .monitoring import ONLINE_WINDOW, method_totals, period_window


def _day_window():
    """Return today's local timezone aware half-open interval."""
    window = period_window("today")
    return window["first"], window["start"], window["end"]


def _payment_totals(sales):
    """Money received by method: cash net of change; fiado and store credit left out.

    Kept in the legacy shape of the endpoint (cash/pix/card_credit/card_debit/other).
    """
    totals = method_totals(sales)
    return {key: totals[key] for key in ("cash", "pix", "card_credit", "card_debit", "other")}


@endpoint
def monitoring(request):
    """Return the owner-only operational and financial snapshot for today."""
    method(request, "GET")
    member = membership_context(request, roles={"owner"})
    require_feature(member, "cloud_dashboard")
    tenant = member.tenant
    now = timezone.now()
    online_after = now - ONLINE_WINDOW
    today, start, end = _day_window()

    # Revoked terminals are left out of the list too, so it matches terminals_total.
    terminals = list(Terminal.objects.select_related("shop")
                     .filter(tenant=tenant, revoked=False).order_by("name", "id"))
    active_terminals = terminals
    online_ids = {row.id for row in active_terminals
                  if row.last_seen_at and row.last_seen_at >= online_after}

    # The day a sale was made, not the day the cloud received it (offline sales sync later).
    sales = Sale.objects.filter(tenant=tenant, status="completed",
                                created_at__gte=start, created_at__lt=end)
    summary = sales.aggregate(count=Count("id"), revenue=Sum("total_cents"))
    count = summary["count"] or 0
    revenue = summary["revenue"] or 0

    by_terminal = (sales.values("terminal_id", "terminal__name", "shop__name")
                   .annotate(sales_count=Count("id"), revenue_cents=Sum("total_cents"),
                             last_sale_at=Max("created_at"))
                   .order_by("-revenue_cents", "terminal__name"))
    sales_by_terminal = [{
        "terminal_id": str(row["terminal_id"]) if row["terminal_id"] else None,
        "terminal_name": row["terminal__name"] or "Terminal sem identificação",
        "shop_name": row["shop__name"] or "",
        "sales_count": row["sales_count"] or 0,
        "revenue_cents": row["revenue_cents"] or 0,
        "last_sale_at": iso(row["last_sale_at"]),
    } for row in by_terminal]

    finance_enabled = "finance" in tenant.subscription.plan.features
    payables = receivables = 0
    if finance_enabled:
        open_finance = FinanceEntry.objects.filter(tenant=tenant, status="open")
        payables = (open_finance.filter(kind="payable")
                    .aggregate(total=Sum("amount_cents"))["total"] or 0)
        receivables = (open_finance.filter(kind="receivable")
                       .aggregate(total=Sum("amount_cents"))["total"] or 0)

    result = {
        "generated_at": iso(now),
        "online_window_seconds": int(ONLINE_WINDOW.total_seconds()),
        "period": {"date": today.isoformat(), "from": iso(start), "to": iso(end)},
        "terminals_total": len(active_terminals),
        "terminals_online": len(online_ids),
        "terminals": [{
            "id": str(row.id),
            "name": row.name,
            "shop_id": str(row.shop_id),
            "shop_name": row.shop.name,
            "device_id": row.device_id,
            "version": row.version,
            "paired": bool(row.token_hash),
            "heartbeat_pending_events": row.heartbeat_pending_events,
            "heartbeat_error": row.heartbeat_error,
            "revoked": row.revoked,
            "status": "revoked" if row.revoked else ("online" if row.id in online_ids else "offline"),
            "last_seen_at": iso(row.last_seen_at),
        } for row in terminals],
        "sales": {
            "count": count,
            "revenue_cents": revenue,
            "average_ticket_cents": revenue // count if count else 0,
        },
        "sales_by_terminal": sales_by_terminal,
        "payments_cents": _payment_totals(sales),
        "finance": {
            "enabled": finance_enabled,
            "open_payables_cents": payables,
            "open_receivables_cents": receivables,
        },
    }
    response = JsonResponse(result)
    response["Cache-Control"] = "private, no-store"
    return response
