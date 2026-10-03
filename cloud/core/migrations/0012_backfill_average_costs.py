from django.db import migrations


def backfill_average_costs(apps, schema_editor):
    Product = apps.get_model("core", "Product")
    ReceiptItem = apps.get_model("core", "StockReceiptItem")
    for product in Product.objects.all().iterator():
        quantity = 0
        average = None
        items = (ReceiptItem.objects.filter(product_id=product.pk)
                 .select_related("receipt")
                 .order_by("receipt__created_at", "receipt_id", "id"))
        for item in items:
            total = quantity + item.quantity_milli
            if total <= 0:
                continue
            if quantity > 0 and average is not None:
                average = ((quantity * average) + (item.quantity_milli * item.unit_cost_cents)
                           + total // 2) // total
            else:
                average = item.unit_cost_cents
            if item.average_cost_cents != average:
                item.average_cost_cents = average
                item.save(update_fields=["average_cost_cents"])
            quantity = total
        if average is not None and product.cost_cents != average:
            Product.objects.filter(pk=product.pk).update(cost_cents=average)


class Migration(migrations.Migration):
    dependencies = [("core", "0011_stockreceiptitem_average_cost")]

    operations = [migrations.RunPython(backfill_average_costs, migrations.RunPython.noop)]
