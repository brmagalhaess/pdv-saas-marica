from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("core", "0009_product_image")]

    operations = [
        migrations.AddField(
            model_name="tenant",
            name="max_discount_bps",
            field=models.PositiveIntegerField(default=10000),
        ),
    ]
