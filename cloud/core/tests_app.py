"""PDV2 Gestão app API (/api/v1/app/*): who gets in, tokens, isolation and the numbers.

The JSON examples of docs/APP-GESTAO.md are the fixtures shared with the iOS
and Android apps; the key sets below are copied from them, so any rename
breaks these tests before it breaks a phone.
"""
import json
import re
import uuid
from datetime import datetime, time, timedelta

import pyotp
from django.contrib.auth import get_user_model
from django.contrib.auth.hashers import make_password
from django.contrib.auth.models import Group
from django.test import Client, TestCase, override_settings
from django.utils import timezone

from shared.secrets import encrypt_secret
from .models import (AccountDeletionRequest, AppAccess, AppDevice, AuditEvent, CashMovement, CashSession,
                     CreditReceipt, Customer, FinanceEntry, Membership, Plan, PlatformMFA, Product, Sale, SaleItem,
                     SalePayment, SaleReturn, Shop, StockBalance, Subscription, SyncConflict, Tenant, Terminal)

LOCAL_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}-03:00$")
PASSWORD = "Senha-forte-1"
DEVICE = {"platform": "ios", "name": "iPhone", "model": "iPhone16,2", "os_version": "iOS 26.1",
          "app_version": "1.0.0 (1)"}
ALL = ["sales", "cash", "stock", "credit", "terminals"]

# Key sets of the contract examples (docs/APP-GESTAO.md §6).
TOKEN_KEYS = {"access_token", "access_expires_at", "access_expires_in", "refresh_token", "refresh_expires_at"}
DEVICE_KEYS = {"id", "platform", "name", "model", "os_version", "app_version", "created_at", "last_seen_at", "current"}
TENANT_ACCESS_KEYS = {"id", "name", "all_shops", "shops", "permissions", "access_expires_on"}
ME_KEYS = {"user", "device", "platform", "tenants", "links", "server_time"}
LINK_KEYS = {"privacy_url", "support_url", "account_deletion_url"}
PLATFORM_ME_KEYS = {"enabled", "superuser", "can_manage_access", "roles"}
DASHBOARD_KEYS = {"generated_at", "tenant", "shop", "shops", "period", "permissions", "sales", "payments", "series",
                  "cash", "stock", "credit", "terminals"}
PERIOD_KEYS = {"key", "label", "date_from", "date_to", "from", "to"}
SALES_KEYS = {"count", "revenue_cents", "average_ticket_cents", "cancelled_count", "cancelled_cents", "returns_cents",
              "net_revenue_cents"}
PAYMENTS_KEYS = {"cash_cents", "pix_cents", "card_credit_cents", "card_debit_cents", "other_cents",
                 "received_total_cents", "credit_account_cents", "store_credit_cents", "credit_received_cents"}
CASH_KEYS = {"open_count", "closed_count", "shortage_count", "shortage_cents"}
STOCK_KEYS = {"zeroed_count", "below_minimum_count"}
CREDIT_KEYS = {"open_cents", "overdue_cents", "overdue_customers"}
TERMINALS_KEYS = {"total", "online", "offline", "unpaired", "online_window_seconds", "conflicts_open"}
SALE_ROW_KEYS = {"id", "created_at", "shop", "terminal_name", "station_name", "operator_name", "customer_name",
                 "total_cents", "status", "items_count", "payment_methods"}
SALE_DETAIL_KEYS = {"id", "created_at", "received_at", "status", "shop", "terminal_name", "station_name",
                    "operator_name", "seller_name", "customer_name", "gross_cents", "promo_discount_cents",
                    "discount_cents", "total_cents", "change_cents", "items", "payments", "returns", "cancelled_at",
                    "cancellation_reason"}
ITEM_KEYS = {"line", "name", "unit", "quantity_milli", "unit_price_cents", "gross_cents", "promo_discount_cents",
             "discount_cents", "line_total_cents"}
RETURN_KEYS = {"id", "created_at", "total_cents", "refund_method", "refund_label", "reason"}
SESSION_KEYS = {"id", "shop", "terminal_name", "station_name", "operator_name", "status", "opened_at", "closed_at",
                "opening_cents", "sales_count", "sales_total_cents", "cash_net_cents", "supplied_cents",
                "withdrawn_cents", "expected_cents", "counted_cents", "difference_cents", "incomplete"}
SESSION_DETAIL_EXTRA = {"closed_by_name", "supervisor_name", "credit_received_cents", "by_method", "movements"}
MOVEMENT_KEYS = {"id", "kind", "label", "amount_cents", "reason", "operator_name", "created_at"}
STOCK_ROW_KEYS = {"shop", "product_id", "name", "sku", "unit", "quantity_milli", "minimum_milli", "status"}
CREDIT_ROW_KEYS = {"customer_id", "name", "phone", "open_cents", "overdue_cents", "oldest_due_date", "entries_count"}
CREDIT_TOTAL_KEYS = {"open_cents", "overdue_cents", "customers", "overdue_customers"}
TERMINAL_ROW_KEYS = {"id", "name", "shop", "status", "version", "last_seen_at", "pending_events", "error"}
CONFLICT_ROW_KEYS = {"id", "created_at", "shop", "terminal_name", "reason", "reason_text", "status", "status_text"}
ACCESS_KEYS = {"id", "scope", "user", "tenant", "all_shops", "shops", "permissions", "reason", "granted_by",
               "granted_at", "expires_on", "status", "status_text", "revoked_at", "revoked_by", "revoke_reason",
               "devices_active"}
PLATFORM_DEVICE_KEYS = {"id", "platform", "name", "model", "os_version", "app_version", "created_at", "last_seen_at",
                        "last_ip", "user", "status", "revoked_at", "revoke_reason"}
PLATFORM_TENANT_KEYS = {"id", "name", "active", "subscription_status", "subscription_status_text", "shops_count",
                        "terminals_total", "terminals_offline", "conflicts_open", "last_sale_at", "last_seen_at",
                        "health", "health_text"}
OPTION_MEMBER_KEYS = {"user_id", "name", "email", "role", "role_text", "has_active_access"}
DELETION_KEYS = {"id", "email", "user", "source", "note", "status", "status_text", "created_at", "handled_at",
                 "handled_by", "handled_note"}


def local_midnight(day):
    return timezone.make_aware(datetime.combine(day, time.min), timezone.get_current_timezone())


class AppBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        User = get_user_model()

        def user(email, name, **extra):
            return User.objects.create_user(username=email, email=email, password=PASSWORD, first_name=name, **extra)
        cls.owner = user("dona@mercado.test", "Dona")
        cls.manager = user("gerente@mercado.test", "Gerente")
        cls.outsider = user("fora@outra.test", "Fora")
        cls.root = User.objects.create_superuser(username="root@crowntech.test", email="root@crowntech.test",
                                                 password=PASSWORD, first_name="Bruno")
        cls.support = user("suporte@crowntech.test", "Suporte", is_staff=True)
        cls.support.groups.add(Group.objects.get(name="platform_support"))
        cls.tenant = Tenant.objects.create(name="Mercado Bom Preço")
        cls.other_tenant = Tenant.objects.create(name="Outra Empresa")
        plan = Plan.objects.get(slug="essential")
        for tenant in (cls.tenant, cls.other_tenant):
            Subscription.objects.create(tenant=tenant, plan=plan, status="trial",
                                        trial_ends_at=timezone.now() + timedelta(days=14))
        Membership.objects.create(tenant=cls.tenant, user=cls.owner, name="Dona", role="owner")
        cls.manager_member = Membership.objects.create(tenant=cls.tenant, user=cls.manager, name="Gerente",
                                                       role="manager")
        Membership.objects.create(tenant=cls.other_tenant, user=cls.outsider, name="Fora", role="owner")
        cls.centro = Shop.objects.create(tenant=cls.tenant, name="Loja Centro")
        cls.bairro = Shop.objects.create(tenant=cls.tenant, name="Loja Bairro")
        cls.foreign_shop = Shop.objects.create(tenant=cls.other_tenant, name="Loja Fora")
        cls.terminal = Terminal.objects.create(tenant=cls.tenant, shop=cls.centro, name="Caixa 1", token_hash="a" * 64,
                                               last_seen_at=timezone.now(), version="2.1.0")
        cls.terminal_bairro = Terminal.objects.create(tenant=cls.tenant, shop=cls.bairro, name="Caixa B",
                                                      token_hash="b" * 64,
                                                      last_seen_at=timezone.now() - timedelta(minutes=20))
        cls.foreign_terminal = Terminal.objects.create(tenant=cls.other_tenant, shop=cls.foreign_shop, name="Caixa X",
                                                       token_hash="c" * 64)
        cls.product = Product.objects.create(tenant=cls.tenant, sku="7891000100103", name="Leite integral 1 L",
                                             price_cents=500, stock_minimum_milli=12000)

    # helpers ---------------------------------------------------------------
    def grant(self, user, tenant=None, shops=(), permissions=None, scope="tenant", **extra):
        row = AppAccess.objects.create(user=user, scope=scope, tenant=tenant if scope == "tenant" else None,
                                       permissions=list(permissions if permissions is not None else ALL),
                                       granted_by=self.root, reason="Teste", **extra)
        row.shops.set(shops)
        return row

    def post(self, path, body, client=None, **headers):
        return (client or self.client).post(path, data=json.dumps(body), content_type="application/json", **headers)

    def login(self, email, password=PASSWORD, **headers):
        return self.post("/api/v1/app/auth/login", {"email": email, "password": password, "device": DEVICE}, **headers)

    def signed_in(self, user):
        response = self.login(user.email)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["status"], "ok")
        return response.json()

    def get(self, path, token, tenant=None, **params):
        headers = {"HTTP_AUTHORIZATION": "Bearer " + token}
        if tenant is not None:
            headers["HTTP_X_TENANT_ID"] = str(tenant.id if hasattr(tenant, "id") else tenant)
        return self.client.get(path, params, **headers)

    def call(self, verb, path, token, body=None, tenant=None):
        headers = {"HTTP_AUTHORIZATION": "Bearer " + token}
        if tenant is not None:
            headers["HTTP_X_TENANT_ID"] = str(tenant.id)
        return self.client.generic(verb, path, data=json.dumps(body) if body is not None else "",
                                   content_type="application/json", **headers)

    def assertCode(self, response, status, code):
        self.assertEqual(response.status_code, status, response.content)
        body = response.json()
        self.assertEqual(body["code"], code, body)
        self.assertTrue(body["error"])
        return body

    def enable_mfa(self, user):
        secret = pyotp.random_base32()
        PlatformMFA.objects.create(user=user, secret_encrypted=encrypt_secret(secret), enabled=True,
                                   recovery_codes=[make_password("PDV2-ABCD-EFGH")])
        return secret

    def platform_tokens(self, user):
        secret = self.enable_mfa(user)
        first = self.login(user.email).json()
        self.assertEqual(first["status"], "mfa_required")
        response = self.post("/api/v1/app/auth/mfa", {"mfa_token": first["mfa_token"],
                                                       "code": pyotp.TOTP(secret).now()})
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()

    def sale(self, *, shop=None, total=1000, created_at=None, status="completed", payments=(("cash", 1000),),
             change=0, terminal=None, session_id="", customer=None):
        shop = shop or self.centro
        row = Sale.objects.create(id=uuid.uuid4(), tenant=shop.tenant, shop=shop,
                                  terminal=terminal or (self.terminal if shop == self.centro else self.terminal_bairro),
                                  request_id=uuid.uuid4().hex, content_hash="d" * 64, gross_cents=total,
                                  total_cents=total, change_cents=change, status=status,
                                  created_at=created_at or timezone.now(), cash_session_id=session_id,
                                  customer=customer, operator_name_snapshot="Carla")
        for number, (method_name, amount) in enumerate(payments):
            SalePayment.objects.create(sale=row, line_number=number, method=method_name, amount_cents=amount)
        return row


