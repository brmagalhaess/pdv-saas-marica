"""Management reports. JSON for the panel, CSV (Excel pt-BR) for export.

Every report returns {"columns": [...], "rows": [...], "totals": {...}} where a
column type is one of text, int, money, milli, percent so the panel and CSV
format values the same way.
"""
import csv
import io
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP

from django.db.models import Count, Q, Sum
from django.db.models.functions import ExtractHour, TruncDate
from django.http import HttpResponse, JsonResponse
from django.utils import timezone

from .api_common import APIError, endpoint, membership_context, method, require_feature, scoped
from .models import FinanceEntry, Product, Sale, SaleItem, SalePayment, SaleReturn, Shop, StockMovement

READ = {"owner", "admin", "manager", "finance", "viewer"}
PAYMENT_NAMES = {"cash": "Dinheiro", "pix": "Pix", "card_credit": "Crédito", "card_debit": "Débito", "card": "Cartão",
                 "credit_account": "A prazo (fiado)", "store_credit": "Crédito na loja"}
COVERAGE_DAYS = 15


def _col(key, label, kind="text"):
    return {"key": key, "label": label, "type": kind}


def _period(request):
    today = timezone.localdate()
    try:
        start = date.fromisoformat(request.GET["from"]) if request.GET.get("from") else today.replace(day=1)
        end = date.fromisoformat(request.GET["to"]) if request.GET.get("to") else today
    except ValueError:
        raise APIError("Data inválida")
    if end < start or (end - start).days > 366:
        raise APIError("Período inválido (máximo de 1 ano)")
    return start, end


def _sales(tenant, shop, start, end):
    rows = Sale.objects.filter(tenant=tenant, status="completed",
                               created_at__date__gte=start, created_at__date__lte=end)
    return rows.filter(shop=shop) if shop else rows


def _pct(part, whole):
    return int((Decimal(part) * 10000 / Decimal(whole)).quantize(Decimal("1"), rounding=ROUND_HALF_UP)) if whole else 0


def _returns(tenant, shop, start, end):
    rows = SaleReturn.objects.filter(tenant=tenant, sale__status="completed",
                                     created_at__date__gte=start, created_at__date__lte=end)
    return rows.filter(shop=shop) if shop else rows


