"""Purchases from an inbound NF-e XML: preview, product matching, stock entry and payables."""
import hashlib
import re
from collections import OrderedDict
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP

from django.db import IntegrityError, transaction
from django.http import JsonResponse
from django.utils import timezone

from . import nfe
from .api_common import APIError, audit, digest, endpoint, fields, membership_context, method, money, payload, scoped, text
from .models import Category, FinanceEntry, Product, Shop, StockReceipt, Supplier, SupplierProductCode
from .views_resources import default_product_image, generated_product_codes
from .views_stock import STOCK_WRITE, record_receipt

MAX_FACTOR_MILLI = 1_000_000 * 1000


def _xml(data):
    raw = data.get("xml")
    if not isinstance(raw, str) or not raw.strip():
        raise APIError("Envie o conteúdo do XML da NF-e")
    return raw.encode("utf-8")


def _digits(value):
    return re.sub(r"\D", "", value or "")


def _supplier(tenant, parsed):
    document = _digits(parsed["supplier"]["document"])
    if not document:
        return None
    for row in Supplier.objects.filter(tenant=tenant).exclude(document=""):
        if _digits(row.document) == document:
            return row
    return None


def _suggestions(tenant, supplier, items):
    memory = {}
    if supplier is not None:
        memory = {row.supplier_code: row for row in SupplierProductCode.objects.select_related("product")
                  .filter(tenant=tenant, supplier=supplier, supplier_code__in=[i["code"] for i in items])}
    barcodes = {i["ean"] for i in items if i["ean"]} | {i["ean_trib"] for i in items if i["ean_trib"]}
    by_barcode = {p.barcode: p for p in Product.objects.filter(tenant=tenant, barcode__in=barcodes, active=True)}
    result = []
    for item in items:
        known = memory.get(item["code"])
        if known and known.product.active:
            result.append({"product_id": str(known.product_id), "product_name": known.product.name,
                           "factor_milli": known.factor_milli, "source": "memory"})
            continue
        product = by_barcode.get(item["ean"]) or by_barcode.get(item["ean_trib"])
        if product:
            result.append({"product_id": str(product.id), "product_name": product.name,
                           "factor_milli": 1000, "source": "barcode"})
        else:
            result.append({"product_id": None, "product_name": None, "factor_milli": 1000, "source": None})
    return result


@endpoint
def nfe_preview(request):
    method(request, "POST")
    member = membership_context(request, roles=STOCK_WRITE)
    data = payload(request)
    fields(data, ("xml",), ("xml",))
    parsed = nfe.parse(_xml(data))
    tenant = member.tenant
    supplier = _supplier(tenant, parsed)
    imported = StockReceipt.objects.filter(tenant=tenant, idempotency_key=f"nfe:{parsed['key']}").first()
    tenant_doc = _digits(getattr(tenant, "document", ""))
    warnings = []
    if tenant_doc and parsed["recipient_document"] and _digits(parsed["recipient_document"]) != tenant_doc:
        warnings.append("O destinatário desta NF-e não é o CNPJ cadastrado da empresa.")
    if sum(i["total_cost_cents"] for i in parsed["items"]) != parsed["total_cents"]:
        warnings.append("A soma dos itens difere do total da nota (há valores fora dos itens, como frete da nota).")
    for item, suggestion in zip(parsed["items"], _suggestions(tenant, supplier, parsed["items"])):
        item["suggestion"] = suggestion
    return JsonResponse({**parsed, "supplier_id": str(supplier.id) if supplier else None,
                         "already_imported": bool(imported),
                         "receipt_id": str(imported.id) if imported else None, "warnings": warnings})


def _new_product(tenant, item, spec):
    fields(spec, ("name", "price_cents", "category_id", "unit"), ("price_cents",))
    unit = spec.get("unit") or item["suggested_unit"]
    if unit not in ("UN", "KG", "L"):
        raise APIError("Unidade inválida")
    category = scoped(Category, tenant, spec["category_id"]) if spec.get("category_id") else None
    name = text(spec.get("name") or item["name"], "Nome do produto", 180)
    codes = generated_product_codes(tenant)
    barcode = item["ean"] or item["ean_trib"]
    if barcode and not Product.objects.filter(tenant=tenant, barcode=barcode).exists():
        codes["barcode"] = barcode
    return Product.objects.create(tenant=tenant, name=name, unit=unit, category=category,
                                  ncm=item["ncm"] if len(item["ncm"]) in (7, 8) else "",
                                  price_cents=money(spec["price_cents"], "Preço de venda"),
                                  image_url=default_product_image(name, category.name if category else ""), **codes)


