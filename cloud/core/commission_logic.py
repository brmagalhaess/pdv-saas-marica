"""Sales commission accrual, reversal and payout.

Rate per line: the category rate when set, otherwise the credited member's
rate. The credited member is the seller, or the operator when no seller was
chosen. Callers hold the sale's transaction.
"""
from decimal import Decimal, ROUND_HALF_UP

from django.db import transaction
from django.utils import timezone

from .api_common import APIError, audit
from .models import Commission, FinanceEntry


def _cents(base, bps):
    return int((Decimal(base) * Decimal(bps) / Decimal(10000)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def accrue(sale):
    member = sale.seller or sale.operator
    if member is None or sale.status != "completed":
        return None
    base = amount = 0
    for item in sale.items.select_related("product__category"):
        category = item.product.category
        rate = category.commission_bps if category and category.commission_bps is not None else member.commission_bps
        if rate:
            base += item.line_total_cents
            amount += _cents(item.line_total_cents, rate)
    if amount <= 0:
        return None
    row, _ = Commission.objects.get_or_create(
        sale=sale, reversal=False,
        defaults={"tenant": sale.tenant, "seller": member, "base_cents": base, "amount_cents": amount})
    return row


def reverse(sale, actor=None):
    row = Commission.objects.select_for_update().filter(sale=sale, reversal=False).first()
    if row is None:
        return None
    if row.status == "pending":
        row.status = "cancelled"
        row.save(update_fields=["status"])
        return row
    if row.status == "paid":
        reversal, _ = Commission.objects.get_or_create(
            sale=sale, reversal=True,
            defaults={"tenant": sale.tenant, "seller": row.seller, "base_cents": -row.base_cents,
                      "amount_cents": -row.amount_cents})
        audit(sale.tenant, actor, "commission.reversed_after_payment", reversal)
        return reversal
    return row


def pay(tenant, seller, actor, due_date):
    """Close all pending commissions of a seller into one open payable."""
    with transaction.atomic():
        rows = list(Commission.objects.select_for_update()
                    .filter(tenant=tenant, seller=seller, status="pending").order_by("created_at"))
        total = sum(row.amount_cents for row in rows)
        if not rows or total <= 0:
            raise APIError("Não há comissão positiva pendente para este vendedor", 409)
        entry = FinanceEntry.objects.create(
            tenant=tenant, kind="payable", amount_cents=total, due_date=due_date, created_by=actor,
            description=f"Comissão de {seller.name} ({len(rows)} vendas)"[:240])
        now = timezone.now()
        Commission.objects.filter(pk__in=[row.pk for row in rows]).update(
            status="paid", paid_at=now, finance_entry=entry)
        audit(tenant, actor, "commission.paid", entry, {"seller_id": str(seller.id), "count": len(rows),
                                                         "amount_cents": total})
        return entry, rows