class AppLoginTests(AppBase):
    def test_login_without_access_is_denied_and_creates_no_device(self):
        body = self.assertCode(self.login(self.owner.email), 403, "app_access_denied")
        self.assertIn("administrador do PDV2", body["error"])
        self.assertEqual(AppDevice.objects.count(), 0)
        self.assertTrue(AuditEvent.objects.filter(action="app.login_denied", actor=self.owner).exists())
        self.assertCode(self.login(self.owner.email, "errada"), 401, "invalid_credentials")

    def test_role_does_not_decide_owner_denied_manager_with_access_enters(self):
        self.grant(self.manager, self.tenant)
        self.assertCode(self.login(self.owner.email), 403, "app_access_denied")
        data = self.signed_in(self.manager)
        self.assertEqual(set(data["tokens"]), TOKEN_KEYS)
        self.assertEqual(data["tokens"]["access_expires_in"], 900)
        self.assertTrue(data["tokens"]["access_token"].startswith("pa_"))
        self.assertTrue(data["tokens"]["refresh_token"].startswith("pr_"))
        self.assertEqual(AppDevice.objects.get().activated_at is not None, True)
        self.assertTrue(AuditEvent.objects.filter(action="app.login", actor=self.manager).exists())

    def test_tenant_login_me_and_dashboard_with_bearer(self):
        self.grant(self.owner, self.tenant, shops=[self.centro], permissions=["sales", "cash"])
        data = self.signed_in(self.owner)
        token = data["tokens"]["access_token"]
        me = data["me"]
        self.assertEqual(set(me), ME_KEYS)
        self.assertEqual(set(me["device"]), DEVICE_KEYS)
        self.assertTrue(me["device"]["current"])
        self.assertIsNone(me["platform"])
        self.assertEqual(set(me["links"]), LINK_KEYS)
        self.assertEqual(me["links"]["privacy_url"], "https://pdv2.crowntech.com.br/privacidade/")
        self.assertEqual(len(me["tenants"]), 1)
        self.assertEqual(set(me["tenants"][0]), TENANT_ACCESS_KEYS)
        self.assertFalse(me["tenants"][0]["all_shops"])
        self.assertEqual(me["tenants"][0]["shops"], [{"id": str(self.centro.id), "name": "Loja Centro"}])
        self.assertEqual(me["tenants"][0]["permissions"], ["sales", "cash"])
        again = self.get("/api/v1/app/me", token)
        self.assertEqual(again.status_code, 200, again.content)
        self.assertEqual(again["Cache-Control"], "private, no-store")
        dashboard = self.get("/api/v1/app/dashboard", token, self.tenant)
        self.assertEqual(dashboard.status_code, 200, dashboard.content)
        self.assertEqual(set(dashboard.json()), DASHBOARD_KEYS)
        self.assertCode(self.get("/api/v1/app/dashboard", token), 400, "tenant_required")
        self.assertCode(self.get("/api/v1/app/dashboard", token, self.tenant, period="ano"), 400, "invalid_period")
        tenants = self.get("/api/v1/app/tenants", token).json()
        self.assertEqual([row["id"] for row in tenants["results"]], [str(self.tenant.id)])
        self.assertIsNone(tenants["next_cursor"])

    def test_superuser_needs_mfa_setup_then_totp_and_recovery_code(self):
        self.assertCode(self.login(self.root.email), 403, "mfa_setup_required")
        self.assertEqual(AppDevice.objects.count(), 0)
        secret = self.enable_mfa(self.root)
        first = self.login(self.root.email).json()
        self.assertEqual(set(first), {"status", "mfa_token", "mfa_expires_at", "user"})
        self.assertEqual(first["status"], "mfa_required")
        self.assertTrue(first["mfa_token"].startswith("pm_"))
        self.assertRegex(first["mfa_expires_at"], LOCAL_ISO)
        wrong = "000000" if pyotp.TOTP(secret).now() != "000000" else "111111"
        self.assertCode(self.post("/api/v1/app/auth/mfa", {"mfa_token": first["mfa_token"], "code": wrong}),
                        401, "mfa_invalid")
        self.assertCode(self.post("/api/v1/app/auth/mfa", {"mfa_token": "pm_" + "x" * 30, "code": "123456"}),
                        401, "mfa_expired")
        ok = self.post("/api/v1/app/auth/mfa", {"mfa_token": first["mfa_token"], "code": pyotp.TOTP(secret).now()})
        self.assertEqual(ok.status_code, 200, ok.content)
        data = ok.json()
        self.assertEqual(data["status"], "ok")
        self.assertEqual(set(data["me"]["platform"]), PLATFORM_ME_KEYS)
        self.assertTrue(data["me"]["platform"]["enabled"])
        self.assertTrue(data["me"]["platform"]["can_manage_access"])
        self.assertEqual(data["me"]["tenants"], [])
        device = AppDevice.objects.get(pk=data["me"]["device"]["id"])
        self.assertIsNotNone(device.mfa_verified_at)
        self.assertIsNone(device.mfa_challenge_hash)
        # Refresh of a platform device lasts 30 days.
        refresh_at = datetime.fromisoformat(data["tokens"]["refresh_expires_at"])
        self.assertLess(refresh_at - timezone.now(), timedelta(days=31))
        self.assertTrue(AuditEvent.objects.filter(action="app.mfa_verified", actor=self.root).exists())
        # The challenge is single-use.
        self.assertCode(self.post("/api/v1/app/auth/mfa", {"mfa_token": first["mfa_token"],
                                                           "code": pyotp.TOTP(secret).now()}), 401, "mfa_expired")
        second = self.login(self.root.email).json()
        recovered = self.post("/api/v1/app/auth/mfa", {"mfa_token": second["mfa_token"],
                                                        "recovery_code": "pdv2-abcd-efgh"})
        self.assertEqual(recovered.status_code, 200, recovered.content)
        self.assertEqual(PlatformMFA.objects.get(user=self.root).recovery_codes, [])
        self.assertCode(self.post("/api/v1/app/auth/mfa", {"mfa_token": second["mfa_token"], "code": "1",
                                                           "recovery_code": "x"}), 400, "invalid_request")

    def test_platform_staff_needs_platform_access_then_mfa(self):
        self.enable_mfa(self.support)
        self.assertCode(self.login(self.support.email), 403, "app_access_denied")
        self.grant(self.support, scope="platform")
        self.assertEqual(self.login(self.support.email).json()["status"], "mfa_required")

    @override_settings(PLATFORM_MFA_REQUIRED=False)
    def test_platform_scope_requires_totp_even_when_web_mfa_is_off(self):
        self.assertCode(self.login(self.root.email), 403, "mfa_setup_required")

    def test_sixth_login_attempt_is_rate_limited_with_retry_after(self):
        for _ in range(5):
            self.assertCode(self.login(self.owner.email, "errada"), 401, "invalid_credentials")
        body = self.assertCode(self.login(self.owner.email, "errada"), 429, "rate_limited")
        self.assertGreater(body["retry_after_seconds"], 0)
        self.assertLessEqual(body["retry_after_seconds"], 900)

    def test_login_validates_shape(self):
        bad = dict(DEVICE, platform="windows")
        self.assertCode(self.post("/api/v1/app/auth/login", {"email": "a@b.c", "password": "x", "device": bad}),
                        400, "invalid_request")
        self.assertCode(self.post("/api/v1/app/auth/login", {"email": "a@b.c", "password": "x", "device": DEVICE,
                                                             "extra": 1}), 400, "invalid_request")
        self.assertCode(self.client.get("/api/v1/app/auth/login"), 405, "method_not_allowed")

    def test_login_without_csrf_token_succeeds_with_enforced_csrf(self):
        self.grant(self.owner, self.tenant)
        client = Client(enforce_csrf_checks=True)
        response = self.post("/api/v1/app/auth/login", {"email": self.owner.email, "password": PASSWORD,
                                                        "device": DEVICE}, client=client)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertNotIn("sessionid", response.cookies)

    def test_device_limit_disconnects_the_oldest(self):
        self.grant(self.owner, self.tenant)
        for _ in range(10):
            self.signed_in(self.owner)
        first = AppDevice.objects.order_by("created_at").first()
        AppDevice.objects.filter(pk=first.pk).update(last_seen_at=timezone.now() - timedelta(days=5))
        self.signed_in(self.owner)
        self.assertEqual(AppDevice.objects.filter(user=self.owner, revoked_at__isnull=True).count(), 10)
        self.assertEqual(AppDevice.objects.get(pk=first.pk).revoke_reason, "limit")


