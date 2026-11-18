"""Pacote mensal do contador: configuração, pacotes gerados e envios por e-mail.

Importado no fim de core/models.py (app_label "core"). Ver docs/PACOTE-CONTADOR.md.
"""
import uuid

from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models


class AccountantSettings(models.Model):
    """Dados do contador (registro 0100 do SPED) e opções do pacote. O e-mail fica em Tenant.accountant_email."""
    PROFILES = [(p, p) for p in ("A", "B", "C")]
    tenant = models.OneToOneField("core.Tenant", on_delete=models.CASCADE, primary_key=True,
                                  related_name="accountant_settings")
    name = models.CharField(max_length=100, blank=True)
    cpf = models.CharField(max_length=11, blank=True)
    crc = models.CharField(max_length=15, blank=True)
    cnpj = models.CharField(max_length=14, blank=True)
    cep = models.CharField(max_length=8, blank=True)
    address = models.CharField(max_length=60, blank=True)
    number = models.CharField(max_length=10, blank=True)
    complement = models.CharField(max_length=60, blank=True)
    neighborhood = models.CharField(max_length=60, blank=True)
    phone = models.CharField(max_length=11, blank=True)
    city_ibge = models.CharField(max_length=7, blank=True)
    # Envio automático no dia 1º do pacote do mês anterior.
    auto_send = models.BooleanField(default=True)
    # SPED: perfil de apresentação (0000/IND_PERFIL), definido pela UF/contador.
    sped_profile = models.CharField(max_length=1, choices=PROFILES, default="A")
    # Gerar SPED para Simples Nacional mesmo assim (exigência específica da UF).
    sped_for_simples = models.BooleanField(default=False)
    # E116 (obrigação a recolher): código de receita da UF e dia de vencimento no mês seguinte.
    e116_cod_or = models.CharField(max_length=3, blank=True, default="000")
    e116_cod_rec = models.CharField(max_length=30, blank=True)
    e116_due_day = models.PositiveSmallIntegerField(null=True, blank=True,
                                                    validators=[MinValueValidator(1), MaxValueValidator(31)])
    updated_at = models.DateTimeField(auto_now=True)


class AccountantPackage(models.Model):
    STATUS = [(s, s) for s in ("queued", "running", "ready", "failed", "expired")]
    TRIGGERS = [(s, s) for s in ("manual", "schedule")]
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey("core.Tenant", on_delete=models.PROTECT, related_name="accountant_packages")
    year = models.PositiveSmallIntegerField()
    month = models.PositiveSmallIntegerField(validators=[MinValueValidator(1), MaxValueValidator(12)])
    status = models.CharField(max_length=10, choices=STATUS, default="queued")
    trigger = models.CharField(max_length=10, choices=TRIGGERS, default="manual")
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                     related_name="+")
    include_sped = models.BooleanField(default=False)
    sped_forced = models.BooleanField(default=False)
    filename = models.CharField(max_length=120, blank=True)
    content = models.BinaryField(null=True, blank=True)
    content_sha256 = models.CharField(max_length=64, blank=True)
    size_bytes = models.PositiveBigIntegerField(default=0)
    sped_content = models.BinaryField(null=True, blank=True)
    summary = models.JSONField(default=dict, blank=True)
    error = models.CharField(max_length=500, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    ready_at = models.DateTimeField(null=True, blank=True)
    # Depois disto o ZIP é apagado (os XMLs continuam no banco: basta gerar de novo).
    expires_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [models.Index(fields=["tenant", "-created_at"], name="acct_pkg_tenant_created")]


class AccountantDelivery(models.Model):
    """Um envio por e-mail ao contador, com link próprio (token guardado só como hash)."""
    STATUS = [(s, s) for s in ("queued", "sent", "failed")]
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey("core.Tenant", on_delete=models.PROTECT, related_name="accountant_deliveries")
    package = models.ForeignKey(AccountantPackage, on_delete=models.CASCADE, related_name="deliveries")
    to_email = models.EmailField(max_length=254)
    status = models.CharField(max_length=10, choices=STATUS, default="queued")
    trigger = models.CharField(max_length=10, default="manual")
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                     related_name="+")
    token_hash = models.CharField(max_length=64, blank=True, db_index=True)
    link_expires_at = models.DateTimeField(null=True, blank=True)
    error = models.CharField(max_length=500, blank=True)
    downloads = models.PositiveIntegerField(default=0)
    last_download_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    sent_at = models.DateTimeField(null=True, blank=True)
