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

from django.db.models import Count, F, Q, Sum
from django.db.models.functions import Coalesce, ExtractHour, TruncDate
from django.http import HttpResponse, JsonResponse
from django.utils import timezone

from .api_common import APIError, endpoint, membership_context, method, require_feature, scoped
from .payment_methods import fee_lookup
from .stock_balance import balances as stock_balances
from .models import (FinanceEntry, Product, Sale, SaleItem, SalePayment, SaleReturn, SaleReturnItem, Shop,
                     StockMovement)

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
    returned = dict(_returns(tenant, shop, start, end).annotate(hour=ExtractHour("created_at"))
                    .values_list("hour").annotate(v=Sum("total_cents")))
    hours = {r["hour"]: r for r in rows}
    data = []
    for hour in sorted(set(hours) | set(returned)):
        r = hours.get(hour, {"count": 0, "total": 0})
        back = returned.get(hour, 0)
        data.append({"hour": f"{hour:02d}h", "count": r["count"], "total": r["total"], "returns": back,
                     "net": r["total"] - back})
    return ([_col("hour", "Hora"), _col("count", "Vendas", "int"), _col("total", "Faturamento", "money"),
             _col("returns", "Devoluções", "money"), _col("net", "Faturamento líquido", "money")], data,
            {key: sum(r[key] for r in data) for key in ("count", "total", "returns", "net")})


def _returns_by_payment(tenant, shop, start, end):
    """Returned amount per payment bucket of sales_by_payment.

    Cash leaves the cash row, an account refund abates the fiado row and store
    credit issued counts against the store-credit row (it comes back as a
    payment when used). A refund made outside the PDV (Pix sent back, card
    chargeback) is charged to the main electronic payment of the sale.
    """
    returns = _returns(tenant, shop, start, end)
    buckets = {"cash": "cash", "account": "credit_account", "store_credit": "store_credit"}
    result = defaultdict(int)
    for refund, amount in (returns.exclude(refund_method="external")
                           .values_list("refund_method").annotate(v=Sum("total_cents"))):
        result[buckets[refund]] += amount
    external = dict(returns.filter(refund_method="external").values_list("sale_id").annotate(v=Sum("total_cents")))
    if external:
        main = {}
        for sale_id, method_name, configured_id, amount in (
                SalePayment.objects.filter(sale_id__in=list(external))
                .values_list("sale_id", "method", "payment_method_id", "amount_cents")):
            rank = (method_name not in ("cash", "credit_account", "store_credit"), amount)
            if sale_id not in main or rank > main[sale_id][0]:
                main[sale_id] = (rank, configured_id if method_name == "other" else method_name)
        for sale_id, amount in external.items():
            result[main[sale_id][1] if sale_id in main else "cash"] += amount
    return result