@endpoint
def nfe_import(request):
    method(request, "POST")
    member = membership_context(request, roles=STOCK_WRITE, write=True)
    tenant = member.tenant
    data = payload(request)
    fields(data, ("xml", "shop_id", "lines", "create_payables"), ("xml", "shop_id", "lines"))
    raw = _xml(data)
    parsed = nfe.parse(raw)
    shop = scoped(Shop, tenant, data["shop_id"])
    key = f"nfe:{parsed['key']}"
    if StockReceipt.objects.filter(tenant=tenant, idempotency_key=key).exists():
        raise APIError("Esta NF-e já foi importada", 409)
    lines = data["lines"]
    if not isinstance(lines, list):
        raise APIError("Informe o destino de cada item")
    by_number = {i["n_item"]: i for i in parsed["items"]}
    decisions = {}
    for line in lines:
        if not isinstance(line, dict):
            raise APIError("Item inválido")
        fields(line, ("n_item", "action", "product_id", "factor_milli", "new_product"), ("n_item", "action"))
        if line["n_item"] not in by_number or line["n_item"] in decisions:
            raise APIError("Item da NF-e inexistente ou repetido")
        if line["action"] not in ("map", "new", "skip"):
            raise APIError("Ação inválida para o item")
        decisions[line["n_item"]] = line
    if set(decisions) != set(by_number):
        raise APIError("Informe o destino de todos os itens da NF-e (vincular, criar ou ignorar)")
    try:
        with transaction.atomic():
            type(tenant).objects.select_for_update().get(pk=tenant.pk)
            supplier = _supplier(tenant, parsed)
            if supplier is None and parsed["supplier"]["document"]:
                supplier = Supplier.objects.create(tenant=tenant, name=parsed["supplier"]["name"] or "Fornecedor",
                                                   document=parsed["supplier"]["document"])
            totals = OrderedDict()  # product_id -> [product, quantity_milli, cost_cents_total]
            for n_item, line in sorted(decisions.items()):
                item = by_number[n_item]
                if line["action"] == "skip":
                    continue
                factor = line.get("factor_milli", 1000)
                if type(factor) is not int or not 1 <= factor <= MAX_FACTOR_MILLI:
                    raise APIError(f"Item {n_item}: fator de conversão inválido")
                if line["action"] == "new":
                    if not isinstance(line.get("new_product"), dict):
                        raise APIError(f"Item {n_item}: informe os dados do produto novo")
                    product = _new_product(tenant, item, line["new_product"])
                else:
                    product = scoped(Product, tenant, line.get("product_id"))
                    if not product.active:
                        raise APIError(f"Item {n_item}: produto inativo")
                if not product.stock_controlled:
                    raise APIError(f"Item {n_item}: produto de produção própria não usa entrada de estoque")
                quantity = int((Decimal(item["quantity_milli"]) * factor / 1000).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
                if quantity <= 0 or (product.unit == "UN" and quantity % 1000):
                    raise APIError(f"Item {n_item} ({item['name']}): a quantidade convertida precisa ser inteira para produto em unidade; ajuste o fator")
                entry = totals.setdefault(product.id, [product, 0, 0])
                entry[1] += quantity
                entry[2] += item["total_cost_cents"]
                if supplier is not None and item["code"]:
                    SupplierProductCode.objects.update_or_create(
                        supplier=supplier, supplier_code=item["code"],
                        defaults={"tenant": tenant, "product": product, "factor_milli": factor})
            if not totals:
                raise APIError("Nenhum item foi vinculado para entrada de estoque")
            prepared = [(product, qty, int((Decimal(cost) * 1000 / qty).quantize(Decimal("1"), rounding=ROUND_HALF_UP)))
                        for product, qty, cost in totals.values()]
            reference = f"NF-e {parsed['number']} série {parsed['series']}"
            source = (f"NFe{parsed['key']}.xml", "application/xml", len(raw), hashlib.sha256(raw).hexdigest())
            content_hash = digest({"key": parsed["key"], "items": sorted((str(p.id), q, c) for p, q, c in prepared)})
            receipt, _ = record_receipt(tenant, member.user, shop, supplier, reference, key, content_hash, prepared, source)
            payables = []
            if data.get("create_payables", True):
                installments = parsed["installments"] or [{
                    "number": "1", "amount_cents": parsed["total_cents"],
                    "due_date": (timezone.localdate() + timedelta(days=30)).isoformat()}]
                for index, inst in enumerate(installments, 1):
                    if inst["amount_cents"] <= 0:
                        continue
                    payables.append(FinanceEntry.objects.create(
                        tenant=tenant, shop=shop, supplier=supplier, kind="payable", created_by=member.user,
                        description=f"{reference} · parcela {inst['number'] or index}"[:240],
                        amount_cents=inst["amount_cents"], due_date=date.fromisoformat(inst["due_date"])))
            audit(tenant, member.user, "purchase.nfe_imported", receipt,
                  {"key": parsed["key"], "items": len(prepared), "payables": len(payables)})
    except IntegrityError:
        raise APIError("Esta NF-e já foi importada ou há código duplicado", 409)
    return JsonResponse({"receipt_id": str(receipt.id), "supplier_id": str(supplier.id) if supplier else None,
                         "items": len(prepared), "payables": [{"id": str(p.id), "amount_cents": p.amount_cents,
                                                               "due_date": p.due_date.isoformat()} for p in payables]},
                        status=201)