class AppTokenTests(AppBase):
    def setUp(self):
        self.access = self.grant(self.owner, self.tenant)
        self.data = self.signed_in(self.owner)
        self.tokens = self.data["tokens"]
        self.device = AppDevice.objects.get(user=self.owner)

    def refresh(self, value):
        return self.post("/api/v1/app/auth/refresh", {"refresh_token": value})

    def test_expired_access_then_refresh_rotation_and_reuse_detection(self):
        AppDevice.objects.filter(pk=self.device.pk).update(access_expires_at=timezone.now() - timedelta(seconds=1))
        self.assertCode(self.get("/api/v1/app/me", self.tokens["access_token"]), 401, "token_expired")
        old_refresh = self.tokens["refresh_token"]
        first = self.refresh(old_refresh)
        self.assertEqual(first.status_code, 200, first.content)
        pair = first.json()["tokens"]
        self.assertEqual(set(pair), TOKEN_KEYS)
        self.assertNotEqual(pair["refresh_token"], old_refresh)
        self.assertEqual(self.get("/api/v1/app/me", pair["access_token"]).status_code, 200)
        # Lost answer: the old refresh again within 30 s still gets a new pair.
        again = self.refresh(old_refresh)
        self.assertEqual(again.status_code, 200, again.content)
        newest = again.json()["tokens"]
        # After 30 s the old refresh is a stolen token: the device is revoked.
        AppDevice.objects.filter(pk=self.device.pk).update(refresh_rotated_at=timezone.now() - timedelta(seconds=31))
        self.assertCode(self.refresh(old_refresh), 401, "device_revoked")
        self.device.refresh_from_db()
        self.assertEqual(self.device.revoke_reason, "refresh_reuse")
        self.assertCode(self.get("/api/v1/app/me", newest["access_token"]), 401, "device_revoked")
        self.assertCode(self.refresh(newest["refresh_token"]), 401, "device_revoked")
        self.assertTrue(AuditEvent.objects.filter(action="app.refresh_reuse_detected").exists())

    def test_unknown_and_expired_refresh(self):
        self.assertCode(self.refresh("pr_" + "z" * 40), 401, "refresh_invalid")
        self.assertCode(self.refresh("nada"), 401, "refresh_invalid")
        AppDevice.objects.filter(pk=self.device.pk).update(refresh_expires_at=timezone.now() - timedelta(seconds=1))
        self.assertCode(self.refresh(self.tokens["refresh_token"]), 401, "refresh_invalid")

    def test_refresh_without_any_access_revokes_with_403(self):
        AppAccess.objects.filter(pk=self.access.pk).update(revoked_at=timezone.now())
        self.assertCode(self.refresh(self.tokens["refresh_token"]), 403, "app_access_denied")
        self.device.refresh_from_db()
        self.assertEqual(self.device.revoke_reason, "access_ended")

    def test_bearer_shape_and_session_never_count(self):
        self.assertCode(self.client.get("/api/v1/app/me"), 401, "unauthenticated")
        self.assertCode(self.client.get("/api/v1/app/me", HTTP_AUTHORIZATION="Bearer xx"), 401, "unauthenticated")
        self.assertCode(self.get("/api/v1/app/me", "pa_" + "q" * 40), 401, "unauthenticated")
        # A logged-in web session is not accepted by the app routes...
        self.client.force_login(self.owner)
        self.assertCode(self.client.get("/api/v1/app/me"), 401, "unauthenticated")
        self.client.logout()
        # ...and the app token is not accepted by the panel routes.
        response = self.client.get("/api/v1/sales", HTTP_AUTHORIZATION="Bearer " + self.tokens["access_token"],
                                   HTTP_X_TENANT_ID=str(self.tenant.id))
        self.assertEqual(response.status_code, 401)

    def test_logout_is_idempotent_and_revokes(self):
        response = self.call("POST", "/api/v1/app/auth/logout", self.tokens["access_token"], {})
        self.assertEqual(response.json(), {"ok": True})
        self.device.refresh_from_db()
        self.assertEqual(self.device.revoke_reason, "logout")
        self.assertEqual(self.call("POST", "/api/v1/app/auth/logout", self.tokens["access_token"]).json(), {"ok": True})
        self.assertEqual(self.post("/api/v1/app/auth/logout", {}).json(), {"ok": True})
        self.assertCode(self.get("/api/v1/app/me", self.tokens["access_token"]), 401, "device_revoked")
        self.assertEqual(AuditEvent.objects.filter(action="app.logout").count(), 1)

    def test_logout_with_refresh_in_body(self):
        AppDevice.objects.filter(pk=self.device.pk).update(access_expires_at=timezone.now() - timedelta(hours=1))
        self.assertEqual(self.post("/api/v1/app/auth/logout", {"refresh_token": self.tokens["refresh_token"]}).json(),
                         {"ok": True})
        self.assertIsNotNone(AppDevice.objects.get(pk=self.device.pk).revoked_at)

    def test_inactive_user_is_disconnected(self):
        type(self.owner).objects.filter(pk=self.owner.pk).update(is_active=False)
        self.assertCode(self.get("/api/v1/app/me", self.tokens["access_token"]), 401, "device_revoked")

    def test_membership_ended_loses_the_app_at_once(self):
        Membership.objects.filter(user=self.owner, tenant=self.tenant).update(active=False)
        self.assertCode(self.get("/api/v1/app/me", self.tokens["access_token"]), 403, "app_access_denied")
        self.assertCode(self.get("/api/v1/app/me", self.tokens["access_token"]), 401, "device_revoked")

    def test_devices_list_and_delete(self):
        other = self.signed_in(self.owner)["tokens"]
        rows = self.get("/api/v1/app/devices", self.tokens["access_token"]).json()
        self.assertEqual(len(rows["results"]), 2)
        self.assertEqual(set(rows["results"][0]), DEVICE_KEYS)
        self.assertEqual(sum(row["current"] for row in rows["results"]), 1)
        target = next(row for row in rows["results"] if not row["current"])
        foreign = self.grant(self.outsider, self.other_tenant)
        outsider = self.signed_in(self.outsider)["tokens"]
        self.assertCode(self.call("DELETE", f"/api/v1/app/devices/{target['id']}", outsider["access_token"]),
                        404, "not_found")
        self.assertEqual(self.call("DELETE", f"/api/v1/app/devices/{target['id']}",
                                   self.tokens["access_token"]).json(), {"ok": True})
        self.assertCode(self.get("/api/v1/app/me", other["access_token"]), 401, "device_revoked")
        self.assertTrue(AuditEvent.objects.filter(action="app.device_revoked").exists())
        self.assertIsNotNone(foreign)

    def test_account_deletion_request_signs_out_everywhere(self):
        other = self.signed_in(self.owner)["tokens"]
        response = self.call("POST", "/api/v1/app/account/deletion-request", self.tokens["access_token"],
                             {"note": "Não uso mais"})
        self.assertEqual(response.status_code, 202, response.content)
        self.assertEqual(response.json()["status"], "received")
        self.assertIn("15 dias", response.json()["message"])
        self.assertEqual(AppDevice.objects.filter(user=self.owner, revoked_at__isnull=True).count(), 0)
        self.assertCode(self.get("/api/v1/app/me", other["access_token"]), 401, "device_revoked")
        request = AccountDeletionRequest.objects.get()
        self.assertEqual((request.source, request.status, request.note), ("app", "pending", "Não uso mais"))
        self.assertTrue(AuditEvent.objects.filter(action="app.account_deletion_requested").exists())


