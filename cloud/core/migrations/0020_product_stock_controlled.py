from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("core", "0019_tenant_business_hours")]

    operations = [
        migrations.AddField(
            model_name="product",
            name="stock_controlled",
            field=models.BooleanField(default=True),
        ),
    ]
