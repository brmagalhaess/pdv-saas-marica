"""Seed StockBalance from the movement ledger."""
from django.db import migrations
from django.db.models import Sum


def backfill(apps, schema_editor):
    StockMovement = apps.get_model("core", "StockMovement")
    StockBalance = apps.get_model("core", "StockBalance")
    rows = (StockMovement.objects.values("tenant_id", "shop_id", "product_id")
            .annotate(quantity=Sum("delta_milli")).order_by())
    StockBalance.objects.bulk_create([
        StockBalance(tenant_id=r["tenant_id"], shop_id=r["shop_id"], product_id=r["product_id"],
                     quantity_milli=r["quantity"]) for r in rows], batch_size=1000)


class Migration(migrations.Migration):
    dependencies = [("core", "0027_stock_balance")]
    operations = [migrations.RunPython(backfill, migrations.RunPython.noop)]
