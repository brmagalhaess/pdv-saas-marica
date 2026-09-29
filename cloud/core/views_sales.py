from datetime import date, datetime, time

from django.db import transaction
from django.db.models import Count, Sum
from django.http import JsonResponse
from django.utils import timezone

from .api_common import APIError, endpoint, iso, membership_context, method, page, payload, require_feature, scoped, text, uuid_value
from .models import AuditEvent, FinanceEntry, Product, Sale, Shop, StockMovement
from .sale_logic import reverse_sale
from .serializers import sale as sale_data


def _sale(tenant, pk):
    row = Sale.objects.filter(tenant=tenant, pk=uuid_value(pk, "Venda")).first()
    if row is None:
        raise APIError("Venda não encontrada", 404)
    return row


@endpoint
def sales(request):
    method(request, "GET")
    member = membership_context(request)
    rows = Sale.objects.filter(tenant=member.tenant)
    if request.GET.get("shop_id"):
        rows = rows.filter(shop=scoped(Shop, member.tenant, request.GET["shop_id"]))
    if request.GET.get("status"):
        rows = rows.filter(status=request.GET["status"])
    return page(request, rows.order_by("-received_at", "-id"), sale_data)


@endpoint
def sale_detail(request, pk):
    method(request, "GET")
    member = membership_context(request)
    row = _sale(member.tenant, pk)
    return JsonResponse(sale_data(row, detail=True))


@endpoint
def sale_reverse(request, pk, action):
    method(request, "POST")
    roles = {"owner", "admin", "manager"} if action == "cancel" else {"owner", "admin", "finance"}
    member = membership_context(request, roles=roles, write=True)
    data = payload(request)
    if set(data) != {"reason"}:
        raise APIError("Informe motivo")
    reason = text(data["reason"], "Motivo", 240)
    with transaction.atomic():
        row = Sale.objects.select_for_update().filter(tenant=member.tenant, pk=uuid_value(pk)).first()
        if row is None:
            raise APIError("Venda não encontrada", 404)
        reverse_sale(row, reason, "cancelled" if action == "cancel" else "refunded", actor=member.user)
    return JsonResponse(sale_data(row, detail=True))


def _parse_date(value, name):
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        raise APIError(f"{name} inválida")


@endpoint
def dashboard(request):
    method(request, "GET")
    member = membership_context(request)
    require_feature(member, "cloud_dashboard")
    if request.GET.get("from") or request.GET.get("to"):
        require_feature(member, "reports")
    tenant = member.tenant
    shop = scoped(Shop, tenant, request.GET["shop_id"]) if request.GET.get("shop_id") else None
    start = _parse_date(request.GET["from"], "Data inicial") if request.GET.get("from") else timezone.localdate().replace(day=1)
    end = _parse_date(request.GET["to"], "Data final") if request.GET.get("to") else timezone.localdate()
    if end < start or (end - start).days > 366:
        raise APIError("Período inválido")
    sales = Sale.objects.filter(tenant=tenant, status="completed", created_at__date__gte=start, created_at__date__lte=end)
    if shop:
        sales = sales.filter(shop=shop)
    values = sales.aggregate(count=Count("id"), revenue=Sum("total_cents"))
    count, revenue = values["count"] or 0, values["revenue"] or 0
    payable = receivable = 0
    if "finance" in tenant.subscription.plan.features:
        finance = FinanceEntry.objects.filter(tenant=tenant, status="open")
        if shop:
            finance = finance.filter(shop=shop)
        payable = finance.filter(kind="payable").aggregate(total=Sum("amount_cents"))["total"] or 0
        receivable = finance.filter(kind="receivable").aggregate(total=Sum("amount_cents"))["total"] or 0
    low_stock = 0
    shops = [shop] if shop else list(Shop.objects.filter(tenant=tenant, active=True))
    products = list(Product.objects.filter(tenant=tenant, active=True))
    for current_shop in shops:
        amounts = (StockMovement.objects.filter(tenant=tenant, shop=current_shop)
                   .values("product_id").annotate(q=Sum("delta_milli")))
        by_product = {row["product_id"]: row["q"] for row in amounts}
        low_stock += sum(by_product.get(p.id, 0) < p.stock_minimum_milli for p in products if p.stock_minimum_milli > 0)
    recent = list(sales.order_by("-received_at")[:10])
    return JsonResponse({"sales_count": count, "revenue_cents": revenue,
                         "average_ticket_cents": revenue // count if count else 0,
                         "low_stock_count": low_stock,
                         "open_payables_cents": payable,
                         "open_receivables_cents": receivable,
                         "recent_sales": [sale_data(row) for row in recent]})


@endpoint
def audit_events(request):
    method(request, "GET")
    member = membership_context(request, roles={"owner", "admin"})
    rows = AuditEvent.objects.select_related("actor").filter(tenant=member.tenant).order_by("-created_at", "-id")
    return page(request, rows, lambda row: {
        "id": str(row.id), "actor_id": row.actor_id, "actor_email": row.actor.email if row.actor else None,
        "action": row.action, "subject_type": row.subject_type,
        "subject_id": row.subject_id, "detail": row.detail, "created_at": iso(row.created_at)})
