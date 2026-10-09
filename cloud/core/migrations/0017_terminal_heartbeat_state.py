from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("core", "0016_syncconflict_state")]

    operations = [
        migrations.AddField(
            model_name="terminal",
            name="heartbeat_error",
            field=models.CharField(blank=True, max_length=300),
        ),
        migrations.AddField(
            model_name="terminal",
            name="heartbeat_pending_events",
            field=models.PositiveIntegerField(default=0),
        ),
    ]
