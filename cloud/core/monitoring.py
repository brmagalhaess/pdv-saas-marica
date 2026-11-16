"""Management numbers shared by the PDV2 Gestão app and the owner monitoring endpoint.

Rules (fixed in docs/APP-GESTAO.md):
- a sale belongs to the day it was made (`Sale.created_at`), never to the day
  the cloud received it, so an offline sale synced later lands on the right day;
- cash received is cash minus change; fiado (credit_account) and store credit
  are not money received;
- terminal counts ignore revoked terminals.

Every function takes a `shop_ids` filter: None = no shop filter (whole
company), otherwise a list of shop ids the person may see.
"""
from datetime import datetime, time, timedelta

from django.db.models import Count, F, Max, Q, Sum
from django.db.models.functions import ExtractHour, TruncDate
from django.utils import timezone

from .api_common import APIError
from .models import (CashSession, CreditReceipt, FinanceEntry, Product, Sale, SalePayment, SaleReturn,
                     StockBalance, SyncConflict, Terminal)

ONLINE_WINDOW = timedelta(minutes=5)
PERIODS = {"today": "Hoje", "yesterday": "Ontem", "7d": "7 dias", "month": "Mês"}
RECEIVED_METHODS = ("cash", "pix", "card_credit", "card_debit")
NOT_RECEIVED = ("credit_account", "store_credit")


def local_iso(value):
    """Local time (America/Sao_Paulo) with offset and no fraction: 2026-11-16T14:03:12-03:00."""
    if value is None:
        return None
    return timezone.localtime(value).isoformat(timespec="seconds")


def _midnight(day):
    return timezone.make_aware(datetime.combine(day, time.min), timezone.get_current_timezone())


def period_window(key, now=None):
    """Half-open [start, end) window of a period key, in the local time zone."""
    key = key or "today"
    if key not in PERIODS:
        raise APIError("Período inválido.", 400, code="invalid_period")
    today = timezone.localdate(now or timezone.now())
    first = {"today": today, "yesterday": today - timedelta(days=1),
             "7d": today - timedelta(days=6), "month": today.replace(day=1)}[key]
    last = today - timedelta(days=1) if key == "yesterday" else today
    start, end = _midnight(first), _midnight(last + timedelta(days=1))
    return {"key": key, "label": PERIODS[key], "first": first, "last": last, "start": start, "end": end}


def period_dict(window):
    return {"key": window["key"], "label": window["label"],
            "date_from": window["first"].isoformat(), "date_to": window["last"].isoformat(),
            "from": local_iso(window["start"]), "to": local_iso(window["end"])}


def by_shop(queryset, shop_ids, field="shop_id"):
    return queryset if shop_ids is None else queryset.filter(**{field + "__in": shop_ids})


def completed_sales(tenant, shop_ids, start, end):
    return by_shop(Sale.objects.filter(tenant=tenant, status="completed",
                                       created_at__gte=start, created_at__lt=end), shop_ids)


def sales_section(tenant, shop_ids, start, end):
    summary = completed_sales(tenant, shop_ids, start, end).aggregate(count=Count("id"), revenue=Sum("total_cents"))
    count, revenue = summary["count"] or 0, summary["revenue"] or 0
    cancelled = by_shop(Sale.objects.filter(tenant=tenant, status__in=("cancelled", "refunded"),
                                            created_at__gte=start, created_at__lt=end), shop_ids) \
        .aggregate(count=Count("id"), total=Sum("total_cents"))
    returns = by_shop(SaleReturn.objects.filter(tenant=tenant, created_at__gte=start, created_at__lt=end),
                      shop_ids).aggregate(total=Sum("total_cents"))["total"] or 0
    return {"count": count, "revenue_cents": revenue, "average_ticket_cents": revenue // count if count else 0,
            "cancelled_count": cancelled["count"] or 0, "cancelled_cents": cancelled["total"] or 0,
            "returns_cents": returns, "net_revenue_cents": revenue - returns}


def method_totals(sales):
    """{'cash','pix','card_credit','card_debit','other','credit_account','store_credit'} in cents.

    Cash is net of change. The legacy generic `card` and company-defined
    methods count as `other`.
    """
    totals = {key: 0 for key in (*RECEIVED_METHODS, "other", *NOT_RECEIVED)}
    for row in SalePayment.objects.filter(sale__in=sales).values("method").annotate(total=Sum("amount_cents")):
        name = row["method"] if row["method"] in totals else "other"
        totals[name] += row["total"] or 0
    totals["cash"] -= sales.aggregate(change=Sum("change_cents"))["change"] or 0
    return totals


def payments_section(tenant, shop_ids, start, end):
    totals = method_totals(completed_sales(tenant, shop_ids, start, end))
    received = sum(totals[key] for key in (*RECEIVED_METHODS, "other"))
    credit_received = by_shop(CreditReceipt.objects.filter(tenant=tenant, created_at__gte=start, created_at__lt=end),
                              shop_ids).aggregate(total=Sum("amount_cents"))["total"] or 0
    return {"cash_cents": totals["cash"], "pix_cents": totals["pix"], "card_credit_cents": totals["card_credit"],
            "card_debit_cents": totals["card_debit"], "other_cents": totals["other"],
            "received_total_cents": received, "credit_account_cents": totals["credit_account"],
            "store_credit_cents": totals["store_credit"], "credit_received_cents": credit_received}


