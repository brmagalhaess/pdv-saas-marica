from django import forms
from django.conf import settings
from django.contrib import admin
from django.core.exceptions import ValidationError
from django.db.models import Count
from django.http import HttpResponseRedirect
from django.urls import reverse

from . import models
from .api_common import audit
from .views_auth import MFA_VERIFIED


class PlatformAdminSite(admin.AdminSite):
    """Django admin behind the platform login: superuser plus verified MFA.

    The site never authenticates anyone. /login/ is the only door, so the
    attempt limit and the TOTP step cannot be skipped through /control/login/.
    Only models registered below are exposed; auth.User and Group are not.
    """
    site_header = "PDV2 • Administração da plataforma"
    site_title = "PDV2"
    login_url = "/login/"

    def has_permission(self, request):
        user = request.user
        if not (user.is_active and user.is_superuser):
            return False
        if request.session.get(MFA_VERIFIED):
            return True
        # Local development runs without platform MFA; production never does.
        return settings.DEBUG and not settings.PLATFORM_MFA_REQUIRED

    def login(self, request, extra_context=None):
        if request.method == "GET" and self.has_permission(request):
            return HttpResponseRedirect(reverse("admin:index", current_app=self.name))
        return HttpResponseRedirect(self.login_url)


platform_admin = PlatformAdminSite(name="admin")


class ReadOnlyAdmin(admin.ModelAdmin):
    """Inspection only: changes go through the audited /api/v1 routes."""
    actions = None
    show_full_result_count = False

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


class PlanForm(forms.ModelForm):
    class Meta:
        model = models.Plan
        fields = "__all__"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # An empty list is a valid value; the default form field rejects it.
        if "features" in self.fields:
            self.fields["features"].required = False

    def clean_features(self):
        # Same rule as shared/licensing.py: an invalid value makes every
        # terminal of the plan fail to renew its lease.
        value = self.cleaned_data.get("features")
        if value is None:
            return []
        if (not isinstance(value, list) or len(value) > 100
                or any(not isinstance(item, str) or not item or len(item) > 80 for item in value)):
            raise ValidationError('Informe uma lista de recursos, por exemplo ["finance", "fiscal"] '
                                  "(até 100 textos de até 80 caracteres).")
        return value

    def _companies_above(self, queryset, limit):
        return (queryset.filter(tenant__subscription__plan=self.instance).values("tenant_id")
                .annotate(used=Count("id")).filter(used__gt=limit).exists())

    def _clean_limit(self, field, queryset, label):
        value = self.cleaned_data[field]
        if not 1 <= value <= 1000:
            raise ValidationError("O limite vai de 1 a 1000.")
        if not self.instance._state.adding and self._companies_above(queryset, value):
            raise ValidationError(f"Há empresas acima do novo limite de {label}.")
        return value

    def clean_terminal_limit(self):
        return self._clean_limit("terminal_limit", models.Terminal.objects.filter(revoked=False), "terminais")

    def clean_shop_limit(self):
        return self._clean_limit("shop_limit", models.Shop.objects.filter(active=True), "lojas")


@admin.register(models.Plan, site=platform_admin)
class PlanAdmin(admin.ModelAdmin):
    form = PlanForm
    actions = None
    list_display = ("slug", "name", "price_month_cents", "terminal_limit", "shop_limit", "active")
    search_fields = ("slug", "name")

    def get_readonly_fields(self, request, obj=None):
        # Sign-up and billing look plans up by slug.
        return ("slug",) if obj else ()

    def has_delete_permission(self, request, obj=None):
        return False

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        audit(None, request.user, "platform.plan_updated" if change else "platform.plan_created", obj,
              {"fields": form.changed_data, "via": "django_admin"})


class TenantForm(forms.ModelForm):
    class Meta:
        model = models.Tenant
        fields = "__all__"

    def clean_max_discount_bps(self):
        value = self.cleaned_data["max_discount_bps"]
        if value > 10000:
            raise ValidationError("O teto de desconto vai de 0 a 10000 (10000 = 100%).")
        return value


@admin.register(models.Tenant, site=platform_admin)
class TenantAdmin(admin.ModelAdmin):
    form = TenantForm
    actions = None
    list_display = ("name", "trade_name", "document", "fiscal_environment", "active", "created_at")
    search_fields = ("name", "trade_name", "document")
    # The key of the backup copy is never shown or edited here: a changed value makes
    # every replica of the company unreadable exactly when it is needed.
    exclude = ("backup_key_encrypted",)
    readonly_fields = ("backup_key_id", "backup_key_revealed_at")

    def has_add_permission(self, request):
        # Companies are created by sign-up, together with owner, shop and subscription.
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        audit(obj, request.user, "platform.tenant_updated", obj,
              {"fields": form.changed_data, "via": "django_admin"})


@admin.register(models.Subscription, site=platform_admin)
class SubscriptionAdmin(ReadOnlyAdmin):
    list_display = ("tenant", "plan", "status", "license_source", "trial_ends_at", "current_period_end")
    list_filter = ("status", "license_source", "plan")
    list_select_related = ("tenant", "plan")
    search_fields = ("tenant__name",)


@admin.register(models.Terminal, site=platform_admin)
class TerminalAdmin(ReadOnlyAdmin):
    list_display = ("name", "tenant", "shop", "device_id", "version", "revoked", "last_seen_at")
    list_select_related = ("tenant", "shop")
    search_fields = ("name", "device_id", "tenant__name")
    exclude = ("token_hash", "pairing_code_hash", "previous_token_hash")


@admin.register(models.Membership, site=platform_admin)
class MembershipAdmin(ReadOnlyAdmin):
    list_display = ("name", "tenant", "user", "role", "active")
    list_filter = ("role", "active")
    list_select_related = ("tenant", "user")
    search_fields = ("name", "user__email", "tenant__name")
    exclude = ("pin_hash",)


@admin.register(models.FinanceEntry, site=platform_admin)
class FinanceEntryAdmin(ReadOnlyAdmin):
    list_display = ("description", "tenant", "kind", "amount_cents", "due_date", "status")
    list_filter = ("kind", "status")
    list_select_related = ("tenant",)
    search_fields = ("description", "tenant__name")


@admin.register(models.BillingInvoice, site=platform_admin)
class BillingInvoiceAdmin(ReadOnlyAdmin):
    list_display = ("tenant", "provider", "external_id", "amount_cents", "due_date", "status")
    list_filter = ("status", "provider")
    list_select_related = ("tenant",)
    search_fields = ("external_id", "tenant__name")


@admin.register(models.FiscalDocument, site=platform_admin)
class FiscalDocumentAdmin(ReadOnlyAdmin):
    list_display = ("id", "tenant", "kind", "status", "authorization", "created_at")
    list_filter = ("status", "kind")
    list_select_related = ("tenant",)
    search_fields = ("external_id", "authorization", "tenant__name")


@admin.register(models.Sale, site=platform_admin)
class SaleAdmin(ReadOnlyAdmin):
    list_display = ("id", "tenant", "total_cents", "status", "fiscal_status", "created_at")
    list_filter = ("status", "fiscal_status")
    list_select_related = ("tenant",)
    search_fields = ("request_id", "tenant__name")


for model in (models.Shop, models.Category, models.Product, models.Customer,
              models.Supplier, models.StockReceipt, models.StockReceiptItem,
              models.StockMovement, models.SaleItem, models.SalePayment,
              models.SyncEvent, models.AuditEvent, models.BillingEvent):
    platform_admin.register(model, ReadOnlyAdmin)
