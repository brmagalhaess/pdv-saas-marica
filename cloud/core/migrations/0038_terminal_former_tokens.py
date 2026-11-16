import core.models
import django.db.models.deletion
from django.db import migrations, models


def copy_previous_tokens(apps, schema_editor):
    """The single send-only slot of each terminal becomes the first row of its list."""
    Terminal = apps.get_model("core", "Terminal")
    FormerToken = apps.get_model("core", "TerminalFormerToken")
    rows = Terminal.objects.exclude(previous_token_hash__isnull=True).exclude(previous_token_hash="")
    for row in rows.iterator():
        when = row.transferred_at or row.previous_token_expires_at
        if when is None:
            continue
        FormerToken.objects.get_or_create(
            token_hash=row.previous_token_hash,
            defaults={"terminal_id": row.pk, "device_id": row.previous_device_id,
                      "expires_at": row.previous_token_expires_at or when, "transferred_at": when})


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0037_fase2_recuperacao'),
    ]

    operations = [
        migrations.CreateModel(
            name='TerminalFormerToken',
            fields=[
                ('id', models.UUIDField(default=core.models.uid, editable=False, primary_key=True, serialize=False)),
                ('token_hash', models.CharField(max_length=64, unique=True)),
                ('device_id', models.CharField(blank=True, default='', max_length=160)),
                ('expires_at', models.DateTimeField()),
                ('transferred_at', models.DateTimeField()),
                ('terminal', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE,
                                               related_name='former_tokens', to='core.terminal')),
            ],
            options={
                'indexes': [models.Index(fields=['terminal', '-transferred_at'], name='formertoken_term_when')],
            },
        ),
        migrations.RunPython(copy_previous_tokens, migrations.RunPython.noop),
    ]
