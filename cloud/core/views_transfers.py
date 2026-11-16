"""Stock transfers between shops: send (in transit), receive or cancel."""
from django.db import IntegrityError, transaction
from django.http import JsonResponse
from django.utils import timezone

from .api_common import APIError, audit, endpoint, fields, iso, membership_context, method, page, payload, product_quantity, scoped, text, uuid_value
from .models import Product, Shop, StockMovement, StockTransfer, StockTransferItem
from .stock_balance import balance
from .views_stock import STOCK_READ, STOCK_WRITE


def transfer_data(row):
    return {"id": str(row.id), "from_shop_id": str(row.from_shop_id), "to_shop_id": str(row.to_shop_id),
            "from_shop_name": row.from_shop.name, "to_shop_name": row.to_shop.name, "status": row.status,
            "note": row.note, "created_at": iso(row.created_at), "finished_at": iso(row.finished_at),
            "items": [{"product_id": str(i.product_id), "name": i.product.name, "unit": i.product.unit,
                       "quantity_milli": i.quantity_milli, "received_milli": i.received_milli}
                      for i in row.items.select_related("product").order_by("product__name")]}


def _move(row, product, shop, delta, step, actor):
    StockMovement.objects.create(tenant=row.tenant, shop=shop, product=product, delta_milli=delta, source="transfer",
                                 reason=f"Transferência {row.from_shop.name} → {row.to_shop.name}"[:240],
                                 reference=str(row.id), actor=actor, idempotency_key=f"transfer:{step}:{row.id}:{product.id}")


@endpoint
def transfers(request):
    method(request, "GET", "POST")
    member = membership_context(request, roles=STOCK_READ if request.method == "GET" else STOCK_WRITE,
                                write=request.method == "POST")
    tenant = member.tenant
    if request.method == "GET":
        rows = StockTransfer.objects.select_related("from_shop", "to_shop").filter(tenant=tenant)
        if request.GET.get("status"):
            rows = rows.filter(status=request.GET["status"])
        if request.GET.get("shop_id"):
            shop = scoped(Shop, tenant, request.GET["shop_id"])
            rows = rows.filter(from_shop=shop) | rows.filter(to_shop=shop)
        return page(request, rows.order_by("-created_at", "-id"), transfer_data)
    data = payload(request)
    fields(data, ("from_shop_id", "to_shop_id", "items", "note", "idempotency_key"),
           ("from_shop_id", "to_shop_id", "items", "idempotency_key"))
    origin, target = scoped(Shop, tenant, data["from_shop_id"]), scoped(Shop, tenant, data["to_shop_id"])
    if origin.id == target.id or not target.active:
        raise APIError("Escolha uma loja de destino ativa e diferente da origem")
    items = data["items"]
    if not isinstance(items, list) or not 1 <= len(items) <= 200:
        raise APIError("Informe de 1 a 200 itens")
    key = text(data["idempotency_key"], "Chave de repetição", 160)
    previous = StockTransfer.objects.filter(tenant=tenant, idempotency_key=key).first()
    if previous:
        return JsonResponse(transfer_data(previous))
    try:
        with transaction.atomic():
            row = StockTransfer.objects.create(tenant=tenant, from_shop=origin, to_shop=target, idempotency_key=key,
                                               note=text(data.get("note", ""), "Observação", 240, optional=True),
                                               created_by=member.user)
            seen = set()
            for item in items:
                if not isinstance(item, dict):
                    raise APIError("Item inválido")
                fields(item, ("product_id", "quantity_milli"), ("product_id", "quantity_milli"))
                product = Product.objects.select_for_update().filter(tenant=tenant, pk=uuid_value(item["product_id"], "Produto")).first()
                if product is None or not product.active or not product.stock_controlled or product.id in seen:
                    raise APIError("Produto inválido, repetido ou sem controle de estoque")
                seen.add(product.id)
                qty = product_quantity(product, item["quantity_milli"])
                if balance(tenant, origin, product) < qty:
                    raise APIError(f"Estoque insuficiente na origem: {product.name}", 409)
                StockTransferItem.objects.create(transfer=row, product=product, quantity_milli=qty)
                _move(row, product, origin, -qty, "out", member.user)
            audit(tenant, member.user, "stock.transfer_sent", row, {"items": len(seen)})
    except IntegrityError:
        raise APIError("Transferência já registrada", 409)
    return JsonResponse(transfer_data(row), status=201)


def _locked_sent(member, pk):
    row = (StockTransfer.objects.select_for_update().select_related("from_shop", "to_shop")
           .filter(tenant=member.tenant, pk=uuid_value(pk)).first())
    if row is None:
        raise APIError("Transferência não encontrada", 404)
    if row.status != "sent":
        raise APIError("Transferência já recebida ou cancelada", 409)
    return row


@endpoint
def transfer_receive(request, pk):
    method(request, "POST")
    member = membership_context(request, roles=STOCK_WRITE, write=True)
    data = payload(request)
    fields(data, ("items",), ())
    received = {}
    for item in data.get("items") or []:
        if not isinstance(item, dict):
            raise APIError("Item inválido")
        fields(item, ("product_id", "received_milli"), ("product_id", "received_milli"))
        received[uuid_value(item["product_id"], "Produto")] = item["received_milli"]
    with transaction.atomic():
        row = _locked_sent(member, pk)
        divergent = 0
        for item in row.items.select_related("product"):
            got = received.get(item.product_id, item.quantity_milli)
            got = product_quantity(item.product, got) if got else 0
            if got > item.quantity_milli:
                raise APIError(f"Recebido maior que o enviado: {item.product.name}")
            item.received_milli = got
            item.save(update_fields=["received_milli"])
            divergent += got != item.quantity_milli
            if got:
                _move(row, item.product, row.to_shop, got, "in", member.user)
        row.status, row.finished_by, row.finished_at = "received", member.user, timezone.now()
        row.save(update_fields=["status", "finished_by", "finished_at"])
        audit(row.tenant, member.user, "stock.transfer_received", row, {"divergent_items": divergent})
    return JsonResponse(transfer_data(row))


@endpoint
def transfer_cancel(request, pk):
    method(request, "POST")
    member = membership_context(request, roles=STOCK_WRITE, write=True)
    with transaction.atomic():
        row = _locked_sent(member, pk)
        for item in row.items.select_related("product"):
            _move(row, item.product, row.from_shop, item.quantity_milli, "back", member.user)
        row.status, row.finished_by, row.finished_at = "cancelled", member.user, timezone.now()
        row.save(update_fields=["status", "finished_by", "finished_at"])
        audit(row.tenant, member.user, "stock.transfer_cancelled", row)
    return JsonResponse(transfer_data(row))
