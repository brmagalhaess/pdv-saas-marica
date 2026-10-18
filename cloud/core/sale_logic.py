"""Immutable sale snapshots and exactly-once central stock effects."""
from collections import defaultdict
from datetime import datetime, timezone as utc_timezone
from decimal import Decimal, ROUND_HALF_UP

from django.utils import timezone

from .api_common import APIError, audit, digest, fields, money, quantity, scoped, text, uuid_value
from .models import Customer, FiscalDocument, Membership, Product, Sale, SaleItem, SalePayment, StockMovement
from .views_stock import balance


def event_time(value):
    if type(value) is not int or not 0 < value <= 253402300799999:
        raise APIError("Data do evento inválida")
    try:
        return datetime.fromtimestamp(value / 1000, tz=utc_timezone.utc)
    except (ValueError, OverflowError):
        raise APIError("Data do evento inválida")


def _member(tenant, pk, label):
    if not pk:
        return None
    row = Membership.objects.filter(tenant=tenant, pk=uuid_value(pk, label)).first()
    if row is None:
        raise APIError(f"{label} da venda não pertence à empresa")
    return row


def validate_sale(data, tenant):
    if not isinstance(data, dict):
        raise APIError("Venda inválida")
    required = ("request_id", "cash_session_id", "created_at", "gross_cents",
                "discount_cents", "total_cents", "change_cents", "items", "payments")
    fields(data, required + ("customer_id", "customer_name", "customer_document",
                             "operator_id", "seller_id"), required)
    request_id = text(data["request_id"], "Identificador da venda", 160)
    cash_session_id = text(data["cash_session_id"], "Sessão de caixa", 160)
    created_at = event_time(data["created_at"])
    customer = scoped(Customer, tenant, data["customer_id"]) if data.get("customer_id") else None
    operator = _member(tenant, data.get("operator_id"), "Operador")
    seller = _member(tenant, data.get("seller_id"), "Vendedor")
    customer_name = text(data.get("customer_name", ""), "Nome do consumidor", 180, optional=True)
    customer_document = text(data.get("customer_document", ""), "Documento do consumidor", 30, optional=True)
    items, payments = data["items"], data["payments"]
    if not isinstance(items, list) or not 1 <= len(items) <= 200:
        raise APIError("Itens inválidos")
    if not isinstance(payments, list) or not 1 <= len(payments) <= 20:
        raise APIError("Pagamentos inválidos")
    normalized_items = []
    gross_sum = discount_sum = total_sum = 0
    for item in items:
        if not isinstance(item, dict):
            raise APIError("Item inválido")
        required_item = ("product_id", "name", "unit", "quantity_milli", "unit_price_cents",
                         "gross_cents", "discount_cents", "line_total_cents")
        # stock_controlled is the terminal's catalog snapshot; the central
        # product record stays authoritative for stock effects.
        fields(item, required_item + ("ncm", "sku", "stock_controlled"), required_item)
        product = scoped(Product, tenant, item["product_id"])
        name = text(item["name"], "Nome do item", 180)
        if item["unit"] != product.unit:
            raise APIError("Unidade do produto não confere")
        qty = quantity(item["quantity_milli"])
        if product.unit == "UN" and qty % 1000:
            raise APIError("Produto UN exige quantidade inteira")
        price = money(item["unit_price_cents"], "Preço")
        gross = money(item["gross_cents"], "Bruto")
        discount = money(item["discount_cents"], "Desconto")
        line_total = money(item["line_total_cents"], "Total da linha")
        expected = int((Decimal(price) * Decimal(qty) / Decimal(1000)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
        if gross != expected or discount > gross or line_total != gross - discount:
            raise APIError("Totais do item não conferem")
        gross_sum += gross
        discount_sum += discount
        total_sum += line_total
        normalized_items.append((product, name, qty, price, gross, discount, line_total))
    gross_sale = money(data["gross_cents"], "Bruto da venda")
    discount_sale = money(data["discount_cents"], "Desconto da venda")
    total_sale = money(data["total_cents"], "Total da venda", allow_zero=False)
    change = money(data["change_cents"], "Troco")
    if (gross_sale, discount_sale, total_sale) != (gross_sum, discount_sum, total_sum):
        raise APIError("Totais da venda não conferem")
    if discount_sale * 10000 > gross_sale * tenant.max_discount_bps:
        raise APIError("Desconto acima do limite configurado pela administração", 403)
    normalized_payments = []
    paid = cash_paid = 0
    for payment in payments:
        if not isinstance(payment, dict):
            raise APIError("Pagamento inválido")
        fields(payment, ("method", "amount_cents"), ("method", "amount_cents"))
        if payment["method"] not in ("cash", "pix", "card", "card_credit", "card_debit"):
            raise APIError("Forma de pagamento inválida")
        amount = money(payment["amount_cents"], "Pagamento", allow_zero=False)
        normalized_payments.append((payment["method"], amount))
        paid += amount
        if payment["method"] == "cash":
            cash_paid += amount
    if paid - change != total_sale or change > cash_paid:
        raise APIError("Pagamentos não fecham a venda")
    return {"request_id": request_id, "cash_session_id": cash_session_id,
            "created_at": created_at, "customer": customer,
            "gross_cents": gross_sale, "discount_cents": discount_sale,
            "total_cents": total_sale, "change_cents": change,
            "items": normalized_items, "payments": normalized_payments,
            "customer_name": customer_name, "customer_document": customer_document,
            "operator": operator, "seller": seller}


def persist_sale(terminal, event):
    tenant, shop = terminal.tenant, terminal.shop
    data = event["data"]
    validated = validate_sale(data, tenant)
    sale_id = uuid_value(event["aggregate_id"], "ID da venda")
    content_hash = digest(data)
    previous = Sale.objects.filter(pk=sale_id).first()
    if previous:
        if previous.tenant_id != tenant.id or previous.terminal_id != terminal.id or previous.content_hash != content_hash:
            raise APIError("Venda já recebida com conteúdo diferente", 409)
        return previous
    if Sale.objects.filter(tenant=tenant, terminal=terminal, request_id=validated["request_id"]).exists():
        raise APIError("Identificador de venda já usado", 409)
    quantities = defaultdict(int)
    products = {}
    for product, _, qty, *_ in validated["items"]:
        quantities[product.id] += qty
        products[product.id] = product
    stock_status = "negative" if any(products[pid].stock_controlled and
                                     balance(tenant, shop, products[pid]) - qty < 0
                                     for pid, qty in quantities.items()) else "ok"
    sale = Sale.objects.create(id=sale_id, tenant=tenant, shop=shop, terminal=terminal,
                               customer=validated["customer"], customer_name_snapshot=validated["customer_name"],
                               customer_document_snapshot=validated["customer_document"],
                               operator=validated["operator"], seller=validated["seller"],
                               operator_name_snapshot=validated["operator"].name if validated["operator"] else "",
                               seller_name_snapshot=validated["seller"].name if validated["seller"] else "",
                               request_id=validated["request_id"],
                               cash_session_id=validated["cash_session_id"],
                               content_hash=content_hash, gross_cents=validated["gross_cents"],
                               discount_cents=validated["discount_cents"], total_cents=validated["total_cents"],
                               change_cents=validated["change_cents"], stock_status=stock_status,
                               created_at=validated["created_at"])
    for number, (product, name, qty, price, gross, discount, line_total) in enumerate(validated["items"]):
        SaleItem.objects.create(sale=sale, line_number=number, product=product, name=name,
                                unit=product.unit, quantity_milli=qty, unit_price_cents=price,
                                gross_cents=gross, discount_cents=discount, line_total_cents=line_total)
    for number, (method, amount) in enumerate(validated["payments"]):
        SalePayment.objects.create(sale=sale, line_number=number, method=method, amount_cents=amount)
    for product_id, qty in quantities.items():
        product = products[product_id]
        if not product.stock_controlled:
            continue
        StockMovement.objects.create(tenant=tenant, shop=shop, product=product,
                                     delta_milli=-qty, source="sale", reason="Venda",
                                     reference=str(sale.id), terminal=terminal, sale=sale,
                                     idempotency_key=f"sale:{sale.id}:{product_id}")
    audit(tenant, None, "sale.synced", sale, {"terminal_id": str(terminal.id), "stock_status": stock_status})
    return sale


def reverse_sale(sale, reason, status, actor=None, terminal=None, when=None):
    """Caller must hold a transaction and a row lock on sale."""
    reason = text(reason, "Motivo", 240)
    if status not in ("cancelled", "refunded"):
        raise APIError("Reversão inválida")
    if sale.status != "completed":
        if sale.status == status and sale.cancellation_reason == reason:
            return sale
        raise APIError("Venda já revertida com outro motivo ou operação", 409)
    if FiscalDocument.objects.filter(sale=sale, status__in=("authorized", "cancel_pending")).exists():
        raise APIError("Documento fiscal exige cancelamento fiscal separado", 409)
    quantities = defaultdict(int)
    for item in sale.items.all():
        quantities[item.product_id] += item.quantity_milli
    for product_id, qty in quantities.items():
        product = Product.objects.get(pk=product_id)
        if not product.stock_controlled:
            continue
        StockMovement.objects.create(tenant=sale.tenant, shop=sale.shop, product=product,
                                     delta_milli=qty, source="void", reason=reason,
                                     reference=str(sale.id), actor=actor, terminal=terminal or sale.terminal,
                                     sale=sale, idempotency_key=f"void:{sale.id}:{product_id}")
    sale.status = status
    sale.cancellation_reason = reason
    sale.cancelled_at = when or timezone.now()
    sale.save(update_fields=["status", "cancellation_reason", "cancelled_at"])
    audit(sale.tenant, actor, f"sale.{status}", sale, {"reason": reason})
    return sale
