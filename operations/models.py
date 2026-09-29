import uuid
from django.db import models
from django.db.models import Q

class IntegrationAccount(models.Model):
    id=models.UUIDField(primary_key=True,default=uuid.uuid4,editable=False)
    tenant=models.ForeignKey('core.Tenant',on_delete=models.PROTECT,null=True,blank=True)
    provider=models.CharField(max_length=40)
    environment=models.CharField(max_length=30)
    token_encrypted=models.TextField()
    enabled=models.BooleanField(default=False)
    updated_at=models.DateTimeField(auto_now=True)
    class Meta:
        constraints=[models.UniqueConstraint(fields=['tenant','provider'],condition=Q(tenant__isnull=False),name='ops_tenant_provider_unique'),
                     models.UniqueConstraint(fields=['provider'],condition=Q(tenant__isnull=True),name='ops_global_provider_unique')]

class ProviderJob(models.Model):
    id=models.UUIDField(primary_key=True,default=uuid.uuid4,editable=False)
    tenant=models.ForeignKey('core.Tenant',on_delete=models.PROTECT,null=True,blank=True)
    kind=models.CharField(max_length=60)
    idempotency_key=models.CharField(max_length=240,unique=True)
    request_hash=models.CharField(max_length=64)
    payload=models.JSONField(default=dict)
    result=models.JSONField(default=dict)
    status=models.CharField(max_length=24,default='queued')
    attempts=models.PositiveIntegerField(default=0)
    available_at=models.DateTimeField()
    locked_at=models.DateTimeField(null=True,blank=True)
    last_error=models.CharField(max_length=500,blank=True)
    created_at=models.DateTimeField(auto_now_add=True)
    updated_at=models.DateTimeField(auto_now=True)
    class Meta:
        indexes=[models.Index(fields=['status','available_at'])]

class BillingBinding(models.Model):
    id=models.UUIDField(primary_key=True,default=uuid.uuid4,editable=False)
    tenant=models.ForeignKey('core.Tenant',on_delete=models.PROTECT)
    plan=models.ForeignKey('core.Plan',on_delete=models.PROTECT)
    external_id=models.CharField(max_length=160,unique=True)
    customer_id=models.CharField(max_length=160)
    cycle=models.CharField(max_length=20)
    amount_cents=models.PositiveBigIntegerField()
    active=models.BooleanField(default=True)
    created_at=models.DateTimeField(auto_now_add=True)

class FiscalPayload(models.Model):
    document=models.OneToOneField('core.FiscalDocument',on_delete=models.PROTECT,primary_key=True)
    request_payload=models.JSONField(default=dict)
    provider_response=models.JSONField(default=dict)
    xml_content=models.BinaryField(null=True,blank=True)
    xml_sha256=models.CharField(max_length=64,blank=True)
    updated_at=models.DateTimeField(auto_now=True)

class ProductRelease(models.Model):
    id=models.UUIDField(primary_key=True,default=uuid.uuid4,editable=False)
    component=models.CharField(max_length=20)
    channel=models.CharField(max_length=20,default='pilot')
    version=models.CharField(max_length=50)
    url=models.URLField(max_length=2000)
    sha256=models.CharField(max_length=64)
    size_bytes=models.PositiveBigIntegerField()
    notes=models.TextField(blank=True)
    active=models.BooleanField(default=True)
    published_at=models.DateTimeField(auto_now_add=True)
    class Meta:
        constraints=[models.UniqueConstraint(fields=['component','channel','version'],name='release_version_unique')]
