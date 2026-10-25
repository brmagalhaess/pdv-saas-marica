"""Guided inventory count (mobile friendly) with batch adjustment on close."""
from decimal import Decimal

from django.db import IntegrityError, transaction
from django.db.models import F, Q
from django.http import JsonResponse
from django.utils import timezone

from .api_common import APIError, audit, endpoint, fields, iso, membership_context, method, page, payload, product_quantity, scoped, text, uuid_value
from .models import Category, InventoryCount, InventoryCountItem, Product, Shop, StockMovement
from .stock_balance import balance, balances

MANAGE = {"owner", "admin", "manager", "inventory"}
COUNT = MANAGE | {"cashier"}
CLOSE = {"owner", "admin", "manager"}
SEES_EXPECTED = {"owner", "admin", "manager"}


def _scope(count):
    rows = Product.objects.filter(tenant=count.tenant, active=True, stock_controlled=True)
    return rows.filter(category=count.category) if count.category_id else rows


def _value(delta_milli, cost_cents):
    return int(Decimal(delta_milli) * cost_cents / 1000)


def _header(count):
    return {"id": str(count.id), "shop_id": str(count.shop_id), "status": count.status, "blind": count.blind,
            "category_id": str(count.category_id) if count.category_id else None,
            "category_name": count.category.name if count.category_id else None, "note": count.note,
            "created_at": iso(count.created_at), "closed_at": iso(count.closed_at),
            "counted_items": count.items.count(), "scope_items": _scope(count).count()}


def _item(row, show_expected):
    result = {"product_id": str(row.product_id), "name": row.product.name, "sku": row.product.sku,
              "barcode": row.product.barcode, "unit": row.product.unit, "counted_milli": row.counted_milli,
              "counted_at": iso(row.counted_at)}
    if show_expected:
        delta = row.counted_milli - row.balance_at_count_milli
        result.update({"balance_at_count_milli": row.balance_at_count_milli, "difference_milli": delta,
                       "difference_cents": _value(delta, row.product.cost_cents)})
    return result


def _open_count(member, pk):
    count = scoped(InventoryCount, member.tenant, pk)
    if count.status != "open":
        raise APIError("Inventário já encerrado", 409)
    return count


@endpoint
def inventories(request):
    method(request, "GET", "POST")
    member = membership_context(request, roles=COUNT if request.method == "GET" else MANAGE,
                                write=request.method == "POST")
    tenant = member.tenant
    if request.method == "GET":
        rows = InventoryCount.objects.select_related("category").filter(tenant=tenant)
        if request.GET.get("shop_id"):
            rows = rows.filter(shop=scoped(Shop, tenant, request.GET["shop_id"]))
        if request.GET.get("status"):
            rows = rows.filter(status=request.GET["status"])
        return page(request, rows.order_by("-created_at", "-id"), _header)
    data = payload(request)
    fields(data, ("shop_id", "category_id", "blind", "note"), ("shop_id",))
    shop = scoped(Shop, tenant, data["shop_id"])
    category = scoped(Category, tenant, data["category_id"]) if data.get("category_id") else None
    blind = data.get("blind", True)
    if type(blind) is not bool:
        raise APIError("Modo cego inválido")
    try:
        with transaction.atomic():
            count = InventoryCount.objects.create(tenant=tenant, shop=shop, category=category, blind=blind,
                                                  note=text(data.get("note", ""), "Observação", 240, optional=True),
                                                  created_by=member.user)
            audit(tenant, member.user, "inventory.opened", count)
    except IntegrityError:
        raise APIError("Já existe um inventário aberto nesta loja", 409)
    return JsonResponse(_header(count), status=201)


@endpoint
def inventory_detail(request, pk):
    method(request, "GET")
    member = membership_context(request, roles=COUNT)
    count = scoped(InventoryCount, member.tenant, pk)
    show = member.role in SEES_EXPECTED or not count.blind
    items = count.items.select_related("product").order_by("-counted_at")
    result = _header(count)
    result["items"] = [_item(row, show) for row in items]
    result["shows_expected"] = show
    if show:
        counted = {row.product_id for row in items}
        pending = _scope(count).exclude(pk__in=counted).order_by("name")[:500]
        current = balances(count.tenant, count.shop, [p.id for p in pending])
        result["pending"] = [{"product_id": str(p.id), "name": p.name, "sku": p.sku, "unit": p.unit,
                              "balance_milli": current.get(p.id, 0)} for p in pending]
        result["totals"] = {"gain_cents": sum(i["difference_cents"] for i in result["items"] if i["difference_cents"] > 0),
                            "loss_cents": sum(i["difference_cents"] for i in result["items"] if i["difference_cents"] < 0)}
    return JsonResponse(result)


