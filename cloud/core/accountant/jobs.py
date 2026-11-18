"""Tarefas da fila (operations.ProviderJob) do pacote do contador e o agendamento do dia 1º."""
from django.db.models import Q
from django.utils import timezone

from . import package as builder
from .models import AccountantDelivery, AccountantPackage, AccountantSettings

# O agendamento roda no dia 1º; se o worker estiver parado, ainda tenta até o dia 5.
SCHEDULE_LAST_DAY = 5


def previous_month(day):
    return (day.year - 1, 12) if day.month == 1 else (day.year, day.month - 1)


def new_delivery(package, trigger, user=None):
    return AccountantDelivery.objects.create(tenant=package.tenant, package=package, trigger=trigger,
                                             to_email=package.tenant.accountant_email, requested_by=user)


def run_package(job):
    """accountant.package: {package_id, delivery_id?} — gera o ZIP e, se pedido, envia ao contador."""
    package = AccountantPackage.objects.select_related("tenant").get(pk=job.payload["package_id"], tenant=job.tenant)
    builder.generate(package)
    result = {"package_id": str(package.pk), "status": "ready", "size_bytes": package.size_bytes}
    if job.payload.get("delivery_id"):
        delivery = AccountantDelivery.objects.select_related("package__tenant").get(
            pk=job.payload["delivery_id"], tenant=job.tenant)
        builder.send(delivery)
        result["delivery_id"] = str(delivery.pk)
        result["sent_to"] = delivery.to_email
    return result


def run_send(job):
    """accountant.send: {delivery_id} — reenvio do link de um pacote já pronto."""
    delivery = AccountantDelivery.objects.select_related("package__tenant").get(
        pk=job.payload["delivery_id"], tenant=job.tenant)
    builder.send(delivery)
    return {"delivery_id": str(delivery.pk), "sent_to": delivery.to_email}


def run_monthly(job):
    """accountant.monthly: {year, month} — pacote do mês anterior gerado e enviado no dia 1º."""
    tenant = job.tenant
    if not tenant.accountant_email:
        return {"skipped": "sem e-mail do contador"}
    year, month = job.payload["year"], job.payload["month"]
    wanted, _ = builder.sped_policy(tenant)
    package = AccountantPackage.objects.filter(tenant=tenant, year=year, month=month, trigger="schedule").first()
    if not package:
        package = AccountantPackage.objects.create(tenant=tenant, year=year, month=month, trigger="schedule",
                                                   include_sped=wanted)
    if package.status != "ready":
        builder.generate(package)
    delivery = (AccountantDelivery.objects.filter(package=package, trigger="schedule").exclude(status="failed").first()
                or new_delivery(package, "schedule"))
    if delivery.status != "sent":
        builder.send(delivery)
    return {"package_id": str(package.pk), "delivery_id": str(delivery.pk), "sent_to": delivery.to_email}


def schedule_monthly(now=None):
    """Enfileira (uma vez por empresa e mês) o pacote do mês anterior. Idempotente: chave por empresa e mês."""
    from core.models import FiscalDocument, Tenant
    from operations.models import ProviderJob
    from operations.services import enqueue
    today = timezone.localdate(now or timezone.now())
    if today.day > SCHEDULE_LAST_DAY:
        return 0
    year, month = previous_month(today)
    disabled = AccountantSettings.objects.filter(auto_send=False).values("tenant_id")
    tenants = (Tenant.objects.filter(active=True).exclude(accountant_email="").exclude(pk__in=disabled)
               .filter(Q(pk__in=FiscalDocument.objects.values("tenant_id"))))
    created = 0
    for tenant in tenants:
        key = f"accountant.monthly:{tenant.pk}:{year}-{month:02d}"
        if ProviderJob.objects.filter(idempotency_key=key).exists():
            continue
        enqueue("accountant.monthly", tenant, key, {"year": year, "month": month})
        created += 1
    return created


def housekeeping(now=None):
    """Chamado de hora em hora pelo worker: agenda o dia 1º e apaga ZIPs vencidos."""
    builder.purge_expired(now)
    return schedule_monthly(now)