class AppScopeTests(AppBase):
    def token(self, user, **grant):
        self.grant(user, self.tenant, **grant)
        return self.signed_in(user)["tokens"]["access_token"]

    def test_tenant_and_shop_isolation(self):
        token = self.token(self.manager, shops=[self.centro])
        self.assertCode(self.get("/api/v1/app/dashboard", token, self.other_tenant), 403, "tenant_forbidden")
        self.assertCode(self.get("/api/v1/app/dashboard", token, self.tenant, shop_id=str(self.bairro.id)),
                        403, "shop_forbidden")
        self.assertCode(self.get("/api/v1/app/dashboard", token, self.tenant, shop_id=str(self.foreign_shop.id)),
                        403, "shop_forbidden")
        self.assertCode(self.get("/api/v1/app/dashboard", token, "nao-e-uuid"), 400, "invalid_request")
        mine = self.sale(shop=self.centro, total=1000)
        hidden = self.sale(shop=self.bairro, total=5000, payments=(("pix", 5000),))
        foreign = Sale.objects.create(id=uuid.uuid4(), tenant=self.other_tenant, shop=self.foreign_shop,
                                      terminal=self.foreign_terminal, request_id="x", content_hash="e" * 64,
                                      gross_cents=9000, total_cents=9000, created_at=timezone.now())
        self.assertEqual(self.get(f"/api/v1/app/sales/{mine.id}", token, self.tenant).status_code, 200)
        self.assertCode(self.get(f"/api/v1/app/sales/{hidden.id}", token, self.tenant), 404, "not_found")
        self.assertCode(self.get(f"/api/v1/app/sales/{foreign.id}", token, self.tenant), 404, "not_found")
        data = self.get("/api/v1/app/dashboard", token, self.tenant).json()
        self.assertEqual(data["sales"]["revenue_cents"], 1000)
        self.assertEqual(data["shops"], [{"id": str(self.centro.id), "name": "Loja Centro"}])
        self.assertEqual(data["terminals"]["total"], 1)
        listed = self.get("/api/v1/app/sales", token, self.tenant).json()["results"]
        self.assertEqual([row["id"] for row in listed], [str(mine.id)])

    def test_all_shops_access_sees_every_shop_and_can_pick_one(self):
        token = self.token(self.manager)
        self.sale(shop=self.centro, total=1000)
        self.sale(shop=self.bairro, total=5000, payments=(("pix", 5000),))
        self.assertEqual(self.get("/api/v1/app/dashboard", token, self.tenant).json()["sales"]["revenue_cents"], 6000)
        data = self.get("/api/v1/app/dashboard", token, self.tenant, shop_id=str(self.bairro.id)).json()
        self.assertEqual(data["sales"]["revenue_cents"], 5000)
        self.assertEqual(data["shop"], {"id": str(self.bairro.id), "name": "Loja Bairro"})

    def test_permission_missing_hides_section_and_route(self):
        token = self.token(self.manager, permissions=["sales"])
        data = self.get("/api/v1/app/dashboard", token, self.tenant).json()
        self.assertIsNone(data["cash"])
        self.assertIsNone(data["stock"])
        self.assertIsNotNone(data["sales"])
        self.assertEqual(data["permissions"], ["sales"])
        for path in ("/api/v1/app/cash-sessions", "/api/v1/app/stock/alerts", "/api/v1/app/credit",
                     "/api/v1/app/terminals", "/api/v1/app/conflicts"):
            self.assertCode(self.get(path, token, self.tenant), 403, "permission_denied")

    def test_revoking_one_company_blocks_it_at_once_and_last_one_disconnects(self):
        second = Tenant.objects.create(name="Segunda Loja")
        Membership.objects.create(tenant=second, user=self.manager, name="Gerente", role="viewer")
        first_access = self.grant(self.manager, self.tenant)
        second_access = self.grant(self.manager, second)
        token = self.signed_in(self.manager)["tokens"]["access_token"]
        platform = self.platform_tokens(self.root)["tokens"]["access_token"]
        response = self.call("POST", f"/api/v1/app/platform/access/{first_access.id}/revoke", platform,
                             {"reason": "Saiu da sociedade"})
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["status"], "revoked")
        self.assertCode(self.get("/api/v1/app/dashboard", token, self.tenant), 403, "tenant_forbidden")
        self.assertEqual(self.get("/api/v1/app/dashboard", token, second).status_code, 200)
        self.call("POST", f"/api/v1/app/platform/access/{second_access.id}/revoke", platform, {"reason": "Fim"})
        self.assertCode(self.get("/api/v1/app/me", token), 401, "device_revoked")

    def test_expired_access_does_not_count(self):
        self.grant(self.manager, self.tenant, expires_at=timezone.now() - timedelta(minutes=1))
        self.assertCode(self.login(self.manager.email), 403, "app_access_denied")

    def test_platform_principal_reads_any_company_and_is_audited_once(self):
        token = self.platform_tokens(self.root)["tokens"]["access_token"]
        self.assertEqual(self.get("/api/v1/app/dashboard", token, self.other_tenant).status_code, 200)
        self.assertEqual(self.get("/api/v1/app/dashboard", token, self.other_tenant).status_code, 200)
        self.assertEqual(AuditEvent.objects.filter(action="app.platform_tenant_viewed",
                                                   tenant=self.other_tenant).count(), 1)
        tenants = self.get("/api/v1/app/tenants", token, q="Outra").json()["results"]
        self.assertEqual([row["name"] for row in tenants], ["Outra Empresa"])
        self.assertTrue(tenants[0]["all_shops"])

    def test_pagination_cursor_and_limits(self):
        token = self.token(self.manager)
        for _ in range(3):
            self.sale()
        first = self.get("/api/v1/app/sales", token, self.tenant, limit=2).json()
        self.assertEqual(len(first["results"]), 2)
        self.assertEqual(first["next_cursor"], "2")
        second = self.get("/api/v1/app/sales", token, self.tenant, limit=2, cursor=first["next_cursor"]).json()
        self.assertEqual(len(second["results"]), 1)
        self.assertIsNone(second["next_cursor"])
        self.assertEqual(len({row["id"] for row in first["results"] + second["results"]}), 3)
        for params in ({"limit": 0}, {"limit": 101}, {"cursor": "-1"}, {"limit": "x"}):
            self.assertCode(self.get("/api/v1/app/sales", token, self.tenant, **params), 400, "invalid_request")
        self.assertCode(self.get("/api/v1/app/sales", token, self.tenant, status="x"), 400, "invalid_request")


