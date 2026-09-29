from django.db import migrations


def seed_plans(apps, schema_editor):
    Plan = apps.get_model("core", "Plan")
    values = [
        ("essential", "Essencial", 14900, 149000, 1, 1,
         ["pdv_local", "cloud_dashboard", "stock"]),
        ("management", "Gestão", 22900, 229000, 1, 1,
         ["pdv_local", "cloud_dashboard", "stock", "finance", "reports"]),
        ("advanced", "Avançado", 34900, 349000, 1, 2,
         ["pdv_local", "cloud_dashboard", "stock", "finance", "reports", "priority_support"]),
    ]
    for slug, name, month, year, terminals, shops, features in values:
        Plan.objects.get_or_create(slug=slug, defaults={
            "name": name, "price_month_cents": month, "price_year_cents": year,
            "terminal_limit": terminals, "shop_limit": shops, "features": features})


class Migration(migrations.Migration):
    dependencies = [("core", "0001_initial")]
    operations = [migrations.RunPython(seed_plans, migrations.RunPython.noop)]
