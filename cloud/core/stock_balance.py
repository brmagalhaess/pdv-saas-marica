"""Materialized stock balance maintained from StockMovement inserts.

Movements are append-only (never updated or deleted), so adding each delta on
insert keeps StockBalance equal to the ledger sum. The update runs inside the
caller's transaction: a rolled-back movement never reaches the balance.
"""
from django.db import IntegrityError, transaction
from django.db.models import F, Sum
from django.db.models.signals import post_save
from django.dispatch import receiver

from .models import StockBalance, StockMovement


@receiver(post_save, sender=StockMovement, dispatch_uid="stock_balance_apply")
def _apply(sender, instance, created, raw=False, **kwargs):
    if not created or raw:
        return
    add(instance.tenant_id, instance.shop_id, instance.product_id, instance.delta_milli)


def add(tenant_id, shop_id, product_id, delta):
    rows = StockBalance.objects.filter(shop_id=shop_id, product_id=product_id)
    if rows.update(quantity_milli=F("quantity_milli") + delta):
        return
    try:
        with transaction.atomic():
            StockBalance.objects.create(tenant_id=tenant_id, shop_id=shop_id, product_id=product_id,
                                        quantity_milli=delta)
    except IntegrityError:  # another transaction created the row first
        rows.update(quantity_milli=F("quantity_milli") + delta)


def balance(tenant, shop, product):
    return (StockBalance.objects.filter(tenant=tenant, shop=shop, product=product)
            .values_list("quantity_milli", flat=True).first() or 0)


def balances(tenant, shop, product_ids=None):
    rows = StockBalance.objects.filter(tenant=tenant, shop=shop)
    if product_ids is not None:
        rows = rows.filter(product_id__in=product_ids)
    return dict(rows.values_list("product_id", "quantity_milli"))


def tenant_quantity(tenant, product):
    return StockBalance.objects.filter(tenant=tenant, product=product).aggregate(v=Sum("quantity_milli"))["v"] or 0


def drift(tenant=None):
    """[(shop_id, product_id, ledger, balance)] where the two disagree."""
    ledger = StockMovement.objects.all() if tenant is None else StockMovement.objects.filter(tenant=tenant)
    expected = {(r["shop_id"], r["product_id"]): r["v"]
                for r in ledger.values("shop_id", "product_id").annotate(v=Sum("delta_milli"))}
    stored = StockBalance.objects.all() if tenant is None else StockBalance.objects.filter(tenant=tenant)
    actual = {(r.shop_id, r.product_id): r.quantity_milli for r in stored}
    return [(shop, product, expected.get((shop, product), 0), actual.get((shop, product), 0))
            for shop, product in sorted(set(expected) | set(actual), key=str)
            if expected.get((shop, product), 0) != actual.get((shop, product), 0)]
