"""Credit-account (fiado) sales, store credit and partial returns.

All functions run inside the sync/reversal transaction and are idempotent:
terminals resend events after a lost acknowledgement.
"""
from collections import defaultdict
from datetime import timedelta
from decimal import Decimal, ROUND_HALF_UP

from django.db.models import Sum
from django.utils import timezone

from .api_common import APIError, audit, digest, fields, money, quantity, scoped, text, uuid_value
from .models import (Commission, CreditReceipt, Customer, CustomerCredit, FinanceEntry, Membership, Sale, SaleReturn,
                     SaleReturnItem, StockMovement)

CREDIT_METHODS = ("credit_account", "store_credit")
TERMINAL_REFUNDS = ("cash", "store_credit", "account")
PANEL_REFUNDS = ("store_credit", "account", "external")


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
    for number, (method, amount, _configured) in enumerate(payments):
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


def validate_return(data, tenant, aggregate_id, origin="terminal", sale=None):
    """Validate a return. Terminal returns already happened at the counter (money
    left the drawer), so an over-return or a return of a sale reversed in the
    panel is accepted and reported in `exceeded` / `reversed` for the admin;
    panel returns are refused instead. `sale` is the row already locked by the caller."""
    from .sale_logic import event_time
    required = ("sale_id", "items", "total_cents", "refund_method", "reason", "operator_id", "created_at")
    allowed = required + ("customer_id", "supervisor_id", "cash_session_id")
    if not isinstance(data, dict) or set(data) - set(allowed) or any(k not in data for k in required):
        raise APIError("Campos inválidos ou ausentes")
    uuid_value(aggregate_id, "Devolução")
    if sale is None:
        sale = Sale.objects.filter(tenant=tenant, pk=uuid_value(data["sale_id"], "Venda")).first()
    if sale is None:
        raise APIError("Venda da devolução não encontrada", 404)
    if sale.status != "completed" and origin != "terminal":
        raise APIError("Venda cancelada não aceita devolução", 409)
    refund = data["refund_method"]
    if refund not in (TERMINAL_REFUNDS if origin == "terminal" else PANEL_REFUNDS):
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
    normalized, total, exceeded = [], 0, []
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
            if origin != "terminal":
                raise APIError(f"Quantidade devolvida maior que a disponível: {line.name}", 409)
            exceeded.append({"product_id": str(product_id), "product_name": line.name, "requested_milli": qty,
                             "central_balance_milli": max(0, line.quantity_milli - already[product_id])})
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
            "created_at": event_time(data["created_at"]), "exceeded": exceeded,
            "reversed": sale.status != "completed"}


