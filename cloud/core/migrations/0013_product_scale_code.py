from django.db import migrations, models
from django.db.models import Q


class Migration(migrations.Migration):
    dependencies = [("core", "0012_backfill_average_costs")]

    operations = [
        migrations.AddField(
            model_name="product",
            name="scale_code",
            field=models.CharField(blank=True, max_length=6),
        ),
        migrations.AddConstraint(
            model_name="product",
            constraint=models.UniqueConstraint(
                condition=~Q(scale_code=""), fields=("tenant", "scale_code"),
                name="product_tenant_scale_code_unique",
            ),
        ),
    ]
