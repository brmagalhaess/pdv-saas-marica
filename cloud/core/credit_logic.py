"""Credit-account (fiado) sales, store credit and partial returns.

All functions run inside the sync/reversal transaction and are idempotent:
terminals resend events after a lost acknowledgement.
"""
from collections import defaultdict
from datetime import timedelta
from decimal import Decimal, ROUND_HALF_UP

from django.db.models import Sum
from django.utils import timezone

from .api_common import APIError, audit, digest, money, quantity, scoped, text, uuid_value
from .models import (Commission, Customer, CustomerCredit, FinanceEntry, Membership, Sale, SaleReturn,
                     SaleReturnItem, StockMovement)

CREDIT_METHODS = ("credit_account", "store_credit")
REFUNDS = ("cash", "store_credit", "account")


def return_line_amount(line_total_cents, line_quantity_milli, returned_milli):
    """Share of a sold line refunded for a partial quantity; same rule on the terminal."""
    return int((Decimal(line_total_cents) * Decimal(returned_milli) / Decimal(line_quantity_milli))
               .quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def balances(tenant, customer_ids=None):
    """{customer_id: (open fiado cents, store credit cents)}."""
    entries = FinanceEntry.objects.filter(tenant=tenant, kind="receivable", status="open", sale__isnull=False)
    credits = CustomerCredit.objects.filter(tenant=tenant)
    if customer_ids is not None:
        entries, credits = entries.filter(customer_id__in=customer_ids), credits.filter(customer_id__in=customer_ids)
    open_by = dict(entries.values_list("customer_id").annotate(v=Sum("amount_cents")))
    credit_by = dict(credits.values_list("customer_id").annotate(v=Sum("delta_cents")))
    ids = set(open_by) | set(credit_by)
    return {pk: (open_by.get(pk, 0), credit_by.get(pk, 0)) for pk in ids}


def apply_sale_payments(sale, payments):
    """Create the receivable / credit usage implied by a synced sale."""
    for number, (method, amount) in enumerate(payments):
        if method == "credit_account":
            due = timezone.localdate(sale.created_at) + timedelta(days=sale.tenant.credit_term_days)
            FinanceEntry.objects.get_or_create(
                tenant=sale.tenant, sale=sale, kind="receivable", description=f"Venda a prazo {str(sale.id)[:8]} · parcela {number + 1}",
                defaults={"shop": sale.shop, "customer": sale.customer, "amount_cents": amount, "due_date": due})
        elif method == "store_credit":
            CustomerCredit.objects.get_or_create(
                idempotency_key=f"sale:{sale.id}:{number}",
                defaults={"tenant": sale.tenant, "customer": sale.customer, "delta_cents": -amount, "source": "sale",
                          "description": f"Usado na venda {str(sale.id)[:8]}", "sale": sale})


def reverse_sale_credit(sale):
    """Undo fiado and store credit of a cancelled sale."""
    for entry in FinanceEntry.objects.select_for_update().filter(sale=sale, kind="receivable"):
        if entry.status == "open":
            entry.status = "cancelled"
            entry.save(update_fields=["status"])
        elif entry.status == "settled":
            CustomerCredit.objects.get_or_create(
                idempotency_key=f"cancel:{sale.id}:{entry.id}",
                defaults={"tenant": sale.tenant, "customer": entry.customer, "delta_cents": entry.amount_cents,
                          "source": "sale_cancel", "sale": sale,
                          "description": f"Fiado já pago da venda cancelada {str(sale.id)[:8]}"})
    for used in CustomerCredit.objects.filter(sale=sale, source="sale"):
        CustomerCredit.objects.get_or_create(
            idempotency_key=f"cancel-credit:{used.id}",
            defaults={"tenant": sale.tenant, "customer": used.customer, "delta_cents": -used.delta_cents,
                      "source": "sale_cancel", "sale": sale,
                      "description": f"Crédito devolvido pela venda cancelada {str(sale.id)[:8]}"})


def validate_return(data, tenant, aggregate_id):
    from .sale_logic import event_time
    required = ("sale_id", "items", "total_cents", "refund_method", "reason", "operator_id", "created_at")
    allowed = required + ("customer_id", "supervisor_id", "cash_session_id")
    if not isinstance(data, dict) or set(data) - set(allowed) or any(k not in data for k in required):
        raise APIError("Campos inválidos ou ausentes")
    uuid_value(aggregate_id, "Devolução")
    sale = Sale.objects.filter(tenant=tenant, pk=uuid_value(data["sale_id"], "Venda")).first()
    if sale is None:
        raise APIError("Venda da devolução não encontrada", 404)
    if sale.status != "completed":
        raise APIError("Venda cancelada não aceita devolução", 409)
    refund = data["refund_method"]
    if refund not in REFUNDS:
        raise APIError("Forma de devolução inválida")
    customer = sale.customer
    if data.get("customer_id"):
        customer = scoped(Customer, tenant, data["customer_id"])
    if refund == "store_credit" and customer is None:
        raise APIError("Crédito na loja exige cliente cadastrado")
    if refund == "account" and not sale.payments.filter(method="credit_account").exists():
        raise APIError("Abatimento no fiado exige venda a prazo")
    items = data["items"]
    if not isinstance(items, list) or not 1 <= len(items) <= 200:
        raise APIError("Itens da devolução inválidos")
    sold = {item.product_id: item for item in sale.items.all()}
    already = defaultdict(int)
    for row in SaleReturnItem.objects.filter(sale_return__sale=sale).exclude(sale_return_id=uuid_value(aggregate_id)):
        already[row.product_id] += row.quantity_milli
    normalized, total = [], 0
    for item in items:
        if not isinstance(item, dict) or set(item) != {"product_id", "quantity_milli", "amount_cents"}:
            raise APIError("Item da devolução inválido")
        product_id = uuid_value(item["product_id"], "Produto")
        line = sold.get(product_id)
        if line is None:
            raise APIError("Produto não pertence à venda")
        qty = quantity(item["quantity_milli"])
        if line.unit == "UN" and qty % 1000:
            raise APIError("Produto UN exige quantidade inteira")
        if already[product_id] + qty > line.quantity_milli:
            raise APIError("Quantidade devolvida maior que a vendida", 409)
        amount = money(item["amount_cents"], "Valor devolvido", allow_zero=False)
        if amount != return_line_amount(line.line_total_cents, line.quantity_milli, qty):
            raise APIError("Valor devolvido não confere com a venda")
        already[product_id] += qty
        total += amount
        normalized.append((line.product, qty, amount))
    if money(data["total_cents"], "Total devolvido", allow_zero=False) != total:
        raise APIError("Total da devolução não confere")
    member = lambda key: (Membership.objects.filter(tenant=tenant, pk=uuid_value(data[key], "Pessoa")).first()
                          if data.get(key) else None)
    return {"sale": sale, "customer": customer, "refund": refund, "items": normalized, "total": total,
            "reason": text(data["reason"], "Motivo", 240), "operator": member("operator_id"),
            "supervisor": member("supervisor_id"), "cash_session_id": text(data.get("cash_session_id") or "", "Caixa", 160, optional=True),
            "created_at": event_time(data["created_at"])}


def apply_return(terminal, event):
    tenant = terminal.tenant
    content_hash = digest(event["data"])
    previous = SaleReturn.objects.filter(pk=uuid_value(event["aggregate_id"])).first()
    if previous:
        if previous.tenant_id != tenant.id or previous.content_hash != content_hash:
            raise APIError("Devolução já recebida com conteúdo diferente", 409)
        return previous
    v = validate_return(event["data"], tenant, event["aggregate_id"])
    sale = Sale.objects.select_for_update().get(pk=v["sale"].pk)
    ret = SaleReturn.objects.create(id=uuid_value(event["aggregate_id"]), tenant=tenant, shop=terminal.shop,
                                    terminal=terminal, sale=sale, customer=v["customer"], operator=v["operator"],
                                    supervisor=v["supervisor"], cash_session_id=v["cash_session_id"],
                                    refund_method=v["refund"], total_cents=v["total"], reason=v["reason"],
                                    content_hash=content_hash, created_at=v["created_at"])
    for product, qty, amount in v["items"]:
        SaleReturnItem.objects.create(sale_return=ret, product=product, quantity_milli=qty, amount_cents=amount)
        if product.stock_controlled:
            StockMovement.objects.create(tenant=tenant, shop=terminal.shop, product=product, delta_milli=qty,
                                         source="return", reason=f"Devolução: {v['reason']}"[:240],
                                         reference=str(ret.id), terminal=terminal, sale=sale,
                                         idempotency_key=f"return:{ret.id}:{product.id}")
    if v["refund"] == "store_credit":
        _issue_credit(ret, v["customer"], v["total"], "return", f"Devolução da venda {str(sale.id)[:8]}")
    elif v["refund"] == "account":
        remaining = v["total"]
        for entry in FinanceEntry.objects.select_for_update().filter(sale=sale, kind="receivable", status="open").order_by("due_date"):
            if remaining <= 0:
                break
            cut = min(remaining, entry.amount_cents)
            remaining -= cut
            if cut == entry.amount_cents:
                entry.status = "cancelled"
                entry.description = (entry.description + " · abatido por devolução")[:240]
                entry.save(update_fields=["status", "description"])
            else:
                entry.amount_cents -= cut
                entry.save(update_fields=["amount_cents"])
        if remaining > 0:  # fiado already paid: the customer keeps the difference as credit
            _issue_credit(ret, v["customer"] or sale.customer, remaining, "account_overpaid",
                          f"Devolução de fiado já pago, venda {str(sale.id)[:8]}")
    commission = Commission.objects.filter(sale=sale, reversal=False).exclude(status="cancelled").first()
    if commission and sale.total_cents:
        share = int((Decimal(commission.amount_cents) * v["total"] / sale.total_cents).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
        if share:
            Commission.objects.get_or_create(sale_return=ret, defaults={
                "tenant": tenant, "sale": sale, "seller": commission.seller, "reversal": True,
                "base_cents": -min(v["total"], commission.base_cents), "amount_cents": -share})
    audit(tenant, None, "sale.returned", ret, {"sale_id": str(sale.id), "refund": v["refund"], "total_cents": v["total"]})
    return ret


def _issue_credit(ret, customer, amount, source, description):
    if customer is None:
        raise APIError("Crédito na loja exige cliente cadastrado")
    CustomerCredit.objects.get_or_create(
        idempotency_key=f"return:{ret.id}:{source}",
        defaults={"tenant": ret.tenant, "customer": customer, "delta_cents": amount, "source": source,
                  "description": description[:240], "sale": ret.sale, "sale_return": ret})