def _persist_return(tenant, shop, terminal, return_id, v, content_hash, created_by=None):
    sale = Sale.objects.select_for_update().get(pk=v["sale"].pk)
    # A sale reversed in the panel already got its stock, fiado and commission
    # back: only the refund handed over at the counter is recorded.
    reversed_sale = v.get("reversed", False)
    ret = SaleReturn.objects.create(id=return_id, tenant=tenant, shop=shop, terminal=terminal, created_by=created_by,
                                    sale=sale, customer=v["customer"], operator=v["operator"],
                                    supervisor=v["supervisor"], cash_session_id=v["cash_session_id"],
                                    refund_method=v["refund"], total_cents=v["total"], reason=v["reason"],
                                    content_hash=content_hash, created_at=v["created_at"])
    for product, qty, amount in v["items"]:
        SaleReturnItem.objects.create(sale_return=ret, product=product, quantity_milli=qty, amount_cents=amount)
        if product.stock_controlled and not reversed_sale:
            StockMovement.objects.create(tenant=tenant, shop=shop, product=product, delta_milli=qty,
                                         source="return", reason=f"Devolução: {v['reason']}"[:240],
                                         reference=str(ret.id), terminal=terminal, sale=sale, actor=created_by,
                                         idempotency_key=f"return:{ret.id}:{product.id}")
    if v["refund"] == "store_credit":
        _issue_credit(ret, v["customer"], v["total"], "return", f"Devolução da venda {str(sale.id)[:8]}")
    elif v["refund"] == "account" and not reversed_sale:
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
    commission = (None if reversed_sale else
                  Commission.objects.filter(sale=sale, reversal=False).exclude(status="cancelled").first())
    if commission and sale.total_cents:
        share = int((Decimal(commission.amount_cents) * v["total"] / sale.total_cents).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
        if share:
            Commission.objects.get_or_create(sale_return=ret, defaults={
                "tenant": tenant, "sale": sale, "seller": commission.seller, "reversal": True,
                "base_cents": -min(v["total"], commission.base_cents), "amount_cents": -share})
    audit(tenant, created_by, "sale.returned", ret, {"sale_id": str(sale.id), "refund": v["refund"],
                                                     "total_cents": v["total"], "origin": "terminal" if terminal else "panel"})
    return ret


def apply_return(terminal, event, conflicts=None):
    """Terminal return from sync. Returns (sale_return, first conflict or None);
    every conflict opened is appended to `conflicts`."""
    from .sale_logic import open_conflict
    tenant = terminal.tenant
    data = event["data"]
    content_hash = digest(data)
    previous = SaleReturn.objects.filter(pk=uuid_value(event["aggregate_id"], "Devolução")).first()
    if previous:
        if previous.tenant_id != tenant.id or previous.content_hash != content_hash:
            raise APIError("Devolução já recebida com conteúdo diferente", 409)
        return previous, None
    # Lock the sale before validating so a panel return or reversal cannot slip in between.
    sale = Sale.objects.select_for_update().filter(tenant=tenant, pk=uuid_value(data.get("sale_id"), "Venda")).first()
    v = validate_return(data, tenant, event["aggregate_id"], origin="terminal", sale=sale)
    ret = _persist_return(tenant, terminal.shop, terminal, uuid_value(event["aggregate_id"]), v, content_hash)
    found = []
    if v["exceeded"]:
        found.append(open_conflict(
            terminal, event, "return_exceeds_sold",
            "Devolução no caixa acima do disponível (provável devolução já registrada no painel). Confira o estoque e o reembolso.",
            items=v["exceeded"], sale_id=str(v["sale"].id)))
    if v["reversed"]:
        found.append(open_conflict(
            terminal, event, "return_on_reversed_sale",
            "Devolução no caixa de uma venda já cancelada ou estornada no painel. "
            "O reembolso já saiu do caixa: confira se o cliente recebeu duas vezes.",
            sale_id=str(v["sale"].id), sale_status=v["sale"].status, refund_method=v["refund"], total_cents=v["total"],
            items=[{"product_id": str(product.id), "product_name": product.name, "quantity_milli": qty,
                    "amount_cents": amount} for product, qty, amount in v["items"]]))
    if conflicts is not None:
        conflicts.extend(found)
    return ret, found[0] if found else None


def panel_return(member, sale, data):
    """Return registered in the web panel for a synced sale."""
    import uuid as uuidlib
    fields(data, ("items", "refund_method", "reason", "customer_id"), ("items", "refund_method", "reason"))
    items = data["items"]
    if not isinstance(items, list) or not items:
        raise APIError("Informe os itens devolvidos")
    lines = {item.product_id: item for item in sale.items.all()}
    prepared = []
    for item in items:
        if not isinstance(item, dict) or set(item) != {"product_id", "quantity_milli"}:
            raise APIError("Item da devolução inválido")
        line = lines.get(uuid_value(item["product_id"], "Produto"))
        if line is None:
            raise APIError("Produto não pertence à venda")
        qty = quantity(item["quantity_milli"])
        prepared.append({"product_id": str(line.product_id), "quantity_milli": qty,
                         "amount_cents": return_line_amount(line.line_total_cents, line.quantity_milli, qty)})
    return_id = uuidlib.uuid4()
    payload = {"sale_id": str(sale.id), "items": prepared, "total_cents": sum(i["amount_cents"] for i in prepared),
               "refund_method": data["refund_method"], "reason": data["reason"], "operator_id": str(member.id),
               "created_at": int(timezone.now().timestamp() * 1000)}
    if data.get("customer_id"):
        payload["customer_id"] = data["customer_id"]
    v = validate_return(payload, member.tenant, str(return_id), origin="panel")
    return _persist_return(member.tenant, sale.shop, None, return_id, v, digest(payload), created_by=member.user)


def apply_credit_receipt(terminal, event, conflicts=None):
    """Fiado paid at the terminal: settle oldest open receivables, splitting a
    partially paid one; anything above the open balance becomes store credit.
    The money is already in the drawer, so a customer deactivated meanwhile
    is still credited and reported in `conflicts`."""
    from .sale_logic import event_time, open_conflict
    data = event["data"]
    previous = CreditReceipt.objects.filter(pk=uuid_value(event["aggregate_id"])).first()
    if previous:
        if previous.tenant_id != terminal.tenant_id or previous.amount_cents != data["amount_cents"]:
            raise APIError("Recebimento já registrado com outros dados", 409)
        return previous
    tenant = terminal.tenant
    customer = Customer.objects.filter(tenant=tenant, pk=uuid_value(data["customer_id"], "Cliente")).first()
    if customer is None:
        raise APIError("Cliente do recebimento de fiado não encontrado", 404)
    when = event_time(data["created_at"])
    receipt = CreditReceipt.objects.create(
        id=uuid_value(event["aggregate_id"]), tenant=tenant, shop=terminal.shop, terminal=terminal, customer=customer,
        operator=Membership.objects.filter(tenant=tenant, pk=uuid_value(data["operator_id"], "Operador")).first(),
        method=data["method"], amount_cents=data["amount_cents"], cash_session_id=data.get("cash_session_id") or "",
        created_at=when)
    remaining = receipt.amount_cents
    for entry in (FinanceEntry.objects.select_for_update()
                  .filter(tenant=tenant, customer=customer, kind="receivable", status="open", sale__isnull=False)
                  .order_by("due_date", "created_at")):
        if remaining <= 0:
            break
        if remaining >= entry.amount_cents:
            remaining -= entry.amount_cents
            entry.status, entry.settled_at = "settled", when
            entry.save(update_fields=["status", "settled_at"])
        else:
            FinanceEntry.objects.create(tenant=tenant, shop=entry.shop, customer=customer, sale=entry.sale,
                                        kind="receivable", amount_cents=remaining, due_date=entry.due_date,
                                        status="settled", settled_at=when,
                                        description=(entry.description + " · pago no caixa")[:240])
            entry.amount_cents -= remaining
            entry.save(update_fields=["amount_cents"])
            remaining = 0
    if remaining > 0:
        CustomerCredit.objects.get_or_create(
            idempotency_key=f"credit-receipt:{receipt.id}",
            defaults={"tenant": tenant, "customer": customer, "delta_cents": remaining, "source": "account_overpaid",
                      "description": "Pagamento de fiado acima do saldo em aberto"})
    audit(tenant, None, "customer.credit_received_terminal", receipt,
          {"amount_cents": receipt.amount_cents, "method": receipt.method, "to_store_credit_cents": remaining})
    if not customer.active:
        conflict = open_conflict(terminal, event, "customer_inactive",
                                 "Fiado recebido no caixa de um cliente desativado no cadastro.",
                                 customer_id=str(customer.id), customer_name=customer.name,
                                 amount_cents=receipt.amount_cents, method=receipt.method,
                                 to_store_credit_cents=remaining)
        if conflicts is not None:
            conflicts.append(conflict)
    return receipt


def _issue_credit(ret, customer, amount, source, description):
    if customer is None:
        raise APIError("Crédito na loja exige cliente cadastrado")
    CustomerCredit.objects.get_or_create(
        idempotency_key=f"return:{ret.id}:{source}",
        defaults={"tenant": ret.tenant, "customer": customer, "delta_cents": amount, "source": source,
                  "description": description[:240], "sale": ret.sale, "sale_return": ret})