def series_section(tenant, shop_ids, window):
    sales = completed_sales(tenant, shop_ids, window["start"], window["end"])
    if window["key"] in ("today", "yesterday"):
        rows = {row["hour"]: row for row in sales.annotate(hour=ExtractHour("created_at")).values("hour")
                .annotate(count=Count("id"), revenue=Sum("total_cents"))}
        points = []
        for hour in range(24):
            row = rows.get(hour, {})
            points.append({"key": f"{hour:02d}", "label": f"{hour:02d}h", "count": row.get("count", 0),
                           "revenue_cents": row.get("revenue") or 0})
        return {"kind": "hour", "points": points}
    rows = {row["day"]: row for row in sales.annotate(day=TruncDate("created_at")).values("day")
            .annotate(count=Count("id"), revenue=Sum("total_cents"))}
    points, day = [], window["first"]
    while day <= window["last"]:
        row = rows.get(day, {})
        points.append({"key": day.isoformat(), "label": day.strftime("%d/%m"), "count": row.get("count", 0),
                       "revenue_cents": row.get("revenue") or 0})
        day += timedelta(days=1)
    return {"kind": "day", "points": points}


def cash_section(tenant, shop_ids, start, end):
    sessions = by_shop(CashSession.objects.filter(tenant=tenant), shop_ids)
    closed = sessions.filter(closed_at__gte=start, closed_at__lt=end)
    shortage = closed.filter(difference_cents__lt=0).aggregate(count=Count("id"), total=Sum("difference_cents"))
    return {"open_count": sessions.filter(closed_at__isnull=True).count(), "closed_count": closed.count(),
            "shortage_count": shortage["count"] or 0, "shortage_cents": shortage["total"] or 0}


def stock_products(tenant):
    return Product.objects.filter(tenant=tenant, active=True, stock_controlled=True)


def stock_section(tenant, shop_ids):
    """Counts of (shop, product) pairs; `shop_ids` here is always an explicit list of shops."""
    products = stock_products(tenant)
    balances = StockBalance.objects.filter(tenant=tenant, shop_id__in=shop_ids, product__in=products)
    with_minimum = products.filter(stock_minimum_milli__gt=0)
    missing = (with_minimum.count() * len(shop_ids) -
               balances.filter(product__stock_minimum_milli__gt=0).count())
    zeroed = balances.filter(quantity_milli__lte=0).count() + max(missing, 0)
    below = balances.filter(product__stock_minimum_milli__gt=0, quantity_milli__gt=0,
                            quantity_milli__lt=F("product__stock_minimum_milli")).count()
    return {"zeroed_count": zeroed, "below_minimum_count": below}


def credit_entries(tenant, shop_ids):
    return by_shop(FinanceEntry.objects.filter(tenant=tenant, kind="receivable", sale__isnull=False,
                                               status="open"), shop_ids)


def credit_section(tenant, shop_ids):
    today = timezone.localdate()
    entries = credit_entries(tenant, shop_ids)
    totals = entries.aggregate(open=Sum("amount_cents"), overdue=Sum("amount_cents", filter=Q(due_date__lt=today)))
    customers = entries.filter(due_date__lt=today).values("customer_id").distinct().count()
    return {"open_cents": totals["open"] or 0, "overdue_cents": totals["overdue"] or 0,
            "overdue_customers": customers}


def terminal_status(row, online_after):
    if not row.token_hash:
        return "unpaired"
    return "online" if row.last_seen_at and row.last_seen_at >= online_after else "offline"


def terminals_section(tenant, shop_ids):
    online_after = timezone.now() - ONLINE_WINDOW
    rows = by_shop(Terminal.objects.filter(tenant=tenant, revoked=False), shop_ids)
    paired = rows.filter(token_hash__isnull=False)
    online = paired.filter(last_seen_at__gte=online_after).count()
    total = paired.count()
    conflicts = by_shop(SyncConflict.objects.filter(tenant=tenant, status="open"), shop_ids).count()
    return {"total": total, "online": online, "offline": total - online,
            "unpaired": rows.filter(token_hash__isnull=True).count(),
            "online_window_seconds": int(ONLINE_WINDOW.total_seconds()), "conflicts_open": conflicts}


def last_activity(tenant_ids):
    """{tenant_id: (last sale created_at, last terminal contact)} for a list of companies."""
    sales = dict(Sale.objects.filter(tenant_id__in=tenant_ids).values_list("tenant_id")
                 .annotate(v=Max("created_at")))
    seen = dict(Terminal.objects.filter(tenant_id__in=tenant_ids, revoked=False).values_list("tenant_id")
                .annotate(v=Max("last_seen_at")))
    return {pk: (sales.get(pk), seen.get(pk)) for pk in tenant_ids}