def sales_by_day(tenant, shop, start, end):
    rows = (_sales(tenant, shop, start, end).annotate(day=TruncDate("created_at")).values("day")
            .annotate(count=Count("id"), total=Sum("total_cents"), discount=Sum("discount_cents")).order_by("day"))
    returned = dict(_returns(tenant, shop, start, end).annotate(day=TruncDate("created_at"))
                    .values_list("day").annotate(v=Sum("total_cents")))
    days = {r["day"]: r for r in rows}
    data = []
    for day in sorted(set(days) | set(returned)):
        r = days.get(day, {"count": 0, "total": 0, "discount": 0})
        back = returned.get(day, 0)
        data.append({"day": day.isoformat(), "count": r["count"], "total": r["total"], "discount": r["discount"],
                     "returns": back, "net": r["total"] - back, "ticket": r["total"] // r["count"] if r["count"] else 0})
    count, total = sum(r["count"] for r in data), sum(r["total"] for r in data)
    back = sum(r["returns"] for r in data)
    return ([_col("day", "Dia"), _col("count", "Vendas", "int"), _col("total", "Faturamento", "money"),
             _col("discount", "Descontos", "money"), _col("returns", "Devoluções", "money"),
             _col("net", "Faturamento líquido", "money"), _col("ticket", "Ticket médio", "money")], data,
            {"count": count, "total": total, "discount": sum(r["discount"] for r in data), "returns": back,
             "net": total - back, "ticket": total // count if count else 0})


def sales_by_hour(tenant, shop, start, end):
    rows = (_sales(tenant, shop, start, end).annotate(hour=ExtractHour("created_at")).values("hour")
            .annotate(count=Count("id"), total=Sum("total_cents")).order_by("hour"))
    data = [{"hour": f"{r['hour']:02d}h", "count": r["count"], "total": r["total"]} for r in rows]
    return ([_col("hour", "Hora"), _col("count", "Vendas", "int"), _col("total", "Faturamento", "money")], data,
            {"count": sum(r["count"] for r in data), "total": sum(r["total"] for r in data)})


def sales_by_payment(tenant, shop, start, end):
    sales = _sales(tenant, shop, start, end)
    sums = defaultdict(int)
    for method_name, amount in SalePayment.objects.filter(sale__in=sales).values_list("method", "amount_cents"):
        sums[method_name] += amount
    sums["cash"] -= sales.aggregate(v=Sum("change_cents"))["v"] or 0
    total = sum(sums.values())
    data = [{"method": PAYMENT_NAMES.get(k, k), "total": v, "share": _pct(v, total)}
            for k, v in sorted(sums.items(), key=lambda kv: -kv[1]) if v]
    return ([_col("method", "Forma"), _col("total", "Recebido", "money"), _col("share", "Participação", "percent")],
            data, {"total": total, "share": 10000 if total else 0})


def _by_person(field, label):
    def report(tenant, shop, start, end):
        rows = (_sales(tenant, shop, start, end).values(f"{field}_name_snapshot")
                .annotate(count=Count("id"), total=Sum("total_cents"), discount=Sum("discount_cents"))
                .order_by("-total"))
        data = [{"person": r[f"{field}_name_snapshot"] or "Não informado", "count": r["count"], "total": r["total"],
                 "discount": r["discount"], "ticket": r["total"] // r["count"]} for r in rows]
        return ([_col("person", label), _col("count", "Vendas", "int"), _col("total", "Faturamento", "money"),
                 _col("discount", "Descontos", "money"), _col("ticket", "Ticket médio", "money")], data,
                {"count": sum(r["count"] for r in data), "total": sum(r["total"] for r in data),
                 "discount": sum(r["discount"] for r in data)})
    return report


def products_abc(tenant, shop, start, end):
    rows = (SaleItem.objects.filter(sale__in=_sales(tenant, shop, start, end))
            .values("product_id", "product__name", "product__sku", "product__cost_cents")
            .annotate(qty=Sum("quantity_milli"), revenue=Sum("line_total_cents")).order_by("-revenue"))
    total = sum(r["revenue"] for r in rows)
    data, running = [], 0
    for r in rows:
        # Class by the share accumulated *before* this item, so the best seller
        # is always A even when it alone exceeds 80% of revenue.
        before = _pct(running, total)
        running += r["revenue"]
        cost = int(Decimal(r["product__cost_cents"]) * Decimal(r["qty"]) / 1000)
        share = _pct(running, total)
        data.append({"sku": r["product__sku"], "product": r["product__name"], "qty": r["qty"],
                     "revenue": r["revenue"], "cost": cost, "profit": r["revenue"] - cost,
                     "margin": _pct(r["revenue"] - cost, r["revenue"]), "cumulative": share,
                     "abc": "A" if before < 8000 else "B" if before < 9500 else "C"})
    revenue, cost = total, sum(r["cost"] for r in data)
    return ([_col("abc", "Curva"), _col("sku", "SKU"), _col("product", "Produto"), _col("qty", "Quantidade", "milli"),
             _col("revenue", "Faturamento", "money"), _col("cost", "Custo estimado", "money"),
             _col("profit", "Lucro bruto", "money"), _col("margin", "Margem", "percent"),
             _col("cumulative", "% acumulado", "percent")], data,
            {"revenue": revenue, "cost": cost, "profit": revenue - cost, "margin": _pct(revenue - cost, revenue)})


def profit_by_day(tenant, shop, start, end):
    items = (SaleItem.objects.filter(sale__in=_sales(tenant, shop, start, end))
             .annotate(day=TruncDate("sale__created_at"))
             .values("day", "line_total_cents", "quantity_milli", "product__cost_cents"))
    days = defaultdict(lambda: [0, 0])
    for r in items:
        days[r["day"]][0] += r["line_total_cents"]
        days[r["day"]][1] += int(Decimal(r["product__cost_cents"]) * Decimal(r["quantity_milli"]) / 1000)
    data = [{"day": d.isoformat(), "revenue": v[0], "cost": v[1], "profit": v[0] - v[1],
             "margin": _pct(v[0] - v[1], v[0])} for d, v in sorted(days.items())]
    revenue, cost = sum(r["revenue"] for r in data), sum(r["cost"] for r in data)
    return ([_col("day", "Dia"), _col("revenue", "Faturamento", "money"), _col("cost", "Custo estimado", "money"),
             _col("profit", "Lucro bruto", "money"), _col("margin", "Margem", "percent")], data,
            {"revenue": revenue, "cost": cost, "profit": revenue - cost, "margin": _pct(revenue - cost, revenue)})


def low_stock(tenant, shop, start, end):
    shops = [shop] if shop else list(Shop.objects.filter(tenant=tenant, active=True))
    since = timezone.now() - timedelta(days=30)
    data = []
    for current in shops:
        balances = dict(StockMovement.objects.filter(tenant=tenant, shop=current)
                        .values_list("product_id").annotate(q=Sum("delta_milli")))
        sold = dict(StockMovement.objects.filter(tenant=tenant, shop=current, source="sale", created_at__gte=since)
                    .values_list("product_id").annotate(q=Sum("delta_milli")))
        for product in Product.objects.filter(tenant=tenant, active=True, stock_controlled=True).order_by("name"):
            qty = balances.get(product.id, 0)
            daily = -sold.get(product.id, 0) / 30
            target = max(product.stock_minimum_milli, int(daily * COVERAGE_DAYS))
            if qty > product.stock_minimum_milli and (daily == 0 or qty > daily * 7):
                continue
            unit = 1000 if product.unit == "UN" else 1
            suggested = max(0, target - qty)
            suggested = -(-suggested // unit) * unit
            data.append({"shop": current.name, "sku": product.sku, "product": product.name, "balance": qty,
                         "minimum": product.stock_minimum_milli, "daily": int(daily),
                         "days_left": int(qty / daily) if daily > 0 and qty > 0 else 0, "suggested": suggested,
                         "cost": int(Decimal(product.cost_cents) * Decimal(suggested) / 1000)})
    return ([_col("shop", "Loja"), _col("sku", "SKU"), _col("product", "Produto"), _col("balance", "Saldo", "milli"),
             _col("minimum", "Mínimo", "milli"), _col("daily", "Venda média/dia (30d)", "milli"),
             _col("days_left", "Dias de cobertura", "int"), _col("suggested", f"Sugestão de compra ({COVERAGE_DAYS} dias)", "milli"),
             _col("cost", "Custo da sugestão", "money")], data, {"cost": sum(r["cost"] for r in data)})


def stock_losses(tenant, shop, start, end):
    rows = StockMovement.objects.filter(tenant=tenant, source__in=("manual", "terminal_adjustment"), delta_milli__lt=0,
                                        created_at__date__gte=start, created_at__date__lte=end)
    if shop:
        rows = rows.filter(shop=shop)
    rows = (rows.values("reason", "product__name", "product__cost_cents")
            .annotate(qty=Sum("delta_milli"), events=Count("id")).order_by("qty"))
    data = [{"reason": r["reason"], "product": r["product__name"], "events": r["events"], "qty": -r["qty"],
             "cost": int(Decimal(r["product__cost_cents"]) * Decimal(-r["qty"]) / 1000)} for r in rows]
    return ([_col("reason", "Motivo"), _col("product", "Produto"), _col("events", "Ocorrências", "int"),
             _col("qty", "Quantidade", "milli"), _col("cost", "Custo estimado", "money")], data,
            {"cost": sum(r["cost"] for r in data)})


def cash_flow(tenant, shop, start, end):
    days = defaultdict(lambda: {"sales": 0, "received": 0, "paid": 0, "due_in": 0, "due_out": 0})
    # Money that actually came in at the counter: fiado and store credit are not cash in.
    sales = _sales(tenant, shop, start, end)
    for row in (SalePayment.objects.filter(sale__in=sales).exclude(method__in=("credit_account", "store_credit"))
                .annotate(day=TruncDate("sale__created_at")).values("day").annotate(v=Sum("amount_cents"))):
        days[row["day"]]["sales"] += row["v"]
    for row in sales.annotate(day=TruncDate("created_at")).values("day").annotate(v=Sum("change_cents")):
        days[row["day"]]["sales"] -= row["v"]
    for row in (_returns(tenant, shop, start, end).filter(refund_method="cash")
                .annotate(day=TruncDate("created_at")).values("day").annotate(v=Sum("total_cents"))):
        days[row["day"]]["sales"] -= row["v"]
    finance = FinanceEntry.objects.filter(tenant=tenant)
    if shop:
        finance = finance.filter(Q(shop=shop) | Q(shop__isnull=True))
    settled = finance.filter(status="settled", settled_at__date__gte=start, settled_at__date__lte=end)
    for row in settled.annotate(day=TruncDate("settled_at")).values("day", "kind").annotate(v=Sum("amount_cents")):
        days[row["day"]]["received" if row["kind"] == "receivable" else "paid"] += row["v"]
    for row in finance.filter(status="open", due_date__gte=start, due_date__lte=end).values("due_date", "kind").annotate(v=Sum("amount_cents")):
        days[row["due_date"]]["due_in" if row["kind"] == "receivable" else "due_out"] += row["v"]
    data, balance = [], 0
    for day in sorted(days):
        v = days[day]
        balance += v["sales"] + v["received"] - v["paid"]
        data.append({"day": day.isoformat(), **v, "net": v["sales"] + v["received"] - v["paid"], "balance": balance})
    totals = {k: sum(r[k] for r in data) for k in ("sales", "received", "paid", "due_in", "due_out", "net")}
    return ([_col("day", "Dia"), _col("sales", "Vendas recebidas no caixa", "money"), _col("received", "Recebimentos", "money"),
             _col("paid", "Pagamentos", "money"), _col("net", "Resultado do dia", "money"),
             _col("balance", "Acumulado", "money"), _col("due_in", "A receber (previsto)", "money"),
             _col("due_out", "A pagar (previsto)", "money")], data, totals)


REPORTS = {
    "sales_by_day": ("Vendas por dia", sales_by_day),
    "sales_by_hour": ("Vendas por hora", sales_by_hour),
    "sales_by_payment": ("Vendas por forma de pagamento", sales_by_payment),
    "sales_by_operator": ("Vendas por operador", _by_person("operator", "Operador")),
    "sales_by_seller": ("Vendas por vendedor", _by_person("seller", "Vendedor")),
    "products_abc": ("Curva ABC e lucro por produto", products_abc),
    "profit_by_day": ("Lucro bruto por dia", profit_by_day),
    "cash_flow": ("Fluxo de caixa", cash_flow),
    "low_stock": ("Estoque baixo e sugestão de compra", low_stock),
    "stock_losses": ("Perdas e ajustes de estoque", stock_losses),
}


def _csv_value(value, kind):
    if value is None:
        return ""
    if kind == "money":
        return f"{value / 100:.2f}".replace(".", ",")
    if kind == "milli":
        return f"{value / 1000:.3f}".rstrip("0").rstrip(".").replace(".", ",")
    if kind == "percent":
        return f"{value / 100:.2f}".replace(".", ",")
    return str(value)


@endpoint
def report_catalog(request):
    method(request, "GET")
    member = membership_context(request, roles=READ)
    require_feature(member, "reports")
    return JsonResponse({"results": [{"id": key, "title": title} for key, (title, _) in REPORTS.items()]})


@endpoint
def report(request, kind):
    method(request, "GET")
    member = membership_context(request, roles=READ)
    require_feature(member, "reports")
    if kind not in REPORTS:
        raise APIError("Relatório não encontrado", 404)
    title, build = REPORTS[kind]
    shop = scoped(Shop, member.tenant, request.GET["shop_id"]) if request.GET.get("shop_id") else None
    start, end = _period(request)
    columns, rows, totals = build(member.tenant, shop, start, end)
    if request.GET.get("format") == "csv":
        buffer = io.StringIO()
        writer = csv.writer(buffer, delimiter=";")
        writer.writerow([c["label"] for c in columns])
        for row in rows:
            writer.writerow([_csv_value(row.get(c["key"]), c["type"]) for c in columns])
        if totals:
            writer.writerow([_csv_value(totals.get(c["key"]), c["type"]) if c["key"] in totals else ("Total" if i == 0 else "")
                             for i, c in enumerate(columns)])
        response = HttpResponse("﻿" + buffer.getvalue(), content_type="text/csv; charset=utf-8")
        response["Content-Disposition"] = f'attachment; filename="{kind}_{start}_{end}.csv"'
        return response
    return JsonResponse({"id": kind, "title": title, "from": start.isoformat(), "to": end.isoformat(),
                         "columns": columns, "rows": rows, "totals": totals})
