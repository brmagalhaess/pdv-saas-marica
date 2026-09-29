"""Tenant-scoped business records. Money is integer cents; quantity is milliunits."""
import secrets
import uuid
from django.conf import settings
from django.db import models
from django.db.models import Q


def uid():
    return uuid.uuid4()


def new_license_key():
    """Public subscription identifier; device pairing also requires a one-time code."""
    token = secrets.token_hex(12).upper()
    return "PDV2-" + "-".join(token[index:index + 6] for index in range(0, 24, 6))


class Plan(models.Model):
    id = models.UUIDField(primary_key=True, default=uid, editable=False)
    slug = models.SlugField(max_length=60, unique=True)
    name = models.CharField(max_length=100)
    price_month_cents = models.PositiveBigIntegerField(default=0)
    price_year_cents = models.PositiveBigIntegerField(default=0)
    terminal_limit = models.PositiveIntegerField(default=1)
    shop_limit = models.PositiveIntegerField(default=1)
    features = models.JSONField(default=list)
    active = models.BooleanField(default=True)

    def __str__(self):
        return self.name


class Tenant(models.Model):
    id = models.UUIDField(primary_key=True, default=uid, editable=False)
    name = models.CharField(max_length=160)
    document = models.CharField(max_length=30, blank=True)
    roles_version = models.PositiveIntegerField(default=1)
    # Percentage in basis points: 10000 = 100%. This is the hard company
    # ceiling for discounts, including manager approvals.
    max_discount_bps = models.PositiveIntegerField(default=10000)
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.name


class Subscription(models.Model):
    STATUS = [(s, s) for s in ("trial", "active", "past_due", "suspended", "cancelled")]
    LICENSE_SOURCES = [(s, s) for s in ("trial", "subscription", "vip", "manual")]
    id = models.UUIDField(primary_key=True, default=uid, editable=False)
    tenant = models.OneToOneField(Tenant, on_delete=models.PROTECT, related_name="subscription")
    plan = models.ForeignKey(Plan, on_delete=models.PROTECT)
    status = models.CharField(max_length=20, choices=STATUS, default="trial")
    license_key = models.CharField(max_length=40, unique=True, default=new_license_key, editable=False)
    license_source = models.CharField(max_length=20, choices=LICENSE_SOURCES, default="trial")
    trial_ends_at = models.DateTimeField(null=True, blank=True)
    current_period_end = models.DateTimeField(null=True, blank=True)
    external_id = models.CharField(max_length=160, blank=True)
    updated_at = models.DateTimeField(auto_now=True)


class Shop(models.Model):
    id = models.UUIDField(primary_key=True, default=uid, editable=False)
    tenant = models.ForeignKey(Tenant, on_delete=models.PROTECT, related_name="shops")
    name = models.CharField(max_length=160)
    address = models.JSONField(default=dict, blank=True)
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)


class Membership(models.Model):
    ROLES = [(s, s) for s in ("owner", "admin", "manager", "cashier", "inventory", "finance", "viewer")]
    id = models.UUIDField(primary_key=True, default=uid, editable=False)
    tenant = models.ForeignKey(Tenant, on_delete=models.PROTECT, related_name="memberships")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="pdv_memberships")
    name = models.CharField(max_length=160)
    role = models.CharField(max_length=20, choices=ROLES)
    pin_hash = models.CharField(max_length=200, blank=True)
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["tenant", "user"], name="member_user_tenant_unique")]


class Category(models.Model):
    id = models.UUIDField(primary_key=True, default=uid, editable=False)
    tenant = models.ForeignKey(Tenant, on_delete=models.PROTECT)
    name = models.CharField(max_length=120)
    active = models.BooleanField(default=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["tenant", "name"], name="category_tenant_name_unique")]


