from datetime import date

from django.db import transaction
from django.http import JsonResponse
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from .api_common import APIError, audit, endpoint, fields, membership_context, method, money, page, payload, require_feature, scoped, text
from .models import Customer, FinanceEntry, Shop, Supplier
from .serializers import finance as finance_data

READ = {"owner", "admin", "manager", "finance", "viewer"}
WRITE = {"owner", "admin", "finance"}


def _date(value):
    if not isinstance(value, str):
        raise APIError("Data inválida")
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise APIError("Data inválida")


@endpoint
def finance(request):
    method(request, "GET", "POST")
    member = membership_context(request, roles=READ if request.method == "GET" else WRITE,
                                write=request.method == "POST")
    require_feature(member, "finance")
    tenant = member.tenant
    if request.method == "GET":
        rows = FinanceEntry.objects.filter(tenant=tenant)
        for name in ("kind", "status"):
            if request.GET.get(name):
                rows = rows.filter(**{name: request.GET[name]})
        if request.GET.get("shop_id"):
            rows = rows.filter(shop=scoped(Shop, tenant, request.GET["shop_id"]))
        return page(request, rows.order_by("-created_at", "-id"), finance_data)
    data = payload(request)
    fields(data, ("kind", "description", "amount_cents", "due_date", "shop_id", "customer_id", "supplier_id"),
           ("kind", "description", "amount_cents", "due_date"))
    if data["kind"] not in ("payable", "receivable"):
        raise APIError("Tipo de título inválido")
    shop = scoped(Shop, tenant, data["shop_id"]) if data.get("shop_id") else None
    customer = scoped(Customer, tenant, data["customer_id"]) if data.get("customer_id") else None
    supplier = scoped(Supplier, tenant, data["supplier_id"]) if data.get("supplier_id") else None
    with transaction.atomic():
        row = FinanceEntry.objects.create(tenant=tenant, shop=shop, customer=customer, supplier=supplier,
                                          kind=data["kind"], description=text(data["description"], "Descrição", 240),
                                          amount_cents=money(data["amount_cents"], allow_zero=False),
                                          due_date=_date(data["due_date"]), created_by=member.user)
        audit(tenant, member.user, "finance.created", row)
    return JsonResponse(finance_data(row), status=201)


@endpoint
def finance_detail(request, pk):
    method(request, "GET", "PATCH", "DELETE")
    member = membership_context(request, roles=READ if request.method == "GET" else WRITE,
                                write=request.method != "GET")
    require_feature(member, "finance")
    row = scoped(FinanceEntry, member.tenant, pk)
    if request.method == "GET":
        return JsonResponse(finance_data(row))
    if row.status != "open":
        raise APIError("Título não está aberto", 409)
    if request.method == "DELETE":
        data = {"status": "cancelled"}
    else:
        data = payload(request)
        fields(data, ("description", "amount_cents", "due_date", "shop_id", "customer_id", "supplier_id"))
        if not data:
            raise APIError("Informe ao menos um campo")
        if row.sale_id and set(data) & {"amount_cents", "customer_id", "shop_id"}:
            raise APIError("Fiado gerado por venda: altere apenas descrição ou vencimento; use devolução ou cancelamento da venda", 409)
    values = {}
    for key, value in data.items():
        if key == "description":
            values[key] = text(value, "Descrição", 240)
        elif key == "amount_cents":
            values[key] = money(value, allow_zero=False)
        elif key == "due_date":
            values[key] = _date(value)
        elif key == "shop_id":
            values["shop"] = scoped(Shop, member.tenant, value) if value else None
        elif key == "customer_id":
            values["customer"] = scoped(Customer, member.tenant, value) if value else None
        elif key == "supplier_id":
            values["supplier"] = scoped(Supplier, member.tenant, value) if value else None
        else:
            values[key] = value
    with transaction.atomic():
        for key, value in values.items():
            setattr(row, key, value)
        row.save()
        audit(member.tenant, member.user, "finance.updated", row, {"fields": list(values)})
    return JsonResponse(finance_data(row))


@endpoint
def settle(request, pk):
    method(request, "POST")
    member = membership_context(request, roles=WRITE, write=True)
    require_feature(member, "finance")
    data = payload(request)
    fields(data, ("paid_at", "amount_cents"))
    with transaction.atomic():
        row = FinanceEntry.objects.select_for_update().filter(tenant=member.tenant, pk=pk).first()
        if row is None:
            raise APIError("Título não encontrado", 404)
        amount = money(data.get("amount_cents", row.amount_cents), allow_zero=False)
        if amount != row.amount_cents:
            raise APIError("Liquidação parcial ainda não disponível")
        if row.status == "settled":
            return JsonResponse(finance_data(row))
        if row.status != "open":
            raise APIError("Título cancelado", 409)
        paid_at = timezone.now()
        if data.get("paid_at"):
            paid_at = parse_datetime(data["paid_at"]) if isinstance(data["paid_at"], str) else None
            if paid_at is None or timezone.is_naive(paid_at):
                raise APIError("Data de liquidação inválida")
        row.status = "settled"
        row.settled_at = paid_at
        row.save(update_fields=["status", "settled_at"])
        audit(member.tenant, member.user, "finance.settled", row, {"amount_cents": amount})
    return JsonResponse(finance_data(row))