def sales_by_payment(tenant, shop, start, end):
    sales = _sales(tenant, shop, start, end)
    configured = fee_lookup(tenant)
    sums = defaultdict(int)
    for method_name, configured_id, amount in (SalePayment.objects.filter(sale__in=sales)
                                               .values_list("method", "payment_method_id")
                                               .annotate(v=Sum("amount_cents"))):
        sums[configured_id if method_name == "other" else method_name] += amount
    sums["cash"] -= sales.aggregate(v=Sum("change_cents"))["v"] or 0
    returned = _returns_by_payment(tenant, shop, start, end)
    total = sum(sums.values())
    data = []
    for key in sorted(set(sums) | set(returned), key=lambda k: (-sums[k], str(k))):
        value, back = sums[key], returned.get(key, 0)
        if not value and not back:
            continue
        name, fee_bps = configured.get(key, (PAYMENT_NAMES.get(key, str(key)), 0))
        fee = int((Decimal(max(value - back, 0)) * fee_bps / 10000).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
        data.append({"method": name, "total": value, "returns": back, "share": _pct(value, total), "fee": fee,
                     "net": value - back - fee})
    fees, back = sum(r["fee"] for r in data), sum(r["returns"] for r in data)
    return ([_col("method", "Forma"), _col("total", "Recebido", "money"), _col("returns", "Devoluções", "money"),
             _col("share", "Participação", "percent"), _col("fee", "Taxa estimada", "money"),
             _col("net", "Líquido", "money")],
            data, {"total": total, "returns": back, "share": 10000 if total else 0, "fee": fees,
                   "net": total - back - fees})


def _by_person(field, label):
    def report(tenant, shop, start, end):
        name = f"{field}_name_snapshot"
        rows = (_sales(tenant, shop, start, end).values(name)
                .annotate(count=Count("id"), total=Sum("total_cents"), discount=Sum("discount_cents")))
        # A return counts against whoever made the sale, not who processed the return.
        returned = dict(_returns(tenant, shop, start, end).values_list(f"sale__{name}").annotate(v=Sum("total_cents")))
        people = {r[name]: r for r in rows}
        data = []
        for person in set(people) | set(returned):
            r = people.get(person, {"count": 0, "total": 0, "discount": 0})
            back = returned.get(person, 0)
            data.append({"person": person or "Não informado", "count": r["count"], "total": r["total"],
                         "discount": r["discount"], "returns": back, "net": r["total"] - back,
                         "ticket": r["total"] // r["count"] if r["count"] else 0})
        data.sort(key=lambda r: (-r["net"], r["person"]))
        return ([_col("person", label), _col("count", "Vendas", "int"), _col("total", "Faturamento", "money"),
                 _col("discount", "Descontos", "money"), _col("returns", "Devoluções", "money"),
                 _col("net", "Faturamento líquido", "money"), _col("ticket", "Ticket médio", "money")], data,
                {key: sum(r[key] for r in data) for key in ("count", "total", "discount", "returns", "net")})
    return report


def _cents_from_milli(value):
    return int((Decimal(value) / 1000).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _item_totals(tenant, shop, start, end, sold_key, returned_key):
    """{key: [net quantity, gross revenue, returned amount, net cost in cents x milli]}.

    Items use the cost recorded when they were sold. A return takes the
    quantity, the amount and that same cost out on the day it was made, the
    rule of sales_by_day, so every report closes with it.
    """
    totals = defaultdict(lambda: [0, 0, 0, 0])
    sold = (SaleItem.objects.filter(sale__in=_sales(tenant, shop, start, end))
            .annotate(key=sold_key, unit_cost=Coalesce("unit_cost_cents", "product__cost_cents"))
            .values("key", "unit_cost").annotate(qty=Sum("quantity_milli"), amount=Sum("line_total_cents")))
    for r in sold:
        row = totals[r["key"]]
        row[0] += r["qty"]
        row[1] += r["amount"]
        row[3] += r["qty"] * r["unit_cost"]
    returns = _returns(tenant, shop, start, end)
    sold_cost = {(sale_id, product_id): cost for sale_id, product_id, cost in
                 SaleItem.objects.filter(sale__in=returns.values("sale_id"))
                 .annotate(cost=Coalesce("unit_cost_cents", "product__cost_cents"))
                 .values_list("sale_id", "product_id", "cost")}
    back = (SaleReturnItem.objects.filter(sale_return__in=returns).annotate(key=returned_key)
            .values("key", "sale_return__sale_id", "product_id")
            .annotate(qty=Sum("quantity_milli"), amount=Sum("amount_cents")))
    for r in back:
        row = totals[r["key"]]
        row[0] -= r["qty"]
        row[2] += r["amount"]
        row[3] -= r["qty"] * sold_cost.get((r["sale_return__sale_id"], r["product_id"]), 0)
    return totals


def _profit_row(gross, back, cost_milli):
    revenue, cost = gross - back, _cents_from_milli(cost_milli)
    return {"gross": gross, "returns": back, "revenue": revenue, "cost": cost,
            "profit": revenue - cost, "margin": _pct(revenue - cost, revenue)}


def _profit_totals(data):
    totals = {key: sum(r[key] for r in data) for key in ("gross", "returns", "revenue", "cost", "profit")}
    return {**totals, "margin": _pct(totals["profit"], totals["revenue"])}


PROFIT_COLUMNS = [_col("gross", "Faturamento", "money"), _col("returns", "Devoluções", "money"),
                  _col("revenue", "Faturamento líquido", "money"), _col("cost", "Custo", "money"),
                  _col("profit", "Lucro bruto", "money"), _col("margin", "Margem", "percent")]


def products_abc(tenant, shop, start, end):
    totals = _item_totals(tenant, shop, start, end, F("product_id"), F("product_id"))
    names = {pk: (sku, name) for pk, sku, name in
             Product.objects.filter(tenant=tenant, pk__in=list(totals)).values_list("id", "sku", "name")}
    rows = sorted(({"sku": names[pk][0], "product": names[pk][1], "qty": qty, **_profit_row(*values)}
                   for pk, (qty, *values) in totals.items()), key=lambda r: (-r["revenue"], r["product"], r["sku"]))
    # Products whose returns outweigh the period's sales add nothing to the curve.
    total = sum(r["revenue"] for r in rows if r["revenue"] > 0)
    data, running = [], 0
    for r in rows:
        # Class by the share accumulated *before* this item, so the best seller
        # is always A even when it alone exceeds 80% of revenue.
        before = _pct(running, total)
        running += max(r["revenue"], 0)
        data.append({**r, "cumulative": _pct(running, total),
                     "abc": "C" if r["revenue"] <= 0 else "A" if before < 8000 else "B" if before < 9500 else "C"})
    return ([_col("abc", "Curva"), _col("sku", "SKU"), _col("product", "Produto"),
             _col("qty", "Quantidade líquida", "milli"), *PROFIT_COLUMNS,
             _col("cumulative", "% acumulado", "percent")], data, _profit_totals(data))


def profit_by_day(tenant, shop, start, end):
    totals = _item_totals(tenant, shop, start, end, TruncDate("sale__created_at"),
                          TruncDate("sale_return__created_at"))
    data = [{"day": day.isoformat(), **_profit_row(*values)} for day, (_, *values) in sorted(totals.items())]
    return [_col("day", "Dia"), *PROFIT_COLUMNS], data, _profit_totals(data)


def low_stock(tenant, shop, start, end):
    shops = [shop] if shop else list(Shop.objects.filter(tenant=tenant, active=True))
    since = timezone.now() - timedelta(days=30)
    data = []
    for current in shops:
        balances = stock_balances(tenant, current)
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
    rows = StockMovement.objects.filter(tenant=tenant, source__in=("manual", "terminal_adjustment", "inventory"), delta_milli__lt=0,
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


def promotions_report(tenant, shop, start, end):
    rows = (SaleItem.objects.filter(sale__in=_sales(tenant, shop, start, end), promo_discount_cents__gt=0)
            .values("promotion_name").annotate(uses=Count("sale", distinct=True), qty=Sum("quantity_milli"),
                                               discount=Sum("promo_discount_cents"), revenue=Sum("line_total_cents"))
            .order_by("-discount"))
    data = [{"promotion": r["promotion_name"] or "Promoção", "uses": r["uses"], "qty": r["qty"],
             "discount": r["discount"], "revenue": r["revenue"]} for r in rows]
    return ([_col("promotion", "Promoção"), _col("uses", "Vendas", "int"), _col("qty", "Quantidade", "milli"),
             _col("discount", "Desconto concedido", "money"), _col("revenue", "Faturamento com promoção", "money")], data,
            {"discount": sum(r["discount"] for r in data), "revenue": sum(r["revenue"] for r in data)})


REPORTS = {
    "sales_by_day": ("Vendas por dia", sales_by_day),
    "sales_by_hour": ("Vendas por hora", sales_by_hour),
    "sales_by_payment": ("Vendas por forma de pagamento", sales_by_payment),
    "sales_by_operator": ("Vendas por operador", _by_person("operator", "Operador")),
    "sales_by_seller": ("Vendas por vendedor", _by_person("seller", "Vendedor")),
    "products_abc": ("Curva ABC e lucro por produto", products_abc),
    "profit_by_day": ("Lucro bruto por dia", profit_by_day),
    "promotions": ("Promoções: desconto concedido e faturamento", promotions_report),
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
    value = str(value)
    # Free text (reason, product or person name) must not run as a spreadsheet formula.
    return "'" + value if kind == "text" and value[:1] in ("=", "+", "-", "@", "\t", "\r") else value


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
