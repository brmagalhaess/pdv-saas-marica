"""Immutable sale snapshots and exactly-once central stock effects."""
from collections import defaultdict
from datetime import datetime, timedelta, timezone as utc_timezone
from decimal import Decimal, ROUND_HALF_UP

from django.utils import timezone

from . import commission_logic
from .credit_logic import CREDIT_METHODS, apply_sale_payments, reverse_sale_credit
from .api_common import APIError, audit, digest, fields, money, quantity, scoped, text, uuid_value
from .models import (Commission, Customer, FiscalDocument, Membership, PaymentMethod, Product, Promotion, Sale, SaleItem,
                     SalePayment, SaleReturn, SaleReturnItem, StockMovement, SyncConflict)
from .stock_balance import balance
from .station_logic import station_of

UNITS = ("UN", "KG", "L")
# NFC-e states (docs/NFCE.md). A contingency note was handed to the consumer: it counts as issued.
FISCAL_AUTHORIZED = ("authorized", "contingency")
FISCAL_IN_PROGRESS = ("pending",)
# Terminal clocks drift; a rule edited within this window is not reported as changed after the sale.
CLOCK_TOLERANCE = timedelta(minutes=5)


def open_conflict(terminal, event, reason, message, **detail):
    """Note for the administrator about an offline event: one row per event and reason."""
    row, _ = SyncConflict.objects.get_or_create(
        terminal=terminal, event_id=event["event_id"], reason=reason,
        defaults={"tenant": terminal.tenant, "shop": terminal.shop, "event_type": str(event.get("type"))[:40],
                  "aggregate_id": str(event.get("aggregate_id"))[:160], "detail": {**detail, "message": message}})
    return row


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
    """Integrity errors raise. Rules the company may have changed after an offline
    sale (discount limit, unit, stock control, deactivations) never do: the sale
    is kept as sold and each divergence is returned in `divergences`."""
    if not isinstance(data, dict):
        raise APIError("Venda inválida")
    required = ("request_id", "cash_session_id", "created_at", "gross_cents",
                "discount_cents", "total_cents", "change_cents", "items", "payments")
    fields(data, required + ("customer_id", "customer_name", "customer_document",
                             "operator_id", "seller_id", "promo_discount_cents"), required)
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
    divergences = []
    units, stock_control, inactive, promotions = [], [], [], []
    gross_sum = discount_sum = total_sum = promo_sum = 0
    for item in items:
        if not isinstance(item, dict):
            raise APIError("Item inválido")
        required_item = ("product_id", "name", "unit", "quantity_milli", "unit_price_cents",
                         "gross_cents", "discount_cents", "line_total_cents")
        # stock_controlled is the terminal's catalog snapshot; the central
        # product record stays authoritative for stock effects.
        fields(item, required_item + ("ncm", "sku", "stock_controlled", "promo_discount_cents",
                                      "promotion_id", "promotion_name"), required_item)
        product = scoped(Product, tenant, item["product_id"])
        name = text(item["name"], "Nome do item", 180)
        unit = item["unit"]
        if unit not in UNITS:
            raise APIError("Unidade do item inválida")
        qty = quantity(item["quantity_milli"])
        if unit == "UN" and qty % 1000:
            raise APIError("Produto UN exige quantidade inteira")
        line = {"product_id": str(product.id), "product_name": name, "quantity_milli": qty}
        if unit != product.unit:
            units.append({**line, "sold_unit": unit, "current_unit": product.unit})
        if type(item.get("stock_controlled")) is bool and item["stock_controlled"] != product.stock_controlled:
            stock_control.append({**line, "sold_stock_controlled": item["stock_controlled"],
                                  "current_stock_controlled": product.stock_controlled})
        if not product.active:
            inactive.append(line)
        price = money(item["unit_price_cents"], "Preço")
        gross = money(item["gross_cents"], "Bruto")
        discount = money(item["discount_cents"], "Desconto")
        line_total = money(item["line_total_cents"], "Total da linha")
        # Promotion applied offline by the terminal. Accepted even if the rule was
        # edited or ended since: the sale already happened at that price.
        promo = money(item.get("promo_discount_cents", 0), "Desconto de promoção")
        promotion = scoped(Promotion, tenant, item["promotion_id"]) if item.get("promotion_id") else None
        if bool(promo) != bool(promotion):
            raise APIError("Desconto de promoção sem promoção correspondente")
        if promotion is not None and (not promotion.active or promotion.updated_at > created_at + CLOCK_TOLERANCE):
            promotions.append({**line, "promotion_id": str(promotion.id), "promotion_name": promotion.name,
                               "promo_discount_cents": promo, "promotion_active": promotion.active})
        expected = int((Decimal(price) * Decimal(qty) / Decimal(1000)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
        if gross != expected or promo + discount > gross or line_total != gross - promo - discount:
            raise APIError("Totais do item não conferem")
        gross_sum += gross
        promo_sum += promo
        discount_sum += discount
        total_sum += line_total
        normalized_items.append((product, name, qty, price, gross, discount, line_total,
                                 (promo, promotion, text(item.get("promotion_name") or (promotion.name if promotion else ""),
                                                         "Promoção", 80, optional=True)), unit))
    gross_sale = money(data["gross_cents"], "Bruto da venda")
    discount_sale = money(data["discount_cents"], "Desconto da venda")
    total_sale = money(data["total_cents"], "Total da venda", allow_zero=False)
    change = money(data["change_cents"], "Troco")
    promo_sale = money(data.get("promo_discount_cents", 0), "Promoções da venda")
    if (gross_sale, discount_sale, total_sale, promo_sale) != (gross_sum, discount_sum, total_sum, promo_sum):
        raise APIError("Totais da venda não conferem")
    # The administration limit applies to the operator's manual discount over the promotional price.
    base = gross_sale - promo_sale
    if discount_sale * 10000 > base * tenant.max_discount_bps:
        divergences.append(("discount_over_limit", "Venda no caixa com desconto acima do limite atual da administração.",
                            {"discount_cents": discount_sale, "base_cents": base, "limit_bps": tenant.max_discount_bps,
                             "discount_bps": discount_sale * 10000 // base if base else 0}))
    if units:
        divergences.append(("unit_changed", "A unidade do produto mudou depois da venda no caixa. Confira o estoque.",
                            {"items": units}))
    if stock_control:
        divergences.append(("stock_control_changed",
                            "O controle de estoque do produto mudou depois da venda no caixa. Confira o estoque.",
                            {"items": stock_control}))
    if inactive:
        divergences.append(("product_inactive", "Venda no caixa de produto desativado no cadastro.", {"items": inactive}))
    if promotions:
        divergences.append(("promotion_changed", "Venda no caixa com promoção alterada ou encerrada depois.",
                            {"items": promotions}))
    builtin = {row.kind: row for row in PaymentMethod.objects.filter(tenant=tenant).exclude(kind="other")}
    normalized_payments = []
    disabled = []
    paid = cash_paid = 0
    for payment in payments:
        if not isinstance(payment, dict):
            raise APIError("Pagamento inválido")
        fields(payment, ("method", "amount_cents", "payment_method_id"), ("method", "amount_cents"))
        if payment["method"] not in ("cash", "pix", "card", "card_credit", "card_debit", "other", *CREDIT_METHODS):
            raise APIError("Forma de pagamento inválida")
        # Accepted even if deactivated since: an offline sale already happened.
        configured = (scoped(PaymentMethod, tenant, payment.get("payment_method_id"))
                      if payment["method"] == "other" else None)
        if configured is not None and configured.kind != "other":
            raise APIError("Forma de pagamento inválida")
        if payment["method"] != "other" and payment.get("payment_method_id"):
            raise APIError("Forma de pagamento inválida")
        if payment["method"] in CREDIT_METHODS and customer is None:
            raise APIError("Venda a prazo ou com crédito na loja exige cliente cadastrado")
        amount = money(payment["amount_cents"], "Pagamento", allow_zero=False)
        config = configured or builtin.get(payment["method"])
        if config is not None and not config.active:
            disabled.append({"method": payment["method"], "payment_method_id": str(config.id), "name": config.name,
                             "amount_cents": amount})
        normalized_payments.append((payment["method"], amount, configured))
        paid += amount
        if payment["method"] == "cash":
            cash_paid += amount
    if paid - change != total_sale or change > cash_paid:
        raise APIError("Pagamentos não fecham a venda")
    if disabled:
        divergences.append(("payment_method_inactive", "Venda no caixa recebida em forma de pagamento desativada.",
                            {"payments": disabled}))
    return {"request_id": request_id, "cash_session_id": cash_session_id,
            "created_at": created_at, "customer": customer,
            "gross_cents": gross_sale, "discount_cents": discount_sale,
            "total_cents": total_sale, "change_cents": change,
            "items": normalized_items, "payments": normalized_payments, "promo_discount_cents": promo_sale,
            "customer_name": customer_name, "customer_document": customer_document,
            "operator": operator, "seller": seller, "divergences": divergences}


def persist_sale(terminal, event, conflicts=None):
    """Store a terminal sale; divergences from the current configuration are
    appended to `conflicts` as SyncConflict rows."""
    tenant, shop = terminal.tenant, terminal.shop
    data = event["data"]
    sale_id = uuid_value(event["aggregate_id"], "ID da venda")
    content_hash = digest(data)
    # A resend is recognized before any validation: rules may have changed
    # since the sale was accepted and it must not be judged again.
    previous = Sale.objects.filter(pk=sale_id).first()
    if previous:
        if previous.tenant_id != tenant.id or previous.terminal_id != terminal.id or previous.content_hash != content_hash:
            raise APIError("Venda já recebida com conteúdo diferente", 409)
        return previous
    validated = validate_sale(data, tenant)
    if Sale.objects.filter(tenant=tenant, terminal=terminal, request_id=validated["request_id"]).exists():
        raise APIError("Identificador de venda já usado", 409)
    quantities = defaultdict(int)
    products = {}
    for product, _, qty, *_ in validated["items"]:
        quantities[product.id] += qty
        products[product.id] = product
    station = station_of(event)
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
                               station_id=station[0], station_name=station[1],
                               content_hash=content_hash, gross_cents=validated["gross_cents"],
                               discount_cents=validated["discount_cents"], total_cents=validated["total_cents"],
                               promo_discount_cents=validated["promo_discount_cents"],
                               change_cents=validated["change_cents"], stock_status=stock_status,
                               created_at=validated["created_at"])
    for number, (product, name, qty, price, gross, discount, line_total, (promo, promotion, promo_name),
                 unit) in enumerate(validated["items"]):
        SaleItem.objects.create(sale=sale, line_number=number, product=product, name=name,
                                unit=unit, quantity_milli=qty, unit_price_cents=price,
                                gross_cents=gross, promo_discount_cents=promo, promotion=promotion,
                                promotion_name=promo_name, discount_cents=discount, line_total_cents=line_total)
    for number, (method, amount, configured) in enumerate(validated["payments"]):
        SalePayment.objects.create(sale=sale, line_number=number, method=method, amount_cents=amount,
                                   payment_method=configured)
    for product_id, qty in quantities.items():
        product = products[product_id]
        if not product.stock_controlled:
            continue
        StockMovement.objects.create(tenant=tenant, shop=shop, product=product,
                                     delta_milli=-qty, source="sale", reason="Venda",
                                     reference=str(sale.id), terminal=terminal, sale=sale,
                                     idempotency_key=f"sale:{sale.id}:{product_id}")
    apply_sale_payments(sale, validated["payments"])
    commission_logic.accrue(sale)
    audit(tenant, None, "sale.synced", sale, {"terminal_id": str(terminal.id), "stock_status": stock_status})
    for reason, message, detail in validated["divergences"]:
        conflict = open_conflict(terminal, event, reason, message, sale_id=str(sale.id), **detail)
        if conflicts is not None:
            conflicts.append(conflict)
    return sale


def reverse_sale(sale, reason, status, actor=None, terminal=None, when=None):
    """Reversal asked in the panel. Caller must hold a transaction and a row lock on sale."""
    reason = text(reason, "Motivo", 240)
    if status not in ("cancelled", "refunded"):
        raise APIError("Reversão inválida")
    if sale.status != "completed":
        if sale.status == status and sale.cancellation_reason == reason:
            return sale
        raise APIError("Venda já revertida com outro motivo ou operação", 409)
    if FiscalDocument.objects.filter(sale=sale, status__in=FISCAL_AUTHORIZED).exists():
        raise APIError("Documento fiscal exige cancelamento fiscal separado", 409)
    if sale.returns.exists():
        raise APIError("Venda com devolução registrada não pode ser cancelada; registre a devolução dos itens restantes", 409)
    return _apply_reversal(sale, reason, status, actor, terminal, when)


def void_from_terminal(terminal, event, conflicts=None):
    """sale.voided: the counter already gave stock and money back, so the
    reversal is never refused. Whatever the panel did to the same sale in the
    meantime (reversal, return, authorized NFC-e) becomes a conflict."""
    data = event["data"]
    reason = text(data["reason"], "Motivo", 240)
    sale = Sale.objects.select_for_update().filter(
        tenant=terminal.tenant, shop=terminal.shop, terminal=terminal, pk=uuid_value(event["aggregate_id"])).first()
    if sale is None:
        raise APIError("Venda original do estorno não encontrada", 404)
    found = []
    if sale.status != "completed":
        if sale.status != "cancelled" or sale.cancellation_reason != reason:
            found.append(open_conflict(
                terminal, event, "void_already_reversed",
                "Venda cancelada no caixa já estava cancelada ou estornada no painel. "
                "Confira se o cliente foi reembolsado duas vezes.",
                sale_id=str(sale.id), sale_status=sale.status, total_cents=sale.total_cents,
                panel_reason=sale.cancellation_reason, terminal_reason=reason))
    else:
        returned = defaultdict(int)
        for row in SaleReturnItem.objects.filter(sale_return__sale=sale):
            returned[row.product_id] += row.quantity_milli
        returned_cents = sum(SaleReturn.objects.filter(sale=sale).values_list("total_cents", flat=True))
        _apply_reversal(sale, reason, "cancelled", None, terminal, event_time(data["created_at"]), returned)
        if returned:
            found.append(open_conflict(
                terminal, event, "void_after_return",
                "Venda cancelada no caixa depois de uma devolução já registrada. Confira o reembolso e o estoque.",
                sale_id=str(sale.id), total_cents=sale.total_cents, returned_cents=returned_cents))
        fiscal = (FiscalDocument.objects.filter(sale=sale, status__in=FISCAL_AUTHORIZED + FISCAL_IN_PROGRESS,
                                                replaced_by__isnull=True).order_by("-created_at").first())
        if fiscal and fiscal.access_key and len(reason.strip()) >= 15:
            # The register already asked the manager's PIN and a reason: cancel the NFC-e within the
            # SEFAZ deadline (or right after authorization). Out of the deadline it becomes a conflict.
            from .nfce.service import request_cancel
            try:
                request_cancel(fiscal, reason)
                fiscal = None
            except APIError:
                pass
        if fiscal:
            message = ("Venda cancelada no caixa com NFC-e em processamento. Se a nota for autorizada, cancele-a na SEFAZ."
                       if fiscal.status in FISCAL_IN_PROGRESS else
                       "Venda cancelada no caixa com NFC-e autorizada. Cancele a nota fiscal dentro do prazo da SEFAZ.")
            found.append(open_conflict(
                terminal, event, "fiscal_cancel_required", message, sale_id=str(sale.id),
                total_cents=sale.total_cents, fiscal_document_id=str(fiscal.id), fiscal_status=fiscal.status,
                authorization=fiscal.authorization))
    if conflicts is not None:
        conflicts.extend(found)
    return sale


def _reverse_commission_after_returns(sale):
    """Returns already took their share of the commission: reverse only the rest."""
    row = Commission.objects.select_for_update().filter(sale=sale, reversal=False).first()
    if row is None:
        return
    partial = Commission.objects.filter(sale=sale, reversal=True, sale_return__isnull=False)
    if row.status == "pending":
        row.status = "cancelled"
        row.save(update_fields=["status"])
        partial.filter(status="pending").update(status="cancelled")
    elif row.status == "paid":
        live = partial.exclude(status="cancelled")
        amount = row.amount_cents + sum(live.values_list("amount_cents", flat=True))
        if amount > 0:
            base = max(0, row.base_cents + sum(live.values_list("base_cents", flat=True)))
            Commission.objects.get_or_create(
                sale=sale, reversal=True, sale_return__isnull=True,
                defaults={"tenant": sale.tenant, "seller": row.seller, "base_cents": -base, "amount_cents": -amount})


def _apply_reversal(sale, reason, status, actor, terminal, when, returned=None):
    quantities = defaultdict(int)
    for item in sale.items.all():
        quantities[item.product_id] += item.quantity_milli
    for product_id, qty in quantities.items():
        qty -= (returned or {}).get(product_id, 0)  # already back in stock through a return
        product = Product.objects.get(pk=product_id)
        if qty <= 0 or not product.stock_controlled:
            continue
        StockMovement.objects.create(tenant=sale.tenant, shop=sale.shop, product=product,
                                     delta_milli=qty, source="void", reason=reason,
                                     reference=str(sale.id), actor=actor, terminal=terminal or sale.terminal,
                                     sale=sale, idempotency_key=f"void:{sale.id}:{product_id}")
    reverse_sale_credit(sale)
    if returned:
        _reverse_commission_after_returns(sale)
    else:
        commission_logic.reverse(sale, actor)
    sale.status = status
    sale.cancellation_reason = reason
    sale.cancelled_at = when or timezone.now()
    sale.save(update_fields=["status", "cancellation_reason", "cancelled_at"])
    audit(sale.tenant, actor, f"sale.{status}", sale, {"reason": reason})
    return sale
