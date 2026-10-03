from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("core", "0010_tenant_max_discount")]

    operations = [
        migrations.AddField(
            model_name="stockreceiptitem",
            name="average_cost_cents",
            field=models.PositiveBigIntegerField(blank=True, null=True),
        ),
    ]
