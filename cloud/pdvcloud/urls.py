from django.contrib import admin
from django.urls import include, path

from core import views_auth as auth, views_finance as finance, views_pages as pages
from core import views_platform as platform, views_resources as resources
from core import views_sales as sales, views_stock as stock, views_terminal as terminal
from core import views_users as users
from core import views_owner as owner
from core import views_labels as labels

admin.site.site_header = "PDV2 • Administração da plataforma"
admin.site.site_title = "PDV2"

urlpatterns = [
    path("", lambda request: pages.page(request, "landing")),
    path("login/", lambda request: pages.page(request, "login")),
    path("app/", lambda request: pages.page(request, "app")),
    path("platform/", lambda request: pages.page(request, "platform")),
    path("manifest.webmanifest", lambda request: pages.public_asset(request, "manifest")),
    path("sw.js", lambda request: pages.public_asset(request, "sw")),
    path("offline/", lambda request: pages.public_asset(request, "offline")),
    path("health", pages.health), path("health/", pages.health),
    path("control/", admin.site.urls),
    path("api/v1/csrf", auth.csrf),
    path("api/v1/auth/register", auth.register),
    path("api/v1/auth/login", auth.login_view),
    path("api/v1/auth/mfa/setup", auth.mfa_setup),
    path("api/v1/auth/mfa/status", auth.mfa_status),
    path("api/v1/auth/mfa/enroll", auth.mfa_enroll),
    path("api/v1/auth/mfa/setup/confirm", auth.mfa_setup_confirm),
    path("api/v1/auth/mfa/verify", auth.mfa_verify),
    path("api/v1/auth/mfa/recovery-codes", auth.mfa_recovery_codes),
    path("api/v1/auth/logout", auth.logout_view),
    path("api/v1/auth/me", auth.me),
    path("api/v1/plans", auth.plans),
    path("api/v1/subscription", terminal.subscription),
    path("api/v1/stock", stock.stock),
    path("api/v1/stock/movements", stock.movements),
    path("api/v1/stock/receipts", stock.receipts),
    path("api/v1/finance", finance.finance),
    path("api/v1/finance/<uuid:pk>/settle", finance.settle),
    path("api/v1/finance/<uuid:pk>", finance.finance_detail),
    path("api/v1/users", users.users),
    path("api/v1/users/<uuid:pk>/pin", users.user_pin),
    path("api/v1/users/<uuid:pk>", users.user_detail),
    path("api/v1/terminals", terminal.terminals),
    path("api/v1/terminals/<uuid:pk>", terminal.terminal_detail),
    path("api/v1/terminal/pair", terminal.pair),
    path("api/v1/terminal/bootstrap", terminal.bootstrap),
    path("api/v1/terminal/heartbeat", terminal.heartbeat),
    path("api/v1/terminal/sync", terminal.sync),
    path("api/v1/label-settings", labels.label_settings),
    path("api/v1/sync/conflicts", terminal.conflicts),
    path("api/v1/sync/conflicts/<uuid:pk>", terminal.conflict_detail),
    path("api/v1/sales", sales.sales),
    path("api/v1/sales/people", sales.sale_people),
    path("api/v1/sales/<uuid:pk>/cancel", sales.sale_reverse, {"action": "cancel"}),
    path("api/v1/sales/<uuid:pk>/refund", sales.sale_reverse, {"action": "refund"}),
    path("api/v1/sales/<uuid:pk>", sales.sale_detail),
    path("api/v1/dashboard", sales.dashboard),
    path("api/v1/owner/monitoring", owner.monitoring),
    path("api/v1/audit", sales.audit_events),
    path("api/v1/platform/tenants", platform.tenants),
    path("api/v1/platform/tenants/<uuid:pk>", platform.tenant_detail),
    path("api/v1/platform/plans", platform.platform_plans),
    path("api/v1/platform/plans/<uuid:pk>", platform.platform_plan_detail),
    path("api/v1/platform/subscriptions", platform.subscriptions),
    path("api/v1/platform/subscriptions/<uuid:pk>", platform.subscription_detail),
    path("api/v1/platform/subscriptions/<uuid:pk>/vip", platform.subscription_vip),
    path("api/v1/platform/terminals", platform.platform_terminals),
    path("api/v1/platform/terminals/<uuid:pk>", platform.platform_terminal_detail),
    path("api/v1/platform/audit", platform.platform_audit),
    path("api/v1/platform/invoices", platform.invoices),
    path("api/v1/platform/users", platform.platform_users),
    path("api/v1/platform/users/<int:pk>", platform.platform_user_detail),
    path("api/v1/ops/", include("operations.urls")),
    path("api/v1/<str:kind>", resources.collection),
    path("api/v1/<str:kind>/<uuid:pk>", resources.detail),
]
