from django.contrib import admin

from . import models


class ReadOnlyAdmin(admin.ModelAdmin):
    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(models.Plan)
class PlanAdmin(admin.ModelAdmin):
    list_display = ("slug", "name", "price_month_cents", "terminal_limit", "shop_limit", "active")
    search_fields = ("slug", "name")


@admin.register(models.Tenant)
class TenantAdmin(admin.ModelAdmin):
    list_display = ("name", "document", "active", "created_at")
    search_fields = ("name", "document")


@admin.register(models.Subscription)
class SubscriptionAdmin(admin.ModelAdmin):
    list_display = ("tenant", "plan", "status", "trial_ends_at", "current_period_end")
    list_filter = ("status", "plan")


@admin.register(models.Terminal)
class TerminalAdmin(admin.ModelAdmin):
    list_display = ("name", "tenant", "shop", "device_id", "version", "revoked", "last_seen_at")
    search_fields = ("name", "device_id", "tenant__name")
    readonly_fields = ("token_hash", "pairing_code_hash")


@admin.register(models.Membership)
class MembershipAdmin(admin.ModelAdmin):
    list_display = ("name", "tenant", "user", "role", "active")
    list_filter = ("role", "active")
    exclude = ("pin_hash",)


for model in (models.Shop, models.Category, models.Product, models.Customer,
              models.Supplier, models.StockReceipt, models.FinanceEntry,
              models.BillingInvoice, models.FiscalDocument):
    admin.site.register(model)


for model in (models.StockMovement, models.Sale, models.SaleItem, models.SalePayment,
              models.SyncEvent, models.AuditEvent, models.BillingEvent,
              models.StockReceiptItem):
    admin.site.register(model, ReadOnlyAdmin)
