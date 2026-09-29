from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0007_subscription_license"),
    ]

    operations = [
        migrations.AddField(
            model_name="stockreceipt",
            name="source_file_name",
            field=models.CharField(blank=True, max_length=180),
        ),
        migrations.AddField(
            model_name="stockreceipt",
            name="source_file_mime",
            field=models.CharField(blank=True, max_length=100),
        ),
        migrations.AddField(
            model_name="stockreceipt",
            name="source_file_sha256",
            field=models.CharField(blank=True, max_length=64),
        ),
        migrations.AddField(
            model_name="stockreceipt",
            name="source_file_size",
            field=models.PositiveIntegerField(default=0),
        ),
    ]