class AppNumbersTests(AppBase):
    def setUp(self):
        self.grant(self.owner, self.tenant)
        self.token = self.signed_in(self.owner)["tokens"]["access_token"]

    def dashboard(self, **params):
        response = self.get("/api/v1/app/dashboard", self.token, self.tenant, **params)
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()

    def test_summary_numbers_match_real_sales(self):
        today = timezone.localdate()
        midnight = local_midnight(today)
        customer = Customer.objects.create(tenant=self.tenant, name="João da Silva", phone="21999990000")
        # Cash 1500 for a 1000 sale: 500 of change.
        self.sale(total=1000, payments=(("cash", 1500),), change=500)
        # Fiado sale: not money received.
        fiado = self.sale(total=3000, payments=(("credit_account", 3000),), customer=customer)
        FinanceEntry.objects.create(tenant=self.tenant, shop=self.centro, customer=customer, kind="receivable",
                                    description="Fiado", amount_cents=3000, due_date=today - timedelta(days=3),
                                    sale=fiado)
        # Pix + store credit.
        self.sale(total=2000, payments=(("pix", 1000), ("store_credit", 1000)))
        # Legacy card and company-defined methods count as "other".
        self.sale(total=800, payments=(("card", 500), ("other", 300)))
        # Cancelled sale of today.
        self.sale(total=700, status="cancelled")
        # Made yesterday, received today: belongs to yesterday.
        late = self.sale(total=4000, created_at=midnight - timedelta(hours=2), payments=(("pix", 4000),))
        self.assertGreaterEqual(late.received_at, midnight)
        SaleReturn.objects.create(id=uuid.uuid4(), tenant=self.tenant, shop=self.centro, sale=late,
                                  refund_method="cash", total_cents=250, reason="Defeito", content_hash="f" * 64,
                                  created_at=timezone.now())
        CreditReceipt.objects.create(id=uuid.uuid4(), tenant=self.tenant, shop=self.centro, terminal=self.terminal,
                                     customer=customer, method="cash", amount_cents=1200, created_at=timezone.now())
        data = self.dashboard(period="today")
        sales, pay = data["sales"], data["payments"]
        self.assertEqual(set(sales), SALES_KEYS)
        self.assertEqual(set(pay), PAYMENTS_KEYS)
        self.assertEqual(sales["count"], 4)
        self.assertEqual(sales["revenue_cents"], 6800)
        self.assertEqual(sales["average_ticket_cents"], 1700)
        self.assertEqual((sales["cancelled_count"], sales["cancelled_cents"]), (1, 700))
        self.assertEqual(sales["returns_cents"], 250)
        self.assertEqual(sales["net_revenue_cents"], 6550)
        self.assertEqual(pay["cash_cents"], 1000)
        self.assertEqual(pay["pix_cents"], 1000)
        self.assertEqual(pay["other_cents"], 800)
        self.assertEqual(pay["credit_account_cents"], 3000)
        self.assertEqual(pay["store_credit_cents"], 1000)
        self.assertEqual(pay["received_total_cents"], 2800)
        self.assertEqual(pay["credit_received_cents"], 1200)
        self.assertEqual(sales["revenue_cents"],
                         pay["received_total_cents"] + pay["credit_account_cents"] + pay["store_credit_cents"])
        series = data["series"]
        self.assertEqual(series["kind"], "hour")
        self.assertEqual(len(series["points"]), 24)
        self.assertEqual(series["points"][0], {"key": "00", "label": "00h", "count": series["points"][0]["count"],
                                               "revenue_cents": series["points"][0]["revenue_cents"]})
        self.assertEqual(sum(point["revenue_cents"] for point in series["points"]), 6800)
        yesterday = self.dashboard(period="yesterday")
        self.assertEqual(yesterday["sales"]["count"], 1)
        self.assertEqual(yesterday["sales"]["revenue_cents"], 4000)
        self.assertEqual(yesterday["period"]["date_from"], (today - timedelta(days=1)).isoformat())
        week = self.dashboard(period="7d")
        self.assertEqual(week["series"]["kind"], "day")
        self.assertEqual(len(week["series"]["points"]), 7)
        self.assertEqual(week["series"]["points"][-1]["key"], today.isoformat())
        self.assertEqual(week["series"]["points"][-1]["label"], today.strftime("%d/%m"))
        self.assertEqual(week["sales"]["revenue_cents"], 10800)
        self.assertEqual(week["period"]["to"], (local_midnight(today + timedelta(days=1))).isoformat())
        credit = data["credit"]
        self.assertEqual(set(credit), CREDIT_KEYS)
        self.assertEqual(credit, {"open_cents": 3000, "overdue_cents": 3000, "overdue_customers": 1})
        month = self.dashboard(period="month")
        self.assertEqual(month["period"]["date_from"], today.replace(day=1).isoformat())
        self.assertEqual(len(month["series"]["points"]), today.day)

    def test_dates_are_local_without_fraction_and_period_object(self):
        data = self.dashboard()
        self.assertRegex(data["generated_at"], LOCAL_ISO)
        self.assertEqual(set(data["period"]), PERIOD_KEYS)
        self.assertEqual((data["period"]["key"], data["period"]["label"]), ("today", "Hoje"))
        self.assertRegex(data["period"]["from"], r"T00:00:00-03:00$")
        self.assertEqual(data["period"]["date_from"], data["period"]["date_to"])
        self.assertEqual(set(data["cash"]), CASH_KEYS)
        self.assertEqual(set(data["stock"]), STOCK_KEYS)
        self.assertEqual(set(data["terminals"]), TERMINALS_KEYS)

    def test_terminals_stock_and_cash_sections(self):
        Terminal.objects.create(tenant=self.tenant, shop=self.centro, name="Revogado", revoked=True,
                                token_hash="9" * 64, last_seen_at=timezone.now())
        Terminal.objects.create(tenant=self.tenant, shop=self.centro, name="Novo")
        SyncConflict.objects.create(tenant=self.tenant, shop=self.centro, terminal=self.terminal, event_id="e1",
                                    event_type="sale.completed", aggregate_id="a", reason="negative_stock")
        other = Product.objects.create(tenant=self.tenant, sku="ARROZ", name="Arroz", stock_minimum_milli=5000)
        StockBalance.objects.create(tenant=self.tenant, shop=self.centro, product=other, quantity_milli=2000)
        StockBalance.objects.create(tenant=self.tenant, shop=self.bairro, product=other, quantity_milli=-1000)
        Product.objects.create(tenant=self.tenant, sku="SEMCTRL", name="Pão", stock_minimum_milli=5000,
                               stock_controlled=False)
        session = CashSession.objects.create(id=uuid.uuid4(), tenant=self.tenant, shop=self.centro,
                                             terminal=self.terminal, opening_cents=20000,
                                             opened_at=timezone.now() - timedelta(hours=3))
        closed = CashSession.objects.create(id=uuid.uuid4(), tenant=self.tenant, shop=self.centro,
                                            terminal=self.terminal, opened_at=timezone.now() - timedelta(hours=5),
                                            closed_at=timezone.now() - timedelta(minutes=5), difference_cents=-1500,
                                            expected_cents=10000, counted_cents=8500)
        self.sale(total=1000, payments=(("cash", 2000),), change=1000, session_id=str(session.id))
        self.sale(total=3000, payments=(("pix", 3000),), session_id=session.id.hex)
        CashMovement.objects.create(id=uuid.uuid4(), session=session, kind="withdrawn", amount_cents=400,
                                    reason="Depósito no banco", created_at=timezone.now())
        data = self.dashboard()
        self.assertEqual(data["terminals"], {"total": 2, "online": 1, "offline": 1, "unpaired": 1,
                                             "online_window_seconds": 300, "conflicts_open": 1})
        # Leite: no balance row in both shops (zeroed x2); Arroz: below in Centro, negative in Bairro.
        self.assertEqual(data["stock"], {"zeroed_count": 3, "below_minimum_count": 1})
        self.assertEqual(data["cash"], {"open_count": 1, "closed_count": 1, "shortage_count": 1,
                                        "shortage_cents": -1500})
        alerts = self.get("/api/v1/app/stock/alerts", self.token, self.tenant).json()
        self.assertEqual(alerts["counts"], {"zeroed": 3, "below_minimum": 1})
        self.assertEqual([row["status"] for row in alerts["results"]], ["zeroed", "zeroed", "zeroed", "below_minimum"])
        self.assertEqual(set(alerts["results"][0]), STOCK_ROW_KEYS)
        below = self.get("/api/v1/app/stock/alerts", self.token, self.tenant, status="below_minimum").json()
        self.assertEqual([(row["name"], row["quantity_milli"]) for row in below["results"]], [("Arroz", 2000)])
        sessions = self.get("/api/v1/app/cash-sessions", self.token, self.tenant).json()["results"]
        self.assertEqual(len(sessions), 1)
        self.assertEqual(set(sessions[0]), SESSION_KEYS)
        self.assertEqual((sessions[0]["sales_count"], sessions[0]["sales_total_cents"],
                          sessions[0]["cash_net_cents"], sessions[0]["withdrawn_cents"]), (2, 4000, 1000, 400))
        closed_rows = self.get("/api/v1/app/cash-sessions", self.token, self.tenant, status="closed").json()["results"]
        self.assertEqual([row["difference_cents"] for row in closed_rows], [-1500])
        detail = self.get(f"/api/v1/app/cash-sessions/{session.id}", self.token, self.tenant).json()
        self.assertEqual(set(detail), SESSION_KEYS | SESSION_DETAIL_EXTRA)
        self.assertEqual(detail["by_method"], [{"method": "cash", "label": "Dinheiro", "amount_cents": 1000},
                                               {"method": "pix", "label": "Pix", "amount_cents": 3000}])
        self.assertEqual(set(detail["movements"][0]), MOVEMENT_KEYS)
        self.assertEqual(detail["movements"][0]["label"], "Sangria")
        self.assertCode(self.get(f"/api/v1/app/cash-sessions/{closed.id}", self.token, self.other_tenant),
                        403, "tenant_forbidden")
        terminals = self.get("/api/v1/app/terminals", self.token, self.tenant).json()
        self.assertEqual({row["name"]: row["status"] for row in terminals["results"]},
                         {"Caixa 1": "online", "Caixa B": "offline", "Novo": "unpaired"})
        self.assertEqual(set(terminals["results"][0]), TERMINAL_ROW_KEYS)
        self.assertIsNone(terminals["next_cursor"])
        conflicts = self.get("/api/v1/app/conflicts", self.token, self.tenant).json()["results"]
        self.assertEqual(set(conflicts[0]), CONFLICT_ROW_KEYS)
        self.assertEqual((conflicts[0]["reason_text"], conflicts[0]["status_text"]), ("Estoque negativo", "Em aberto"))

    def test_sale_list_and_detail_shape(self):
        row = self.sale(total=4590, payments=(("pix", 4590),))
        SaleItem.objects.create(sale=row, line_number=1, product=self.product, name="Leite", unit="UN",
                                quantity_milli=1000, unit_price_cents=4590, gross_cents=4590, discount_cents=0,
                                line_total_cents=4590)
        self.sale(total=100, status="refunded")
        listed = self.get("/api/v1/app/sales", self.token, self.tenant, status="completed").json()["results"]
        self.assertEqual(len(listed), 1)
        self.assertEqual(set(listed[0]), SALE_ROW_KEYS)
        self.assertEqual((listed[0]["items_count"], listed[0]["payment_methods"]), (1, ["pix"]))
        self.assertEqual(listed[0]["terminal_name"], "Caixa 1")
        cancelled = self.get("/api/v1/app/sales", self.token, self.tenant, status="cancelled").json()["results"]
        self.assertEqual([item["status"] for item in cancelled], ["refunded"])
        detail = self.get(f"/api/v1/app/sales/{row.id}", self.token, self.tenant).json()
        self.assertEqual(set(detail), SALE_DETAIL_KEYS)
        self.assertEqual(set(detail["items"][0]), ITEM_KEYS)
        self.assertEqual(detail["payments"], [{"method": "pix", "label": "Pix", "amount_cents": 4590}])
        self.assertRegex(detail["created_at"], LOCAL_ISO)
        SaleReturn.objects.create(id=uuid.uuid4(), tenant=self.tenant, shop=self.centro, sale=row,
                                  refund_method="account", total_cents=90, reason="Troca", content_hash="1" * 64,
                                  created_at=timezone.now())
        detail = self.get(f"/api/v1/app/sales/{row.id}", self.token, self.tenant).json()
        self.assertEqual(set(detail["returns"][0]), RETURN_KEYS)
        self.assertEqual(detail["returns"][0]["refund_label"], "Abatido no fiado")

    def test_credit_list_by_customer(self):
        today = timezone.localdate()
        ana = Customer.objects.create(tenant=self.tenant, name="Ana", phone="1")
        bia = Customer.objects.create(tenant=self.tenant, name="Bia", phone="2")
        for customer, amount, due in ((ana, 1000, today + timedelta(days=5)), (bia, 500, today - timedelta(days=1)),
                                      (bia, 700, today + timedelta(days=2))):
            sale = self.sale(total=amount, payments=(("credit_account", amount),), customer=customer)
            FinanceEntry.objects.create(tenant=self.tenant, shop=self.centro, customer=customer, kind="receivable",
                                        description="Fiado", amount_cents=amount, due_date=due, sale=sale)
        data = self.get("/api/v1/app/credit", self.token, self.tenant).json()
        self.assertEqual(set(data["totals"]), CREDIT_TOTAL_KEYS)
        self.assertEqual(data["totals"], {"open_cents": 2200, "overdue_cents": 500, "customers": 2,
                                          "overdue_customers": 1})
        self.assertEqual([row["name"] for row in data["results"]], ["Bia", "Ana"])
        self.assertEqual(set(data["results"][0]), CREDIT_ROW_KEYS)
        self.assertEqual(data["results"][0]["oldest_due_date"], (today - timedelta(days=1)).isoformat())
        overdue = self.get("/api/v1/app/credit", self.token, self.tenant, status="overdue").json()["results"]
        self.assertEqual([row["name"] for row in overdue], ["Bia"])


