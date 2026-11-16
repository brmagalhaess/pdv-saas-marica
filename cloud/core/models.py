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
    trade_name = models.CharField(max_length=160, blank=True)
    document = models.CharField(max_length=30, blank=True)
    state_registration = models.CharField(max_length=30, blank=True)
    municipal_registration = models.CharField(max_length=30, blank=True)
    address_line = models.CharField(max_length=180, blank=True)
    address_number = models.CharField(max_length=20, blank=True)
    address_complement = models.CharField(max_length=80, blank=True)
    address_neighborhood = models.CharField(max_length=100, blank=True)
    address_city = models.CharField(max_length=100, blank=True)
    address_state = models.CharField(max_length=2, blank=True)
    address_zip = models.CharField(max_length=8, blank=True)
    tax_regime = models.CharField(max_length=30, default="simples_nacional")
    fiscal_environment = models.CharField(max_length=20, default="non_fiscal")
    nfce_series = models.PositiveIntegerField(default=1)
    nfce_next_number = models.PositiveBigIntegerField(default=1)
    roles_version = models.PositiveIntegerField(default=1)
    # Percentage in basis points: 10000 = 100%. This is the hard company
    # ceiling for discounts, including manager approvals.
    max_discount_bps = models.PositiveIntegerField(default=10000)
    # Due date of a credit-account (fiado) sale, counted from the sale day.
    credit_term_days = models.PositiveIntegerField(default=30)
    # Local operating schedule consumed by terminals for opening control.
    # Example: {"mon": {"enabled": true, "open": "08:00", "close": "18:00"}}
    business_hours = models.JSONField(default=dict, blank=True)
    # Label stock used by the panel's label print (Administração > Impressoras).
    # Example: {"width_mm": 50, "height_mm": 30, "gap_mm": 2, "sensor": "gap"}
    label_settings = models.JSONField(default=dict, blank=True)
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
    # Sales commission in basis points (100 = 1%) credited when this member
    # is the seller of a sale, or its operator when no seller was chosen.
    commission_bps = models.PositiveIntegerField(default=0)
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["tenant", "user"], name="member_user_tenant_unique")]


class Category(models.Model):
    id = models.UUIDField(primary_key=True, default=uid, editable=False)
    tenant = models.ForeignKey(Tenant, on_delete=models.PROTECT)
    name = models.CharField(max_length=120)
    # Overrides the member rate for items of this category; null = member rate.
    commission_bps = models.PositiveIntegerField(null=True, blank=True)
    active = models.BooleanField(default=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["tenant", "name"], name="category_tenant_name_unique")]


class ProductGroup(models.Model):
    """Variation grid (e.g. Cor × Tamanho). Each combination is an ordinary
    Product, so sales, stock, sync and receipts need no grid awareness."""
    id = models.UUIDField(primary_key=True, default=uid, editable=False)
    tenant = models.ForeignKey(Tenant, on_delete=models.PROTECT, related_name="product_groups")
    name = models.CharField(max_length=140)
    category = models.ForeignKey(Category, on_delete=models.PROTECT, null=True, blank=True)
    # [{"name": "Cor", "values": ["Azul", "Preto"]}, {"name": "Tamanho", "values": ["P", "M"]}]
    attributes = models.JSONField(default=list)
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["tenant", "name"], name="product_group_tenant_name")]


class Product(models.Model):
    UNITS = [(s, s) for s in ("UN", "KG", "L")]
    id = models.UUIDField(primary_key=True, default=uid, editable=False)
    tenant = models.ForeignKey(Tenant, on_delete=models.PROTECT, related_name="products")
    category = models.ForeignKey(Category, on_delete=models.PROTECT, null=True, blank=True)
    sku = models.CharField(max_length=80)
    ncm = models.CharField(max_length=8, blank=True)
    barcode = models.CharField(max_length=40, blank=True)
    scale_code = models.CharField(max_length=6, blank=True)
    name = models.CharField(max_length=180)
    variant = models.CharField(max_length=120, blank=True)
    group = models.ForeignKey(ProductGroup, on_delete=models.PROTECT, null=True, blank=True, related_name="variants")
    variant_values = models.JSONField(default=dict, blank=True)
    unit = models.CharField(max_length=2, choices=UNITS, default="UN")
    price_cents = models.PositiveBigIntegerField(default=0)
    cost_cents = models.PositiveBigIntegerField(default=0)
    stock_minimum_milli = models.PositiveBigIntegerField(default=0)
    stock_controlled = models.BooleanField(default=True)
    image_url = models.CharField(max_length=500, blank=True)
    tax_fields = models.JSONField(default=dict, blank=True)
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["tenant", "sku"], name="product_tenant_sku_unique"),
            models.UniqueConstraint(fields=["tenant", "barcode"], condition=~Q(barcode=""), name="product_tenant_barcode_unique"),
            models.UniqueConstraint(fields=["tenant", "scale_code"], condition=~Q(scale_code=""), name="product_tenant_scale_code_unique"),
        ]
        indexes = [models.Index(fields=["tenant", "name"])]


