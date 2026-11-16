from django.db import migrations

# Additional registers ("caixas adicionais") per shop suggested for the seeded plans.
# Only fills plans still at zero; editable in Plataforma → Planos and in the admin.
SUGGESTED = {"essential": 1, "management": 2, "advanced": 4}


def suggest_max_stations(apps, schema_editor):
    Plan = apps.get_model("core", "Plan")
    for slug, amount in SUGGESTED.items():
        Plan.objects.filter(slug=slug, max_stations=0).update(max_stations=amount)


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0039_fase3_estacoes'),
    ]

    operations = [
        migrations.RunPython(suggest_max_stations, migrations.RunPython.noop),
    ]