class Product(models.Model):
    UNITS = [(s, s) for s in ("UN", "KG", "L")]
    id = models.UUIDField(primary_key=True, default=uid, editable=False)
    tenant = models.ForeignKey(Tenant, on_delete=models.PROTECT, related_name="products")
    category = models.ForeignKey(Category, on_delete=models.PROTECT, null=True, blank=True)
    sku = models.CharField(max_length=80)
    barcode = models.CharField(max_length=40, blank=True)
    name = models.CharField(max_length=180)
    variant = models.CharField(max_length=120, blank=True)
    unit = models.CharField(max_length=2, choices=UNITS, default="UN")
    price_cents = models.PositiveBigIntegerField(default=0)
    cost_cents = models.PositiveBigIntegerField(default=0)
    stock_minimum_milli = models.PositiveBigIntegerField(default=0)
    image_url = models.CharField(max_length=500, blank=True)
    tax_fields = models.JSONField(default=dict, blank=True)
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["tenant", "sku"], name="product_tenant_sku_unique"),
            models.UniqueConstraint(fields=["tenant", "barcode"], condition=~Q(barcode=""), name="product_tenant_barcode_unique"),
        ]
        indexes = [models.Index(fields=["tenant", "name"])]


class Customer(models.Model):
    id = models.UUIDField(primary_key=True, default=uid, editable=False)
    tenant = models.ForeignKey(Tenant, on_delete=models.PROTECT)
    name = models.CharField(max_length=180)
    document = models.CharField(max_length=30, blank=True)
    email = models.EmailField(blank=True)
    phone = models.CharField(max_length=30, blank=True)
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)


class Supplier(models.Model):
    id = models.UUIDField(primary_key=True, default=uid, editable=False)
    tenant = models.ForeignKey(Tenant, on_delete=models.PROTECT)
    name = models.CharField(max_length=180)
    document = models.CharField(max_length=30, blank=True)
    email = models.EmailField(blank=True)
    phone = models.CharField(max_length=30, blank=True)
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)


class Terminal(models.Model):
    id = models.UUIDField(primary_key=True, default=uid, editable=False)
    tenant = models.ForeignKey(Tenant, on_delete=models.PROTECT, related_name="terminals")
    shop = models.ForeignKey(Shop, on_delete=models.PROTECT)
    name = models.CharField(max_length=120)
    device_id = models.CharField(max_length=160, blank=True)
    version = models.CharField(max_length=80, blank=True)
    token_hash = models.CharField(max_length=64, unique=True, null=True, blank=True)
    pairing_code_hash = models.CharField(max_length=64, unique=True, null=True, blank=True)
    pairing_expires_at = models.DateTimeField(null=True, blank=True)
    revoked = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    last_seen_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["tenant", "device_id"], condition=~Q(device_id=""), name="terminal_tenant_device_unique")]


class Sale(models.Model):
    STATUS = [(s, s) for s in ("completed", "cancelled", "refunded")]
    id = models.UUIDField(primary_key=True, editable=False)
    tenant = models.ForeignKey(Tenant, on_delete=models.PROTECT, related_name="sales")
    shop = models.ForeignKey(Shop, on_delete=models.PROTECT)
    terminal = models.ForeignKey(Terminal, on_delete=models.PROTECT, null=True, blank=True)
    customer = models.ForeignKey(Customer, on_delete=models.PROTECT, null=True, blank=True)
    request_id = models.CharField(max_length=160)
    content_hash = models.CharField(max_length=64)
    gross_cents = models.PositiveBigIntegerField()
    discount_cents = models.PositiveBigIntegerField(default=0)
    total_cents = models.PositiveBigIntegerField()
    change_cents = models.PositiveBigIntegerField(default=0)
    status = models.CharField(max_length=20, choices=STATUS, default="completed")
    stock_status = models.CharField(max_length=20, default="ok")
    fiscal_status = models.CharField(max_length=30, default="not_issued")
    created_at = models.DateTimeField()
    received_at = models.DateTimeField(auto_now_add=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)
    cancellation_reason = models.CharField(max_length=240, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["tenant", "terminal", "request_id"], name="sale_request_unique")]
        indexes = [models.Index(fields=["tenant", "-received_at", "-id"])]