class Customer(models.Model):
    id = models.UUIDField(primary_key=True, default=uid, editable=False)
    tenant = models.ForeignKey(Tenant, on_delete=models.PROTECT)
    name = models.CharField(max_length=180)
    document = models.CharField(max_length=30, blank=True)
    email = models.EmailField(blank=True)
    phone = models.CharField(max_length=30, blank=True)
    # Maximum open balance for credit-account (fiado) sales; 0 = no fiado.
    credit_limit_cents = models.PositiveBigIntegerField(default=0)
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
    heartbeat_pending_events = models.PositiveIntegerField(default=0)
    heartbeat_error = models.CharField(max_length=300, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["tenant", "device_id"], condition=~Q(device_id=""), name="terminal_tenant_device_unique")]


class Promotion(models.Model):
    """Price rule evaluated by the terminal, offline included.

    price: promotional unit price in a period · wholesale: unit price from
    `min_quantity_milli` up · buy_x_pay_y: every `buy_quantity` units, pay
    `pay_quantity` (UN only). The best rule for the customer wins; no stacking.
    """
    KINDS = [(s, s) for s in ("price", "wholesale", "buy_x_pay_y")]
    id = models.UUIDField(primary_key=True, default=uid, editable=False)
    tenant = models.ForeignKey(Tenant, on_delete=models.PROTECT, related_name="promotions")
    name = models.CharField(max_length=80)
    kind = models.CharField(max_length=20, choices=KINDS)
    product = models.ForeignKey("Product", on_delete=models.PROTECT, null=True, blank=True)
    category = models.ForeignKey(Category, on_delete=models.PROTECT, null=True, blank=True)
    price_cents = models.PositiveBigIntegerField(null=True, blank=True)
    min_quantity_milli = models.PositiveBigIntegerField(null=True, blank=True)
    buy_quantity = models.PositiveIntegerField(null=True, blank=True)
    pay_quantity = models.PositiveIntegerField(null=True, blank=True)
    starts_at = models.DateTimeField(null=True, blank=True)
    ends_at = models.DateTimeField(null=True, blank=True)
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)


class Sale(models.Model):
    STATUS = [(s, s) for s in ("completed", "cancelled", "refunded")]
    id = models.UUIDField(primary_key=True, editable=False)
    tenant = models.ForeignKey(Tenant, on_delete=models.PROTECT, related_name="sales")
    shop = models.ForeignKey(Shop, on_delete=models.PROTECT)
    terminal = models.ForeignKey(Terminal, on_delete=models.PROTECT, null=True, blank=True)
    customer = models.ForeignKey(Customer, on_delete=models.PROTECT, null=True, blank=True)
    customer_name_snapshot = models.CharField(max_length=180, blank=True)
    customer_document_snapshot = models.CharField(max_length=30, blank=True)
    # Who ran the register and who is credited with the sale. Null only for
    # sales queued by terminals released before these fields existed.
    operator = models.ForeignKey(Membership, on_delete=models.PROTECT, null=True, blank=True,
                                 related_name="operated_sales")
    operator_name_snapshot = models.CharField(max_length=160, blank=True)
    seller = models.ForeignKey(Membership, on_delete=models.PROTECT, null=True, blank=True,
                               related_name="credited_sales")
    seller_name_snapshot = models.CharField(max_length=160, blank=True)
    request_id = models.CharField(max_length=160)
    # Plain id instead of FK: sales synced before CashSession existed reference
    # sessions rebuilt later from SyncEvent, and ordering must never block sync.
    cash_session_id = models.CharField(max_length=160, blank=True, db_index=True)
    content_hash = models.CharField(max_length=64)
    gross_cents = models.PositiveBigIntegerField()
    # Promotions applied by the terminal, kept apart from the operator's manual discount.
    promo_discount_cents = models.PositiveBigIntegerField(default=0)
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
    promo_discount_cents = models.PositiveBigIntegerField(default=0)
    promotion = models.ForeignKey(Promotion, on_delete=models.PROTECT, null=True, blank=True)
    promotion_name = models.CharField(max_length=80, blank=True)
    discount_cents = models.PositiveBigIntegerField()
    line_total_cents = models.PositiveBigIntegerField()
    # Product cost when the sale was recorded, so margin reports of closed
    # periods do not move with later purchases. Null only before the backfill.
    unit_cost_cents = models.PositiveBigIntegerField(null=True, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["sale", "line_number"], name="sale_item_line_unique")]

    def save(self, *args, **kwargs):
        if self.unit_cost_cents is None:
            # Read from the database: the product instance in hand may predate a stock receipt.
            self.unit_cost_cents = (Product.objects.filter(pk=self.product_id)
                                    .values_list("cost_cents", flat=True).first() or 0)
        super().save(*args, **kwargs)