@endpoint
def inventory_count(request, pk):
    """Record a count. `mode=add` sums (one scan per unit); `set` replaces."""
    method(request, "POST")
    member = membership_context(request, roles=COUNT, write=True)
    data = payload(request)
    fields(data, ("product_id", "code", "quantity_milli", "mode"), ("quantity_milli",))
    mode = data.get("mode", "add")
    if mode not in ("add", "set"):
        raise APIError("Modo de contagem inválido")
    with transaction.atomic():
        count = InventoryCount.objects.select_for_update().filter(tenant=member.tenant, pk=uuid_value(pk)).first()
        if count is None:
            raise APIError("Inventário não encontrado", 404)
        if count.status != "open":
            raise APIError("Inventário já encerrado", 409)
        scope = _scope(count)
        if data.get("product_id"):
            product = scope.filter(pk=uuid_value(data["product_id"], "Produto")).first()
        else:
            code = text(data.get("code", ""), "Código", 60)
            product = scope.filter(Q(barcode=code) | Q(sku__iexact=code)).first()
        if product is None:
            raise APIError("Produto não encontrado neste inventário (inativo, sem controle de estoque ou fora da categoria)", 404)
        if mode == "set":
            quantity = product_quantity(product, data["quantity_milli"], allow_negative=False) if data["quantity_milli"] else 0
        else:
            quantity = product_quantity(product, data["quantity_milli"])
        now = timezone.now()
        current = balance(count.tenant, count.shop, product)
        row = InventoryCountItem.objects.filter(count=count, product=product).first()
        if row is None:
            row = InventoryCountItem.objects.create(count=count, product=product, counted_milli=quantity,
                                                    balance_at_count_milli=current, counted_by=member.user, counted_at=now)
        else:
            row.counted_milli = quantity if mode == "set" else F("counted_milli") + quantity
            row.balance_at_count_milli, row.counted_by, row.counted_at = current, member.user, now
            row.save(update_fields=["counted_milli", "balance_at_count_milli", "counted_by", "counted_at"])
            row.refresh_from_db()
    return JsonResponse(_item(row, member.role in SEES_EXPECTED or not count.blind))


@endpoint
def inventory_close(request, pk):
    method(request, "POST")
    member = membership_context(request, roles=CLOSE, write=True)
    data = payload(request)
    fields(data, ("uncounted",), ())
    uncounted = data.get("uncounted", "ignore")
    if uncounted not in ("ignore", "zero"):
        raise APIError("Tratamento de itens não contados inválido")
    with transaction.atomic():
        count = InventoryCount.objects.select_for_update().filter(tenant=member.tenant, pk=uuid_value(pk)).first()
        if count is None:
            raise APIError("Inventário não encontrado", 404)
        if count.status != "open":
            raise APIError("Inventário já encerrado", 409)
        reason = f"Inventário {count.created_at:%d/%m/%Y}" + (f" · {count.category.name}" if count.category_id else "")
        adjustments, gain, loss = 0, 0, 0

        def adjust(product, delta):
            nonlocal adjustments, gain, loss
            if not delta:
                return
            StockMovement.objects.create(tenant=count.tenant, shop=count.shop, product=product, delta_milli=delta,
                                         source="inventory", reason=reason[:240], reference=str(count.id),
                                         actor=member.user, idempotency_key=f"inventory:{count.id}:{product.id}")
            adjustments += 1
            value = _value(delta, product.cost_cents)
            gain, loss = (gain + value, loss) if value > 0 else (gain, loss + value)

        counted = set()
        for row in count.items.select_related("product"):
            counted.add(row.product_id)
            adjust(row.product, row.counted_milli - row.balance_at_count_milli)
        if uncounted == "zero":
            pending = list(_scope(count).exclude(pk__in=counted))
            current = balances(count.tenant, count.shop, [p.id for p in pending])
            for product in pending:
                if current.get(product.id, 0) > 0:
                    adjust(product, -current[product.id])
        count.status, count.closed_by, count.closed_at = "closed", member.user, timezone.now()
        count.save(update_fields=["status", "closed_by", "closed_at"])
        audit(count.tenant, member.user, "inventory.closed", count,
              {"adjustments": adjustments, "gain_cents": gain, "loss_cents": loss, "uncounted": uncounted})
    return JsonResponse({**_header(count), "adjustments": adjustments, "gain_cents": gain, "loss_cents": loss})


@endpoint
def inventory_cancel(request, pk):
    method(request, "POST")
    member = membership_context(request, roles=CLOSE, write=True)
    with transaction.atomic():
        count = _open_count(member, pk)
        count.status, count.closed_by, count.closed_at = "cancelled", member.user, timezone.now()
        count.save(update_fields=["status", "closed_by", "closed_at"])
        audit(count.tenant, member.user, "inventory.cancelled", count)
    return JsonResponse(_header(count))