class SaleItem(models.Model):
    id = models.UUIDField(primary_key=True, default=uid, editable=False)
    sale = models.ForeignKey(Sale, on_delete=models.PROTECT, related_name="items")
    line_number = models.PositiveIntegerField()
    product = models.ForeignKey(Product, on_delete=models.PROTECT)
    name = models.CharField(max_length=180)
    unit = models.CharField(max_length=2)
    quantity_milli = models.PositiveBigIntegerField()
    unit_price_cents = models.PositiveBigIntegerField()
    gross_cents = models.PositiveBigIntegerField()
    discount_cents = models.PositiveBigIntegerField()
    line_total_cents = models.PositiveBigIntegerField()

    class Meta:
        constraints = [models.UniqueConstraint(fields=["sale", "line_number"], name="sale_item_line_unique")]


class SalePayment(models.Model):
    id = models.UUIDField(primary_key=True, default=uid, editable=False)
    sale = models.ForeignKey(Sale, on_delete=models.PROTECT, related_name="payments")
    line_number = models.PositiveIntegerField()
    method = models.CharField(max_length=20)
    amount_cents = models.PositiveBigIntegerField()

    class Meta:
        constraints = [models.UniqueConstraint(fields=["sale", "line_number"], name="sale_payment_line_unique")]


class StockMovement(models.Model):
    SOURCES = [(s, s) for s in ("manual", "receipt", "sale", "void", "terminal_adjustment")]
    id = models.UUIDField(primary_key=True, default=uid, editable=False)
    tenant = models.ForeignKey(Tenant, on_delete=models.PROTECT)
    shop = models.ForeignKey(Shop, on_delete=models.PROTECT)
    product = models.ForeignKey(Product, on_delete=models.PROTECT)
    delta_milli = models.BigIntegerField()
    source = models.CharField(max_length=30, choices=SOURCES)
    reason = models.CharField(max_length=240)
    reference = models.CharField(max_length=160, blank=True)
    idempotency_key = models.CharField(max_length=200, null=True, blank=True)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True)
    terminal = models.ForeignKey(Terminal, on_delete=models.PROTECT, null=True, blank=True)
    sale = models.ForeignKey(Sale, on_delete=models.PROTECT, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["tenant", "source", "idempotency_key"], condition=Q(idempotency_key__isnull=False), name="stock_idem_unique"),
            models.UniqueConstraint(fields=["tenant", "sale", "product", "source"], condition=Q(sale__isnull=False), name="stock_sale_product_source_unique"),
        ]
        indexes = [models.Index(fields=["tenant", "shop", "product", "created_at"])]


class StockReceipt(models.Model):
    id = models.UUIDField(primary_key=True, default=uid, editable=False)
    tenant = models.ForeignKey(Tenant, on_delete=models.PROTECT)
    shop = models.ForeignKey(Shop, on_delete=models.PROTECT)
    supplier = models.ForeignKey(Supplier, on_delete=models.PROTECT, null=True, blank=True)
    reference = models.CharField(max_length=160, blank=True)
    source_file_name = models.CharField(max_length=180, blank=True)
    source_file_mime = models.CharField(max_length=100, blank=True)
    source_file_sha256 = models.CharField(max_length=64, blank=True)
    source_file_size = models.PositiveIntegerField(default=0)
    idempotency_key = models.CharField(max_length=160)
    content_hash = models.CharField(max_length=64, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["tenant", "idempotency_key"], name="receipt_idem_unique")]


class StockReceiptItem(models.Model):
    id = models.UUIDField(primary_key=True, default=uid, editable=False)
    receipt = models.ForeignKey(StockReceipt, on_delete=models.PROTECT, related_name="items")
    product = models.ForeignKey(Product, on_delete=models.PROTECT)
    quantity_milli = models.PositiveBigIntegerField()
    unit_cost_cents = models.PositiveBigIntegerField(default=0)