class PaymentMethod(models.Model):
    """Per-company payment configuration.

    Built-in kinds keep the semantics the register depends on (cash drawer,
    fiado, store credit); `other` rows are company-defined (meal voucher,
    cheque...). Fee and settlement days feed net-revenue reports.
    """
    KINDS = [(s, s) for s in ("cash", "pix", "card_credit", "card_debit", "credit_account", "store_credit", "other")]
    id = models.UUIDField(primary_key=True, default=uid, editable=False)
    tenant = models.ForeignKey(Tenant, on_delete=models.PROTECT, related_name="payment_methods")
    kind = models.CharField(max_length=20, choices=KINDS)
    name = models.CharField(max_length=60)
    fee_bps = models.PositiveIntegerField(default=0)
    settlement_days = models.PositiveIntegerField(default=0)
    active = models.BooleanField(default=True)
    position = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["tenant", "kind"], condition=~models.Q(kind="other"),
                                               name="payment_method_builtin_once")]


class SalePayment(models.Model):
    id = models.UUIDField(primary_key=True, default=uid, editable=False)
    sale = models.ForeignKey(Sale, on_delete=models.PROTECT, related_name="payments")
    line_number = models.PositiveIntegerField()
    method = models.CharField(max_length=20)
    # Set for method="other" (company-defined payment such as a meal voucher).
    payment_method = models.ForeignKey(PaymentMethod, on_delete=models.PROTECT, null=True, blank=True)
    amount_cents = models.PositiveBigIntegerField()

    class Meta:
        constraints = [models.UniqueConstraint(fields=["sale", "line_number"], name="sale_payment_line_unique")]


class Commission(models.Model):
    """Commission accrued on a sale. Reversal of a paid one is a negative row
    so it is discounted from the next payment instead of rewriting history."""
    STATUS = [(s, s) for s in ("pending", "paid", "cancelled")]
    id = models.UUIDField(primary_key=True, default=uid, editable=False)
    tenant = models.ForeignKey(Tenant, on_delete=models.PROTECT, related_name="commissions")
    sale = models.ForeignKey(Sale, on_delete=models.PROTECT, related_name="commissions")
    seller = models.ForeignKey(Membership, on_delete=models.PROTECT, related_name="commissions")
    base_cents = models.BigIntegerField()
    amount_cents = models.BigIntegerField()
    status = models.CharField(max_length=20, choices=STATUS, default="pending")
    reversal = models.BooleanField(default=False)
    # Set on the negative row created by a partial return.
    sale_return = models.ForeignKey("SaleReturn", on_delete=models.PROTECT, null=True, blank=True)
    finance_entry = models.ForeignKey("FinanceEntry", on_delete=models.PROTECT, null=True, blank=True)
    paid_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["sale", "reversal"], condition=models.Q(sale_return__isnull=True),
                                               name="commission_once_per_sale"),
                       models.UniqueConstraint(fields=["sale_return"], condition=models.Q(sale_return__isnull=False),
                                               name="commission_once_per_return")]
        indexes = [models.Index(fields=["tenant", "seller", "status"])]


