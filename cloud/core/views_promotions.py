from django.db import transaction
from django.db.models import Q
from django.http import JsonResponse
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from shared.promotions import describe
from .api_common import APIError, audit, endpoint, fields, iso, membership_context, method, money, page, payload, scoped, text
from .models import Category, Product, Promotion
from .views_resources import CATALOG_WRITE

FIELDS = ("name", "kind", "product_id", "category_id", "price_cents", "min_quantity_milli",
          "buy_quantity", "pay_quantity", "starts_at", "ends_at", "active")


def _ms(value):
    return int(value.timestamp() * 1000) if value else None


def promotion_data(row):
    return {"id": str(row.id), "name": row.name, "kind": row.kind, "label": describe(terminal_data(row)),
            "product_id": str(row.product_id) if row.product_id else None,
            "product_name": row.product.name if row.product_id else None,
            "category_id": str(row.category_id) if row.category_id else None,
            "category_name": row.category.name if row.category_id else None,
            "price_cents": row.price_cents, "min_quantity_milli": row.min_quantity_milli,
            "buy_quantity": row.buy_quantity, "pay_quantity": row.pay_quantity,
            "starts_at": iso(row.starts_at), "ends_at": iso(row.ends_at), "active": row.active,
            "status": status(row)}


def terminal_data(row):
    """Shape consumed by shared.promotions on the terminal."""
    return {"id": str(row.id), "name": row.name, "kind": row.kind,
            "product_id": str(row.product_id) if row.product_id else None,
            "category": row.category.name if row.category_id else None,
            "price_cents": row.price_cents, "min_quantity_milli": row.min_quantity_milli,
            "buy_quantity": row.buy_quantity, "pay_quantity": row.pay_quantity,
            "starts_at_ms": _ms(row.starts_at), "ends_at_ms": _ms(row.ends_at), "active": row.active}


def status(row):
    now = timezone.now()
    if not row.active:
        return "inactive"
    if row.ends_at and row.ends_at <= now:
        return "ended"
    if row.starts_at and row.starts_at > now:
        return "scheduled"
    return "running"


def for_terminal(tenant):
    """Promotions a terminal may need while offline: active and not ended."""
    rows = (Promotion.objects.select_related("category").filter(tenant=tenant, active=True)
            .filter(Q(ends_at__isnull=True) | Q(ends_at__gt=timezone.now())))
    return [terminal_data(row) for row in rows]


def _when(value, label):
    if value in (None, ""):
        return None
    parsed = parse_datetime(value) if isinstance(value, str) else None
    if parsed is None:
        raise APIError(f"{label} inválida")
    return timezone.make_aware(parsed) if timezone.is_naive(parsed) else parsed


def _apply(row, data, tenant, creating):
    fields(data, FIELDS, ("name", "kind") if creating else ())
    if not data:
        raise APIError("Informe ao menos um campo")
    if "name" in data:
        row.name = text(data["name"], "Nome da promoção", 80)
    if "kind" in data:
        if data["kind"] not in ("price", "wholesale", "buy_x_pay_y"):
            raise APIError("Tipo de promoção inválido")
        row.kind = data["kind"]
    if "product_id" in data:
        row.product = scoped(Product, tenant, data["product_id"]) if data["product_id"] else None
    if "category_id" in data:
        row.category = scoped(Category, tenant, data["category_id"]) if data["category_id"] else None
    for key in ("price_cents", "min_quantity_milli"):
        if key in data:
            setattr(row, key, money(data[key], key) if data[key] not in (None, "") else None)
    for key in ("buy_quantity", "pay_quantity"):
        if key in data:
            value = data[key]
            if value not in (None, "") and (type(value) is not int or not 1 <= value <= 1000):
                raise APIError("Quantidade inválida")
            setattr(row, key, value or None)
    if "starts_at" in data:
        row.starts_at = _when(data["starts_at"], "Início")
    if "ends_at" in data:
        row.ends_at = _when(data["ends_at"], "Fim")
    if "active" in data:
        if type(data["active"]) is not bool:
            raise APIError("Estado inválido")
        row.active = data["active"]
    # Coherence of the final state, whatever fields were sent.
    if bool(row.product_id) == bool(row.category_id):
        raise APIError("Escolha um produto ou uma categoria (apenas um)")
    if row.kind in ("price", "wholesale") and not row.price_cents:
        raise APIError("Informe o preço promocional")
    if row.kind == "wholesale" and not row.min_quantity_milli:
        raise APIError("Informe a quantidade mínima do atacado")
    if row.kind == "buy_x_pay_y":
        if not (row.buy_quantity and row.pay_quantity and row.pay_quantity < row.buy_quantity):
            raise APIError("Leve X pague Y exige X maior que Y (ex.: leve 3 pague 2)")
        if row.product_id and row.product.unit != "UN":
            raise APIError("Leve X pague Y vale só para produtos vendidos por unidade")
    if row.product_id and row.kind in ("price", "wholesale") and row.price_cents >= row.product.price_cents:
        raise APIError("O preço promocional precisa ser menor que o preço normal do produto")
    if row.starts_at and row.ends_at and row.ends_at <= row.starts_at:
        raise APIError("O fim precisa ser depois do início")


@endpoint
def promotions(request):
    method(request, "GET", "POST")
    member = membership_context(request, roles=None if request.method == "GET" else CATALOG_WRITE,
                                write=request.method == "POST")
    tenant = member.tenant
    if request.method == "GET":
        rows = Promotion.objects.select_related("product", "category").filter(tenant=tenant)
        return page(request, rows.order_by("-active", "-created_at", "-id"), promotion_data)
    row = Promotion(tenant=tenant)
    _apply(row, payload(request), tenant, creating=True)
    with transaction.atomic():
        row.save()
        audit(tenant, member.user, "promotion.created", row)
    return JsonResponse(promotion_data(row), status=201)


@endpoint
def promotion_detail(request, pk):
    method(request, "GET", "PATCH")
    member = membership_context(request, roles=None if request.method == "GET" else CATALOG_WRITE,
                                write=request.method == "PATCH")
    row = scoped(Promotion, member.tenant, pk)
    if request.method == "GET":
        return JsonResponse(promotion_data(row))
    data = payload(request)
    _apply(row, data, member.tenant, creating=False)
    with transaction.atomic():
        row.save()
        audit(member.tenant, member.user, "promotion.updated", row, {"fields": list(data)})
    return JsonResponse(promotion_data(row))