class OwnerMonitoringNumbersTests(AppBase):
    def test_monitoring_uses_created_at_change_and_leaves_fiado_out(self):
        midnight = local_midnight(timezone.localdate())
        self.sale(total=1000, payments=(("cash", 1500),), change=500)
        self.sale(total=3000, payments=(("credit_account", 3000),))
        self.sale(total=2000, payments=(("store_credit", 500), ("card_debit", 1500)))
        self.sale(total=9000, created_at=midnight - timedelta(hours=1), payments=(("pix", 9000),))
        Terminal.objects.create(tenant=self.tenant, shop=self.centro, name="Revogado", revoked=True)
        self.client.force_login(self.owner)
        data = self.client.get("/api/v1/owner/monitoring", HTTP_X_TENANT_ID=str(self.tenant.id)).json()
        self.assertEqual(data["sales"]["count"], 3)
        self.assertEqual(data["sales"]["revenue_cents"], 6000)
        self.assertEqual(data["payments_cents"], {"cash": 1000, "pix": 0, "card_credit": 0, "card_debit": 1500,
                                                  "other": 0})
        self.assertEqual(data["terminals_total"], 2)
        self.assertEqual(len(data["terminals"]), 2)
        self.assertNotIn("Revogado", [row["name"] for row in data["terminals"]])


class AppPlatformTests(AppBase):
    def setUp(self):
        self.platform = self.platform_tokens(self.root)["tokens"]["access_token"]

    def test_grant_update_revoke_from_the_app(self):
        options = self.get("/api/v1/app/platform/access/options", self.platform, tenant_id=str(self.tenant.id))
        self.assertEqual(options.status_code, 200, options.content)
        members = options.json()["members"]
        self.assertEqual(set(members[0]), OPTION_MEMBER_KEYS)
        self.assertEqual({row["role_text"] for row in members}, {"Proprietário", "Gerente"})
        body = {"scope": "tenant", "user_id": self.manager.pk, "tenant_id": str(self.tenant.id),
                "shop_ids": [str(self.centro.id)], "permissions": ["sales", "cash"], "expires_on": None,
                "reason": "Dona pediu acesso ao app"}
        created = self.call("POST", "/api/v1/app/platform/access", self.platform, body)
        self.assertEqual(created.status_code, 201, created.content)
        access = created.json()
        self.assertEqual(set(access), ACCESS_KEYS)
        self.assertEqual((access["status"], access["status_text"], access["all_shops"]), ("active", "Ativo", False))
        self.assertEqual(access["granted_by"]["id"], self.root.pk)
        dup = self.assertCode(self.call("POST", "/api/v1/app/platform/access", self.platform, body), 409,
                              "access_exists")
        self.assertEqual(dup["access_id"], access["id"])
        self.assertCode(self.call("POST", "/api/v1/app/platform/access", self.platform,
                                  dict(body, user_id=self.outsider.pk)), 409, "member_required")
        self.assertCode(self.call("POST", "/api/v1/app/platform/access", self.platform,
                                  {"scope": "platform", "user_id": self.root.pk, "reason": "Teste"}),
                        409, "superuser_implicit")
        not_staff = self.assertCode(self.call("POST", "/api/v1/app/platform/access", self.platform,
                                              {"scope": "platform", "user_id": self.owner.pk, "reason": "Teste"}),
                                    409, "member_required")
        self.assertIn("equipe CrownTech", not_staff["error"])
        self.assertCode(self.call("POST", "/api/v1/app/platform/access", self.platform,
                                  dict(body, user_id=self.owner.pk, permissions=[])), 400, "invalid_request")
        self.assertCode(self.call("POST", "/api/v1/app/platform/access", self.platform,
                                  dict(body, user_id=self.owner.pk, shop_ids=[str(self.foreign_shop.id)])),
                        400, "invalid_request")
        self.assertCode(self.call("POST", "/api/v1/app/platform/access", self.platform,
                                  dict(body, user_id=self.owner.pk, expires_on="2000-01-01")), 400, "invalid_request")
        staff = self.call("POST", "/api/v1/app/platform/access", self.platform,
                          {"scope": "platform", "user_id": self.support.pk, "expires_on": None, "reason": "Atendimento"})
        self.assertEqual(staff.status_code, 201, staff.content)
        self.assertIsNone(staff.json()["tenant"])
        self.assertEqual(staff.json()["permissions"], ALL)
        until = (timezone.localdate() + timedelta(days=10)).isoformat()
        patched = self.call("PATCH", f"/api/v1/app/platform/access/{access['id']}", self.platform,
                            {"shop_ids": [], "permissions": ALL, "expires_on": until})
        self.assertEqual(patched.status_code, 200, patched.content)
        self.assertEqual((patched.json()["all_shops"], patched.json()["expires_on"]), (True, until))
        self.assertEqual(len(patched.json()["shops"]), 0)
        listing = self.get("/api/v1/app/platform/access", self.platform).json()
        self.assertEqual(len(listing["results"]), 2)
        revoked = self.call("POST", f"/api/v1/app/platform/access/{access['id']}/revoke", self.platform,
                            {"reason": "Fim do contrato"})
        self.assertEqual(revoked.json()["status"], "revoked")
        self.assertCode(self.call("POST", f"/api/v1/app/platform/access/{access['id']}/revoke", self.platform,
                                  {"reason": "De novo"}), 409, "conflict")
        actions = set(AuditEvent.objects.filter(actor=self.root).values_list("action", flat=True))
        self.assertTrue({"app.access_granted", "app.access_updated", "app.access_revoked"} <= actions)
        for event in AuditEvent.objects.filter(action__startswith="app."):
            dumped = json.dumps(event.detail)
            self.assertNotIn("pa_", dumped)
            self.assertNotIn("pr_", dumped)

    def test_platform_tenants_and_devices(self):
        rows = self.get("/api/v1/app/platform/tenants", self.platform).json()["results"]
        self.assertEqual(set(rows[0]), PLATFORM_TENANT_KEYS)
        mercado = next(row for row in rows if row["name"] == "Mercado Bom Preço")
        self.assertEqual((mercado["subscription_status"], mercado["terminals_total"], mercado["terminals_offline"]),
                         ("trial", 2, 1))
        self.assertEqual(mercado["health"], "attention")
        attention = self.get("/api/v1/app/platform/tenants", self.platform, health="attention").json()["results"]
        self.assertIn("Mercado Bom Preço", [row["name"] for row in attention])
        self.grant(self.owner, self.tenant)
        owner_tokens = self.signed_in(self.owner)["tokens"]
        devices = self.get("/api/v1/app/platform/devices", self.platform, q="dona").json()["results"]
        self.assertEqual(len(devices), 1)
        self.assertEqual(set(devices[0]), PLATFORM_DEVICE_KEYS)
        revoked = self.call("POST", f"/api/v1/app/platform/devices/{devices[0]['id']}/revoke", self.platform,
                            {"reason": "Celular perdido"})
        self.assertEqual(revoked.json()["status"], "revoked")
        self.assertCode(self.get("/api/v1/app/me", owner_tokens["access_token"]), 401, "device_revoked")

    def test_tenant_people_and_staff_without_superuser_cannot_manage(self):
        self.grant(self.owner, self.tenant)
        owner = self.signed_in(self.owner)["tokens"]["access_token"]
        self.assertCode(self.get("/api/v1/app/platform/tenants", owner), 403, "platform_forbidden")
        self.grant(self.support, scope="platform")
        support = self.platform_tokens(self.support)["tokens"]["access_token"]
        self.assertEqual(self.get("/api/v1/app/platform/tenants", support).status_code, 200)
        self.assertCode(self.get("/api/v1/app/platform/access", support), 403, "platform_forbidden")
        self.assertCode(self.call("POST", "/api/v1/app/platform/access", support,
                                  {"scope": "tenant", "user_id": self.manager.pk}), 403, "platform_forbidden")


