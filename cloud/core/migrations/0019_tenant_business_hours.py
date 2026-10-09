from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("core", "0018_platformmfa")]

    operations = [
        migrations.AddField(
            model_name="tenant",
            name="business_hours",
            field=models.JSONField(blank=True, default=dict),
        ),
    ]