class CreditReceipt(models.Model):
    """Fiado paid at the terminal; settles the oldest open receivables."""
    id = models.UUIDField(primary_key=True, editable=False)
    tenant = models.ForeignKey(Tenant, on_delete=models.PROTECT)
    shop = models.ForeignKey(Shop, on_delete=models.PROTECT)
    terminal = models.ForeignKey(Terminal, on_delete=models.PROTECT)
    customer = models.ForeignKey(Customer, on_delete=models.PROTECT, related_name="credit_receipts")
    operator = models.ForeignKey(Membership, on_delete=models.PROTECT, null=True, blank=True, related_name="+")
    method = models.CharField(max_length=20)
    amount_cents = models.PositiveBigIntegerField()
    cash_session_id = models.CharField(max_length=160, blank=True)
    created_at = models.DateTimeField()
    received_at = models.DateTimeField(auto_now_add=True)


class SupplierProductCode(models.Model):
    """Remembers how a supplier's item code maps to our product (NF-e import)."""
    id = models.UUIDField(primary_key=True, default=uid, editable=False)
    tenant = models.ForeignKey(Tenant, on_delete=models.PROTECT)
    supplier = models.ForeignKey(Supplier, on_delete=models.PROTECT, related_name="product_codes")
    supplier_code = models.CharField(max_length=60)
    product = models.ForeignKey(Product, on_delete=models.PROTECT, related_name="supplier_codes")
    # Our units per supplier unit, in thousandths (a box of 12 = 12000).
    factor_milli = models.PositiveBigIntegerField(default=1000)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["supplier", "supplier_code"], name="supplier_code_once")]


class SaleReturn(models.Model):
    """Partial or total return, registered at the terminal or in the web panel.

    `external` = refunded outside the PDV (Pix sent back, card chargeback); panel only.
    """
    REFUNDS = [(s, s) for s in ("cash", "store_credit", "account", "external")]
    id = models.UUIDField(primary_key=True, editable=False)
    tenant = models.ForeignKey(Tenant, on_delete=models.PROTECT, related_name="sale_returns")
    shop = models.ForeignKey(Shop, on_delete=models.PROTECT)
    terminal = models.ForeignKey(Terminal, on_delete=models.PROTECT, null=True, blank=True)
    sale = models.ForeignKey(Sale, on_delete=models.PROTECT, related_name="returns")
    customer = models.ForeignKey(Customer, on_delete=models.PROTECT, null=True, blank=True)
    # Null terminal = registered in the web panel (created_by is then set).
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+")
    operator = models.ForeignKey(Membership, on_delete=models.PROTECT, null=True, blank=True, related_name="+")
    supervisor = models.ForeignKey(Membership, on_delete=models.PROTECT, null=True, blank=True, related_name="+")
    cash_session_id = models.CharField(max_length=160, blank=True)
    refund_method = models.CharField(max_length=20, choices=REFUNDS)
    total_cents = models.PositiveBigIntegerField()
    reason = models.CharField(max_length=240)
    content_hash = models.CharField(max_length=64)
    created_at = models.DateTimeField()
    received_at = models.DateTimeField(auto_now_add=True)


class SaleReturnItem(models.Model):
    id = models.UUIDField(primary_key=True, default=uid, editable=False)
    sale_return = models.ForeignKey(SaleReturn, on_delete=models.PROTECT, related_name="items")
    product = models.ForeignKey(Product, on_delete=models.PROTECT)
    quantity_milli = models.PositiveBigIntegerField()
    amount_cents = models.PositiveBigIntegerField()


class CustomerCredit(models.Model):
    """Store-credit ledger: positive issues credit, negative uses it."""
    SOURCES = [(s, s) for s in ("return", "sale", "sale_cancel", "account_overpaid", "manual")]
    id = models.UUIDField(primary_key=True, default=uid, editable=False)
    tenant = models.ForeignKey(Tenant, on_delete=models.PROTECT)
    customer = models.ForeignKey(Customer, on_delete=models.PROTECT, related_name="credits")
    delta_cents = models.BigIntegerField()
    source = models.CharField(max_length=20, choices=SOURCES)
    description = models.CharField(max_length=240)
    sale = models.ForeignKey(Sale, on_delete=models.PROTECT, null=True, blank=True)
    sale_return = models.ForeignKey(SaleReturn, on_delete=models.PROTECT, null=True, blank=True)
    idempotency_key = models.CharField(max_length=200, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)


