from django.db import migrations


def expand_seeded_advanced(apps, schema_editor):
    Plan = apps.get_model("core", "Plan")
    # Preserve plans already customized by a platform administrator.
    Plan.objects.filter(slug="advanced", terminal_limit=1, shop_limit=2,
                        price_month_cents=34900).update(terminal_limit=3)


class Migration(migrations.Migration):
    dependencies = [("core", "0005_ratelimit")]
    operations = [migrations.RunPython(expand_seeded_advanced, migrations.RunPython.noop)]
