"""Give every existing subscription its own permanent public license identifier."""
import secrets

import core.models
from django.db import migrations, models


def backfill_licenses(apps, schema_editor):
    Subscription = apps.get_model("core", "Subscription")
    alias = schema_editor.connection.alias
    for row in Subscription.objects.using(alias).all().iterator():
        token = secrets.token_hex(12).upper()
        row.license_key = "PDV2-" + "-".join(
            token[index:index + 6] for index in range(0, 24, 6))
        row.license_source = ("trial" if row.status == "trial" else
                              "subscription" if row.external_id else "manual")
        row.save(using=alias, update_fields=["license_key", "license_source"])


class Migration(migrations.Migration):
    dependencies = [("core", "0006_advanced_terminal_limit")]

    operations = [
        migrations.AddField(
            model_name="subscription",
            name="license_key",
            field=models.CharField(max_length=40, null=True, editable=False),
        ),
        migrations.AddField(
            model_name="subscription",
            name="license_source",
            field=models.CharField(choices=[("trial", "trial"),
                                             ("subscription", "subscription"),
                                             ("vip", "vip"),
                                             ("manual", "manual")], default="trial", max_length=20),
        ),
        migrations.RunPython(backfill_licenses, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="subscription",
            name="license_key",
            field=models.CharField(default=core.models.new_license_key,
                                   editable=False, max_length=40, unique=True),
        ),
    ]
