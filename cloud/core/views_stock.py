"""One central ledger for manual, receipt and terminal sale movements."""
from django.db import IntegrityError, transaction
from django.db.models import Q, Sum
from django.http import JsonResponse

from .api_common import APIError, audit, digest, endpoint, fields, membership_context, method, money, page, payload, product_quantity, scoped, text
from .models import Product, Shop, StockMovement, StockReceipt, StockReceiptItem, Supplier
from .serializers import movement as movement_data, receipt as receipt_data


STOCK_READ = {"owner", "admin", "manager", "inventory", "finance", "viewer"}
STOCK_WRITE = {"owner", "admin", "manager", "inventory"}


def balance(tenant, shop, product):
    return (StockMovement.objects.filter(tenant=tenant, shop=shop, product=product)
            .aggregate(value=Sum("delta_milli"))["value"] or 0)


def _paging(request):
    try:
        limit = int(request.GET.get("limit", "50"))
        offset = int(request.GET.get("cursor", "0"))
    except ValueError:
        raise APIError("Paginação inválida")
    if not 1 <= limit <= 100 or not 0 <= offset <= 10**7:
        raise APIError("Paginação inválida")
    return limit, offset


@endpoint
def stock(request):
    method(request, "GET")
    member = membership_context(request)
    shop = scoped(Shop, member.tenant, request.GET.get("shop_id"))
    query = request.GET.get("q", "").strip()
    if len(query) > 120:
        raise APIError("Busca muito longa")
    products = Product.objects.filter(tenant=member.tenant)
    if query:
        products = products.filter(Q(name__icontains=query) | Q(sku__icontains=query) | Q(barcode__icontains=query))
    limit, offset = _paging(request)
    rows = list(products.order_by("name", "id")[offset:offset + limit + 1])
    shown = rows[:limit]
    values = (StockMovement.objects.filter(tenant=member.tenant, shop=shop, product_id__in=[p.id for p in shown])
              .values("product_id").annotate(value=Sum("delta_milli")))
    quantities = {row["product_id"]: row["value"] for row in values}
    return JsonResponse({"results": [{"product_id": str(p.id), "sku": p.sku, "name": p.name,
                                      "unit": p.unit, "quantity_milli": quantities.get(p.id, 0),
                                      "stock_minimum_milli": p.stock_minimum_milli,
                                      "below_minimum": quantities.get(p.id, 0) < p.stock_minimum_milli}
                                     for p in shown],
                         "next_cursor": str(offset + limit) if len(rows) > limit else None})


@endpoint
def movements(request):
    method(request, "GET", "POST")
    member = membership_context(request, roles=STOCK_READ if request.method == "GET" else STOCK_WRITE,
                                write=request.method == "POST")
    tenant = member.tenant
    if request.method == "GET":
        shop = scoped(Shop, tenant, request.GET.get("shop_id"))
        rows = StockMovement.objects.filter(tenant=tenant, shop=shop)
        product_id = request.GET.get("product_id")
        if product_id:
            product = scoped(Product, tenant, product_id)
            rows = rows.filter(product=product)
        return page(request, rows.order_by("-created_at", "-id"), movement_data)
    data = payload(request)
    fields(data, ("shop_id", "product_id", "delta_milli", "reason", "idempotency_key"),
           ("shop_id", "product_id", "delta_milli", "reason", "idempotency_key"))
    shop = scoped(Shop, tenant, data["shop_id"])
    product = scoped(Product, tenant, data["product_id"])
    delta = product_quantity(product, data["delta_milli"], allow_negative=True)
    reason = text(data["reason"], "Motivo", 240)
    key = text(data["idempotency_key"], "Chave de repetição", 160)
    with transaction.atomic():
        Product.objects.select_for_update().get(pk=product.pk)
        previous = StockMovement.objects.filter(tenant=tenant, source="manual", idempotency_key=key).first()
        if previous:
            if (previous.shop_id, previous.product_id, previous.delta_milli, previous.reason) != (shop.id, product.id, delta, reason):
                raise APIError("Chave já usada com outros dados", 409)
            return JsonResponse({"movement": movement_data(previous),
                                 "quantity_milli": balance(tenant, shop, product), "replayed": True})
        if balance(tenant, shop, product) + delta < 0:
            raise APIError("Estoque insuficiente para ajuste", 409)
        row = StockMovement.objects.create(tenant=tenant, shop=shop, product=product,
                                           delta_milli=delta, source="manual", reason=reason,
                                           idempotency_key=key, actor=member.user)
        audit(tenant, member.user, "stock.moved", row)
        current = balance(tenant, shop, product)
    return JsonResponse({"movement": movement_data(row), "quantity_milli": current}, status=201)


