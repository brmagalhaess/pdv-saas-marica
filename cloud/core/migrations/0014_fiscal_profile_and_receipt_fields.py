from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("core", "0013_product_scale_code")]

    operations = [
        migrations.AddField(
            model_name="tenant",
            name="trade_name",
            field=models.CharField(blank=True, max_length=160),
        ),
        migrations.AddField(
            model_name="tenant",
            name="state_registration",
            field=models.CharField(blank=True, max_length=30),
        ),
        migrations.AddField(
            model_name="tenant",
            name="municipal_registration",
            field=models.CharField(blank=True, max_length=30),
        ),
        migrations.AddField(
            model_name="tenant",
            name="address_line",
            field=models.CharField(blank=True, max_length=180),
        ),
        migrations.AddField(
            model_name="tenant",
            name="address_number",
            field=models.CharField(blank=True, max_length=20),
        ),
        migrations.AddField(
            model_name="tenant",
            name="address_complement",
            field=models.CharField(blank=True, max_length=80),
        ),
        migrations.AddField(
            model_name="tenant",
            name="address_neighborhood",
            field=models.CharField(blank=True, max_length=100),
        ),
        migrations.AddField(
            model_name="tenant",
            name="address_city",
            field=models.CharField(blank=True, max_length=100),
        ),
        migrations.AddField(
            model_name="tenant",
            name="address_state",
            field=models.CharField(blank=True, max_length=2),
        ),
        migrations.AddField(
            model_name="tenant",
            name="address_zip",
            field=models.CharField(blank=True, max_length=8),
        ),
        migrations.AddField(
            model_name="tenant",
            name="tax_regime",
            field=models.CharField(default="simples_nacional", max_length=30),
        ),
        migrations.AddField(
            model_name="tenant",
            name="fiscal_environment",
            field=models.CharField(default="non_fiscal", max_length=20),
        ),
        migrations.AddField(
            model_name="tenant",
            name="nfce_series",
            field=models.PositiveIntegerField(default=1),
        ),
        migrations.AddField(
            model_name="tenant",
            name="nfce_next_number",
            field=models.PositiveBigIntegerField(default=1),
        ),
        migrations.AddField(
            model_name="product",
            name="ncm",
            field=models.CharField(blank=True, max_length=8),
        ),
        migrations.AddField(
            model_name="sale",
            name="customer_name_snapshot",
            field=models.CharField(blank=True, max_length=180),
        ),
        migrations.AddField(
            model_name="sale",
            name="customer_document_snapshot",
            field=models.CharField(blank=True, max_length=30),
        ),
    ]
