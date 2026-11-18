"""Data of the NFC-e release, in its own migration (own transaction; see 0045_nfce).

1. Older provider states collapse into the five NFC-e states of docs/NFCE.md
   (queued/processing -> pending, denied -> rejected with "Uso denegado" in the message,
   cancel_pending -> authorized; the cancellation still runs from its fiscal.cancel job).
2. Companies with an active Focus NFe account keep emitting through Focus: FiscalConfig
   mode='focusnfe' is created for them (only when they have no FiscalConfig yet).
3. Legacy Focus documents (environment '') get access_key, number, series, issue/authorization
   dates from the stored Focus answer (FiscalPayload.provider_response) and the environment of
   the company's Focus account, so archive, poll, cancel and the accountant package work.

Reverse is a no-op: the old release ignores the new columns, and the status mapping cannot be
undone exactly (docs/NFCE.md, "Rollback").
"""
from django.db import migrations
from django.utils.dateparse import parse_datetime

LEGACY_STATUS = {"queued": "pending", "processing": "pending", "denied": "rejected", "cancel_pending": "authorized"}
ENVIRONMENTS = ("homologation", "production")


def _key(value):
    """Focus answers "NFe" + 44 digits (sometimes only the digits)."""
    text = str(value or "").strip()
    if text[:3].upper() == "NFE":
        text = text[3:]
    return text if text.isdigit() and len(text) == 44 else ""


def _positive(value):
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return number if 0 < number <= 999999999 else None


def _series(value):
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return number if 0 <= number <= 999 else None


def _moment(value):
    try:
        return parse_datetime(str(value)) if value else None
    except ValueError:
        return None


def forwards(apps, schema_editor):
    FiscalDocument = apps.get_model("core", "FiscalDocument")
    FiscalConfig = apps.get_model("core", "FiscalConfig")
    Sale = apps.get_model("core", "Sale")
    IntegrationAccount = apps.get_model("operations", "IntegrationAccount")
    FiscalPayload = apps.get_model("operations", "FiscalPayload")

    for row in FiscalDocument.objects.filter(status="denied").only("pk", "error_message"):
        if not (row.error_message or "").startswith("Uso denegado"):
            row.error_message = ("Uso denegado: " + (row.error_message or ""))[:1000]
            row.save(update_fields=["error_message"])
    for old, new in LEGACY_STATUS.items():
        FiscalDocument.objects.filter(status=old).update(status=new)
        Sale.objects.filter(fiscal_status=old).update(fiscal_status=new)

    accounts = {row.tenant_id: row for row in IntegrationAccount.objects.filter(provider="focusnfe", tenant__isnull=False)}
    for tenant_id, account in accounts.items():
        if account.enabled and not FiscalConfig.objects.filter(tenant_id=tenant_id).exists():
            FiscalConfig.objects.create(tenant_id=tenant_id, mode="focusnfe")

    legacy = list(FiscalDocument.objects.filter(environment="").values_list("pk", flat=True))
    for pk in legacy:
        doc = FiscalDocument.objects.get(pk=pk)
        payload = FiscalPayload.objects.filter(document_id=pk).first()
        response = payload.provider_response if payload and isinstance(payload.provider_response, dict) else {}
        account = accounts.get(doc.tenant_id)
        changed = []
        if doc.provider == "focusnfe" and account and account.environment in ENVIRONMENTS:
            doc.environment = account.environment
            changed.append("environment")
        key = _key(response.get("chave_nfe"))
        if key and not doc.access_key:
            doc.access_key = key
            changed.append("access_key")
        number, series = _positive(response.get("numero")), _series(response.get("serie"))
        if key and number is None:
            # Chave: cUF(2) AAMM(4) CNPJ(14) mod(2) serie(3) nNF(9) ...
            series, number = int(key[22:25]), _positive(key[25:34])
        if number is not None and series is not None and doc.number is None and doc.environment:
            taken = FiscalDocument.objects.filter(tenant_id=doc.tenant_id, environment=doc.environment, model=65,
                                                  series=series, number=number).exclude(pk=pk).exists()
            if not taken:
                doc.number, doc.series = number, series
                changed += ["number", "series"]
        issued = _moment(response.get("data_emissao"))
        if issued and not doc.issued_at:
            doc.issued_at = issued
            changed.append("issued_at")
        authorized = _moment(response.get("data_autorizacao"))
        if authorized and doc.status in ("authorized", "cancelled") and not doc.authorized_at:
            doc.authorized_at = authorized
            changed.append("authorized_at")
        if changed:
            doc.save(update_fields=changed)


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0045_nfce"),
        ("operations", "0004_nfce"),
    ]

    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