@endpoint
def receipts(request):
    method(request, "GET", "POST")
    member = membership_context(request, roles=STOCK_READ if request.method == "GET" else STOCK_WRITE,
                                write=request.method == "POST")
    tenant = member.tenant
    if request.method == "GET":
        rows = StockReceipt.objects.filter(tenant=tenant).prefetch_related("items")
        shop_id = request.GET.get("shop_id")
        if shop_id:
            rows = rows.filter(shop=scoped(Shop, tenant, shop_id))
        return page(request, rows.order_by("-created_at", "-id"), receipt_data)
    data = payload(request)
    fields(data, ("shop_id", "supplier_id", "reference", "idempotency_key", "items"),
           ("shop_id", "idempotency_key", "items"))
    shop = scoped(Shop, tenant, data["shop_id"])
    supplier = scoped(Supplier, tenant, data["supplier_id"]) if data.get("supplier_id") else None
    reference = text(data.get("reference", ""), "Referência", 160, optional=True)
    key = text(data["idempotency_key"], "Chave de repetição", 160)
    items = data["items"]
    if not isinstance(items, list) or not 1 <= len(items) <= 200:
        raise APIError("Itens da entrada inválidos")
    prepared, seen = [], set()
    for item in items:
        if not isinstance(item, dict):
            raise APIError("Item inválido")
        fields(item, ("product_id", "quantity_milli", "unit_cost_cents"), ("product_id", "quantity_milli"))
        product = scoped(Product, tenant, item["product_id"])
        if product.id in seen or not product.active:
            raise APIError("Produto repetido ou inativo")
        seen.add(product.id)
        prepared.append((product, product_quantity(product, item["quantity_milli"]),
                         money(item.get("unit_cost_cents", product.cost_cents), "Custo")))
    content_hash = digest({"shop_id": str(shop.id), "supplier_id": str(supplier.id) if supplier else None,
                           "reference": reference,
                           "items": sorted([(str(p.id), qty, cost) for p, qty, cost in prepared])})
    with transaction.atomic():
        type(tenant).objects.select_for_update().get(pk=tenant.pk)
        previous = StockReceipt.objects.filter(tenant=tenant, idempotency_key=key).first()
        if previous:
            if previous.content_hash != content_hash:
                raise APIError("Chave já usada com outros dados", 409)
            return JsonResponse(receipt_data(previous))
        row = StockReceipt.objects.create(tenant=tenant, shop=shop, supplier=supplier,
                                          reference=reference, idempotency_key=key,
                                          content_hash=content_hash, created_by=member.user)
        for product, qty, cost in prepared:
            StockReceiptItem.objects.create(receipt=row, product=product,
                                            quantity_milli=qty, unit_cost_cents=cost)
            StockMovement.objects.create(tenant=tenant, shop=shop, product=product,
                                         delta_milli=qty, source="receipt", reason="Entrada de mercadoria",
                                         reference=reference or str(row.id), actor=member.user,
                                         idempotency_key=f"receipt:{row.id}:{product.id}")
        audit(tenant, member.user, "stock.received", row)
    return JsonResponse(receipt_data(row), status=201)