class FinanceEntry(models.Model):
    KINDS = [(s, s) for s in ("payable", "receivable")]
    STATUS = [(s, s) for s in ("open", "settled", "cancelled")]
    id = models.UUIDField(primary_key=True, default=uid, editable=False)
    tenant = models.ForeignKey(Tenant, on_delete=models.PROTECT)
    shop = models.ForeignKey(Shop, on_delete=models.PROTECT, null=True, blank=True)
    customer = models.ForeignKey(Customer, on_delete=models.PROTECT, null=True, blank=True)
    supplier = models.ForeignKey(Supplier, on_delete=models.PROTECT, null=True, blank=True)
    kind = models.CharField(max_length=20, choices=KINDS)
    description = models.CharField(max_length=240)
    amount_cents = models.PositiveBigIntegerField()
    due_date = models.DateField()
    status = models.CharField(max_length=20, choices=STATUS, default="open")
    settled_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)


class SyncEvent(models.Model):
    event_id = models.CharField(max_length=64, primary_key=True)
    tenant = models.ForeignKey(Tenant, on_delete=models.PROTECT)
    shop = models.ForeignKey(Shop, on_delete=models.PROTECT)
    terminal = models.ForeignKey(Terminal, on_delete=models.PROTECT)
    event_type = models.CharField(max_length=40)
    aggregate_id = models.CharField(max_length=160)
    payload = models.JSONField()
    payload_hash = models.CharField(max_length=64)
    received_at = models.DateTimeField(auto_now_add=True)


class AuditEvent(models.Model):
    id = models.UUIDField(primary_key=True, default=uid, editable=False)
    tenant = models.ForeignKey(Tenant, on_delete=models.PROTECT, null=True, blank=True)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True)
    action = models.CharField(max_length=100)
    subject_type = models.CharField(max_length=80)
    subject_id = models.CharField(max_length=160, blank=True)
    detail = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)


class BillingInvoice(models.Model):
    id = models.UUIDField(primary_key=True, default=uid, editable=False)
    tenant = models.ForeignKey(Tenant, on_delete=models.PROTECT)
    subscription = models.ForeignKey(Subscription, on_delete=models.PROTECT)
    provider = models.CharField(max_length=40, default="asaas")
    external_id = models.CharField(max_length=160, blank=True)
    amount_cents = models.PositiveBigIntegerField()
    due_date = models.DateField()
    status = models.CharField(max_length=40, default="pending")
    checkout_url = models.URLField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)


class BillingEvent(models.Model):
    id = models.UUIDField(primary_key=True, default=uid, editable=False)
    tenant = models.ForeignKey(Tenant, on_delete=models.PROTECT, null=True, blank=True)
    invoice = models.ForeignKey(BillingInvoice, on_delete=models.PROTECT, null=True, blank=True)
    provider = models.CharField(max_length=40)
    external_id = models.CharField(max_length=160, unique=True)
    event_type = models.CharField(max_length=80)
    payload = models.JSONField()
    received_at = models.DateTimeField(auto_now_add=True)


class FiscalDocument(models.Model):
    id = models.UUIDField(primary_key=True, default=uid, editable=False)
    tenant = models.ForeignKey(Tenant, on_delete=models.PROTECT)
    shop = models.ForeignKey(Shop, on_delete=models.PROTECT)
    sale = models.ForeignKey(Sale, on_delete=models.PROTECT)
    provider = models.CharField(max_length=40, default="focusnfe")
    external_id = models.CharField(max_length=160, blank=True)
    kind = models.CharField(max_length=20, default="nfce")
    status = models.CharField(max_length=40, default="pending")
    authorization = models.CharField(max_length=160, blank=True)
    xml_url = models.URLField(blank=True)
    error_message = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)


class RateLimit(models.Model):
    """Shared DB throttle for login and pairing across all web workers."""
    key = models.CharField(primary_key=True, max_length=64)
    attempts = models.PositiveIntegerField(default=0)
    expires_at = models.DateTimeField()