class WebPlatformAppAccessTests(AppBase):
    def api(self, verb, path, body=None):
        return self.client.generic(verb, path, data=json.dumps(body) if body is not None else None,
                                   content_type="application/json")

    def test_superuser_grants_and_revokes_in_the_panel(self):
        self.client.force_login(self.root)
        options = self.api("GET", f"/api/v1/platform/app-access/options?tenant_id={self.tenant.id}")
        self.assertEqual(options.status_code, 200, options.content)
        staff = self.api("GET", "/api/v1/platform/app-access/options").json()
        self.assertEqual([row["email"] for row in staff["members"]], [self.support.email])
        self.assertIsNone(staff["tenant"])
        created = self.api("POST", "/api/v1/platform/app-access",
                           {"scope": "tenant", "user_id": self.owner.pk, "tenant_id": str(self.tenant.id),
                            "permissions": ALL, "reason": "Liberado pelo painel"})
        self.assertEqual(created.status_code, 201, created.content)
        self.assertEqual(set(created.json()), ACCESS_KEYS)
        self.assertEqual(self.api("POST", "/api/v1/platform/app-access",
                                  {"scope": "tenant", "user_id": self.owner.pk, "tenant_id": str(self.tenant.id),
                                   "permissions": ALL, "reason": "De novo"}).json()["code"], "access_exists")
        listing = self.api("GET", "/api/v1/platform/app-access?status=all").json()
        self.assertEqual(len(listing["results"]), 1)
        tokens = self.signed_in(self.owner)["tokens"]
        devices = self.api("GET", "/api/v1/platform/app-devices").json()["results"]
        self.assertEqual(len(devices), 1)
        revoked = self.api("POST", f"/api/v1/platform/app-access/{created.json()['id']}/revoke",
                           {"reason": "Pedido da empresa"})
        self.assertEqual(revoked.status_code, 200, revoked.content)
        self.assertEqual(revoked.json()["devices_active"], 0)
        self.assertCode(self.get("/api/v1/app/me", tokens["access_token"]), 401, "device_revoked")
        self.assertTrue(AuditEvent.objects.filter(action="app.access_revoked", tenant=self.tenant).exists())

    def test_staff_without_superuser_gets_403_and_csrf_is_enforced(self):
        self.client.force_login(self.support)
        self.assertEqual(self.api("GET", "/api/v1/platform/app-access").status_code, 403)
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.root)
        response = csrf_client.post("/api/v1/platform/app-access", data="{}", content_type="application/json")
        self.assertEqual(response.status_code, 403)

    def test_account_deletion_public_request_and_resolution(self):
        self.grant(self.owner, self.tenant)
        tokens = self.signed_in(self.owner)["tokens"]
        public = self.client.post("/api/v1/public/account-deletion", data=json.dumps({"email": self.owner.email}),
                                  content_type="application/json")
        self.assertEqual(public.status_code, 202, public.content)
        unknown = self.client.post("/api/v1/public/account-deletion", data=json.dumps({"email": "x@nada.test"}),
                                   content_type="application/json")
        self.assertEqual((unknown.status_code, unknown.json()), (202, public.json()))
        # The public form never signs anyone out.
        self.assertEqual(self.get("/api/v1/app/me", tokens["access_token"]).status_code, 200)
        self.client.force_login(self.root)
        pending = self.client.get("/api/v1/platform/account-deletions").json()["results"]
        self.assertEqual(len(pending), 2)
        self.assertEqual(set(pending[0]), DELETION_KEYS)
        mine = next(row for row in pending if row["user"])
        self.assertEqual((mine["source"], mine["status_text"]), ("web", "Pendente"))
        done = self.client.post(f"/api/v1/platform/account-deletions/{mine['id']}/resolve",
                                data=json.dumps({"status": "done", "note": "Confirmado por e-mail"}),
                                content_type="application/json")
        self.assertEqual(done.status_code, 200, done.content)
        self.assertEqual(done.json()["status"], "done")
        self.owner.refresh_from_db()
        self.assertFalse(self.owner.is_active)
        self.assertFalse(AppAccess.objects.filter(user=self.owner, revoked_at__isnull=True).exists())
        self.assertCode(self.get("/api/v1/app/me", tokens["access_token"]), 401, "device_revoked")
        self.assertTrue(AuditEvent.objects.filter(action="app.account_deletion_resolved").exists())
        again = self.client.post(f"/api/v1/platform/account-deletions/{mine['id']}/resolve",
                                 data=json.dumps({"status": "rejected"}), content_type="application/json")
        self.assertEqual(again.status_code, 409)
        root_request = AccountDeletionRequest.objects.create(user=self.root, email=self.root.email, source="web")
        refused = self.client.post(f"/api/v1/platform/account-deletions/{root_request.id}/resolve",
                                   data=json.dumps({"status": "done"}), content_type="application/json")
        self.assertEqual(refused.status_code, 409)

    def test_public_pages_render(self):
        for path, title in (("/privacidade/", "Política de privacidade"), ("/suporte/", "Suporte"),
                            ("/excluir-conta/", "Excluir minha conta")):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200, path)
            self.assertTrue(response["Content-Type"].startswith("text/html"))
            body = response.content.decode()
            self.assertIn(title, body)
            self.assertIn("suporte@crowntech.com.br", body)
            self.assertNotIn("__PDV_", body)
            self.assertNotIn("<script>", body)
        self.assertIn("/assets/public.js", self.client.get("/excluir-conta/").content.decode())
        privacy = self.client.get("/privacidade/").content.decode()
        for text in ("CrownTech", "LGPD", "6 meses", "Excluir minha conta", "anúncios"):
            self.assertIn(text, privacy)