class CashSession(models.Model):
    """Register shift materialized from terminal cash.* events."""
    id = models.UUIDField(primary_key=True, editable=False)
    tenant = models.ForeignKey(Tenant, on_delete=models.PROTECT, related_name="cash_sessions")
    shop = models.ForeignKey(Shop, on_delete=models.PROTECT)
    terminal = models.ForeignKey(Terminal, on_delete=models.PROTECT)
    operator = models.ForeignKey(Membership, on_delete=models.PROTECT, null=True, blank=True,
                                 related_name="opened_cash_sessions")
    opening_cents = models.PositiveBigIntegerField(default=0)
    opened_at = models.DateTimeField()
    closed_at = models.DateTimeField(null=True, blank=True)
    closed_by = models.ForeignKey(Membership, on_delete=models.PROTECT, null=True, blank=True,
                                  related_name="closed_cash_sessions")
    supervisor = models.ForeignKey(Membership, on_delete=models.PROTECT, null=True, blank=True,
                                   related_name="supervised_cash_sessions")
    counted_cents = models.PositiveBigIntegerField(null=True, blank=True)
    # Signed: a cash sale cancelled after a withdrawal leaves the drawer owing.
    expected_cents = models.BigIntegerField(null=True, blank=True)
    difference_cents = models.BigIntegerField(null=True, blank=True)
    # True when a movement or close arrived for a session whose opening was
    # never received; the opening amount is then unknown.
    incomplete = models.BooleanField(default=False)

    class Meta:
        indexes = [models.Index(fields=["tenant", "-opened_at"])]


class CashMovement(models.Model):
    KINDS = [(s, s) for s in ("withdrawn", "supplied")]
    id = models.UUIDField(primary_key=True, editable=False)
    session = models.ForeignKey(CashSession, on_delete=models.PROTECT, related_name="movements")
    kind = models.CharField(max_length=20, choices=KINDS)
    amount_cents = models.PositiveBigIntegerField()
    reason = models.CharField(max_length=240)
    operator = models.ForeignKey(Membership, on_delete=models.PROTECT, null=True, blank=True)
    created_at = models.DateTimeField()


class StockMovement(models.Model):
    SOURCES = [(s, s) for s in ("manual", "receipt", "sale", "void", "terminal_adjustment", "return",
                                "inventory", "transfer")]
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
            # Partial returns repeat (sale, product, source); their own idempotency key protects them.
            models.UniqueConstraint(fields=["tenant", "sale", "product", "source"], condition=Q(sale__isnull=False) & ~Q(source="return"), name="stock_sale_product_source_unique"),
        ]
        indexes = [models.Index(fields=["tenant", "shop", "product", "created_at"])]


class StockBalance(models.Model):
    """Current quantity per shop and product, kept in step with StockMovement.

    Written only by core.stock_balance in the same transaction as each movement;
    `manage.py reconcile_stock` compares it with the movement ledger.
    """
    id = models.BigAutoField(primary_key=True)
    tenant = models.ForeignKey(Tenant, on_delete=models.PROTECT)
    shop = models.ForeignKey(Shop, on_delete=models.PROTECT)
    product = models.ForeignKey(Product, on_delete=models.PROTECT, related_name="balances")
    quantity_milli = models.BigIntegerField(default=0)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["shop", "product"], name="stock_balance_shop_product")]
        indexes = [models.Index(fields=["tenant", "shop"])]


class InventoryCount(models.Model):
    """Guided stock count. Each counted item stores the balance at the moment
    it was counted, so sales made during the count are not read as losses."""
    STATUS = [(s, s) for s in ("open", "closed", "cancelled")]
    id = models.UUIDField(primary_key=True, default=uid, editable=False)
    tenant = models.ForeignKey(Tenant, on_delete=models.PROTECT, related_name="inventory_counts")
    shop = models.ForeignKey(Shop, on_delete=models.PROTECT)
    category = models.ForeignKey(Category, on_delete=models.PROTECT, null=True, blank=True)
    blind = models.BooleanField(default=True)
    status = models.CharField(max_length=20, choices=STATUS, default="open")
    note = models.CharField(max_length=240, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    closed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+")
    closed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["shop"], condition=Q(status="open"), name="one_open_inventory_per_shop")]


