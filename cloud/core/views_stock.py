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
                                      "stock_controlled": p.stock_controlled,
                                      "stock_minimum_milli": p.stock_minimum_milli,
                                      "below_minimum": p.stock_controlled and quantities.get(p.id, 0) < p.stock_minimum_milli}
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
    if not product.stock_controlled:
        raise APIError("Produto de produção própria não usa controle de estoque")
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
        rows = StockReceipt.objects.filter(tenant=tenant).prefetch_related("items__product")
        shop_id = request.GET.get("shop_id")
        if shop_id:
            rows = rows.filter(shop=scoped(Shop, tenant, shop_id))
        return page(request, rows.order_by("-created_at", "-id"), receipt_data)
    data = payload(request)
    fields(data, ("shop_id", "supplier_id", "reference", "idempotency_key", "items", "source_file"),
           ("shop_id", "idempotency_key", "items"))
    shop = scoped(Shop, tenant, data["shop_id"])
    supplier = scoped(Supplier, tenant, data["supplier_id"]) if data.get("supplier_id") else None
    reference = text(data.get("reference", ""), "Referência", 160, optional=True)
    source_file = data.get("source_file")
    source_name = source_mime = source_hash = ""
    source_size = 0
    if source_file is not None:
        if not isinstance(source_file, dict):
            raise APIError("Arquivo de compra inválido")
        fields(source_file, ("name", "mime", "size", "sha256"), ("name", "size", "sha256"))
        source_name = text(source_file["name"], "Arquivo", 180)
        source_mime = text(source_file.get("mime", ""), "Tipo do arquivo", 100, optional=True)
        source_size = source_file["size"]
        if type(source_size) is not int or not 0 <= source_size <= 10 * 1024 * 1024:
            raise APIError("Tamanho do arquivo inválido")
        source_hash = text(source_file["sha256"], "Hash do arquivo", 64)
        if len(source_hash) != 64 or any(char not in "0123456789abcdefABCDEF" for char in source_hash):
            raise APIError("Hash do arquivo inválido")
    key = text(data["idempotency_key"], "Chave de repetição", 160)
    items = data["items"]
    if not isinstance(items, list) or not 1 <= len(items) <= 200:
        raise APIError("Itens da entrada inválidos")
    prepared, seen = [], set()
    for item in items:
        if not isinstance(item, dict):
            raise APIError("Item inválido")
        fields(item, ("product_id", "quantity_milli", "unit_cost_cents"),
               ("product_id", "quantity_milli", "unit_cost_cents"))
        product = scoped(Product, tenant, item["product_id"])
        if product.id in seen or not product.active:
            raise APIError("Produto repetido ou inativo")
        if not product.stock_controlled:
            raise APIError("Produto de produção própria não usa entrada de estoque")
        seen.add(product.id)
        prepared.append((product, product_quantity(product, item["quantity_milli"]),
                         money(item["unit_cost_cents"], "Custo da entrada")))
    content_hash = digest({"shop_id": str(shop.id), "supplier_id": str(supplier.id) if supplier else None,
                           "reference": reference, "source_file": {"name": source_name, "mime": source_mime,
                                                                      "size": source_size, "sha256": source_hash},
                           "items": sorted([(str(p.id), qty, cost) for p, qty, cost in prepared])})
    with transaction.atomic():
        type(tenant).objects.select_for_update().get(pk=tenant.pk)
        previous = StockReceipt.objects.filter(tenant=tenant, idempotency_key=key).first()
        if previous:
            if previous.content_hash != content_hash:
                raise APIError("Chave já usada com outros dados", 409)
            return JsonResponse(receipt_data(previous))
        row = StockReceipt.objects.create(tenant=tenant, shop=shop, supplier=supplier,
                                          reference=reference, source_file_name=source_name,
                                          source_file_mime=source_mime, source_file_sha256=source_hash,
                                          source_file_size=source_size, idempotency_key=key,
                                          content_hash=content_hash, created_by=member.user)
        for product, qty, cost in prepared:
            # Receipts are the only source of purchase cost.  Product.cost_cents
            # stores the weighted moving average used by margin reports.
            product = Product.objects.select_for_update().get(pk=product.pk)
            previous_quantity = (StockMovement.objects.filter(tenant=tenant, product=product)
                                 .aggregate(value=Sum("delta_milli"))["value"] or 0)
            if previous_quantity > 0 and product.cost_cents not in (None, 0):
                total_quantity = previous_quantity + qty
                average = ((previous_quantity * product.cost_cents) + (qty * cost)
                           + total_quantity // 2) // total_quantity
            else:
                average = cost
            StockReceiptItem.objects.create(receipt=row, product=product,
                                            quantity_milli=qty, unit_cost_cents=cost,
                                            average_cost_cents=average)
            StockMovement.objects.create(tenant=tenant, shop=shop, product=product,
                                         delta_milli=qty, source="receipt", reason="Entrada de mercadoria",
                                         reference=reference or str(row.id), actor=member.user,
                                         idempotency_key=f"receipt:{row.id}:{product.id}")
            product.cost_cents = average
            product.save(update_fields=["cost_cents", "updated_at"])
        audit(tenant, member.user, "stock.received", row)
    return JsonResponse(receipt_data(row), status=201)
