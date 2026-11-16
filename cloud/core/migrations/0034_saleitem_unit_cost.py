from django.db import migrations, models
from django.db.models import OuterRef, Subquery
from django.db.models.functions import Coalesce


def backfill_unit_cost(apps, schema_editor):
    """Best estimate for history: the moving average left by the last stock
    receipt before the sale; without one, the product's current cost."""
    Product = apps.get_model("core", "Product")
    Sale = apps.get_model("core", "Sale")
    SaleItem = apps.get_model("core", "SaleItem")
    ReceiptItem = apps.get_model("core", "StockReceiptItem")
    sold_at = Sale.objects.filter(pk=OuterRef(OuterRef("sale_id"))).values("created_at")[:1]
    receipt_cost = (ReceiptItem.objects
                    .filter(product_id=OuterRef("product_id"), average_cost_cents__isnull=False,
                            receipt__created_at__lte=Subquery(sold_at))
                    .order_by("-receipt__created_at", "-id").values("average_cost_cents")[:1])
    current_cost = Product.objects.filter(pk=OuterRef("product_id")).values("cost_cents")[:1]
    SaleItem.objects.filter(unit_cost_cents__isnull=True).update(
        unit_cost_cents=Coalesce(Subquery(receipt_cost), Subquery(current_cost)))


class Migration(migrations.Migration):
    dependencies = [("core", "0033_promotions")]

    operations = [
        migrations.AddField(
            model_name="saleitem",
            name="unit_cost_cents",
            field=models.PositiveBigIntegerField(blank=True, null=True),
        ),
        migrations.RunPython(backfill_unit_cost, migrations.RunPython.noop),
    ]