class InventoryCountItem(models.Model):
    id = models.UUIDField(primary_key=True, default=uid, editable=False)
    count = models.ForeignKey(InventoryCount, on_delete=models.PROTECT, related_name="items")
    product = models.ForeignKey(Product, on_delete=models.PROTECT)
    counted_milli = models.BigIntegerField()
    balance_at_count_milli = models.BigIntegerField()
    counted_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    counted_at = models.DateTimeField()

    class Meta:
        constraints = [models.UniqueConstraint(fields=["count", "product"], name="inventory_item_once")]


class StockTransfer(models.Model):
    STATUS = [(s, s) for s in ("sent", "received", "cancelled")]
    id = models.UUIDField(primary_key=True, default=uid, editable=False)
    tenant = models.ForeignKey(Tenant, on_delete=models.PROTECT, related_name="stock_transfers")
    from_shop = models.ForeignKey(Shop, on_delete=models.PROTECT, related_name="transfers_out")
    to_shop = models.ForeignKey(Shop, on_delete=models.PROTECT, related_name="transfers_in")
    status = models.CharField(max_length=20, choices=STATUS, default="sent")
    note = models.CharField(max_length=240, blank=True)
    idempotency_key = models.CharField(max_length=160)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    finished_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+")
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["tenant", "idempotency_key"], name="transfer_idempotency")]


class StockTransferItem(models.Model):
    id = models.UUIDField(primary_key=True, default=uid, editable=False)
    transfer = models.ForeignKey(StockTransfer, on_delete=models.PROTECT, related_name="items")
    product = models.ForeignKey(Product, on_delete=models.PROTECT)
    quantity_milli = models.PositiveBigIntegerField()
    received_milli = models.PositiveBigIntegerField(null=True, blank=True)


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
    # Snapshot of the moving weighted average immediately after this receipt.
    # Keeping it on the immutable receipt item makes the cost history auditable.
    average_cost_cents = models.PositiveBigIntegerField(null=True, blank=True)


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
    # Set for receivables created by a credit-account (fiado) sale.
    sale = models.ForeignKey("Sale", on_delete=models.PROTECT, null=True, blank=True,
                             related_name="finance_entries")
    # Null for entries created by terminal sync rather than a person.
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True)
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


class SyncConflict(models.Model):
    """Durable reconciliation record for an offline event.

    What happened at the counter is a fact: the event is accepted and every
    divergence from the central records (negative balance, discount above the
    current limit, sale already reversed in the panel...) becomes one row per
    reason for the business admin. `rejected_event` marks an event that could
    not be applied at all and was quarantined with its original payload.
    """
    STATUS = [(s, s) for s in ("open", "acknowledged", "resolved", "ignored")]
    id = models.UUIDField(primary_key=True, default=uid, editable=False)
    tenant = models.ForeignKey(Tenant, on_delete=models.PROTECT, related_name="sync_conflicts")
    shop = models.ForeignKey(Shop, on_delete=models.PROTECT)
    terminal = models.ForeignKey(Terminal, on_delete=models.PROTECT)
    event_id = models.CharField(max_length=64)
    event_type = models.CharField(max_length=40)
    aggregate_id = models.CharField(max_length=160)
    reason = models.CharField(max_length=120, default="negative_stock")
    status = models.CharField(max_length=20, choices=STATUS, default="open")
    detail = models.JSONField(default=dict)
    resolution = models.TextField(blank=True)
    resolved_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
                                    null=True, blank=True, related_name="resolved_sync_conflicts")
    resolved_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["terminal", "event_id", "reason"],
                                               name="sync_conflict_event_reason_unique")]
        indexes = [models.Index(fields=["tenant", "status", "-created_at"]),
                   models.Index(fields=["terminal", "status", "-created_at"])]


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


class PlatformMFA(models.Model):
    """TOTP enrollment for users who can access the SaaS control plane.

    The TOTP seed is encrypted with the same Fernet key used for provider
    credentials. Recovery codes are stored as one-way Django password hashes
    and are removed after a successful use.
    """
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
                                related_name="platform_mfa")
    secret_encrypted = models.TextField(blank=True)
    enabled = models.BooleanField(default=False)
    recovery_codes = models.JSONField(default=list, blank=True)
    failed_attempts = models.PositiveIntegerField(default=0)
    locked_until = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    confirmed_at = models.DateTimeField(null=True, blank=True)
    last_used_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"MFA {self.user_id}"
