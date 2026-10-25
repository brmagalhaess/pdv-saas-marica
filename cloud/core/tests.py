"""API contract tests for tenant isolation, RBAC, licensing and atomic intake."""
import hashlib
import json
import os
import time
import uuid
from datetime import timedelta
from ipaddress import ip_network
from unittest.mock import Mock, patch

import pyotp
from django.contrib.auth import get_user_model
from django.db import connection
from django.db.models import Sum
from django.db.migrations.executor import MigrationExecutor
from django.test import Client, RequestFactory, TestCase, TransactionTestCase, override_settings
from django.utils import timezone

from shared.licensing import generate_keypair, verify_lease
from operations.models import BillingBinding
from operations.services import enqueue
from .api_common import APIError, client_ip, password_policy
from .models import (AuditEvent, Membership, Plan, Product, RateLimit, Sale, SalePayment, Shop,
                      PlatformMFA, StockMovement, Subscription, SyncConflict, SyncEvent, Tenant, Terminal)


class CloudAPITests(TestCase):
    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.owner = User.objects.create_user(username="owner@example.test", email="owner@example.test",
                                             password="Synthetic-42!", first_name="Dona")
        cls.other = User.objects.create_user(username="other@example.test", email="other@example.test",
                                             password="Synthetic-43!")
        cls.tenant = Tenant.objects.create(name="Mercado Um")
        cls.other_tenant = Tenant.objects.create(name="Mercado Dois")
        cls.member = Membership.objects.create(tenant=cls.tenant, user=cls.owner,
                                               name="Dona", role="owner")
        Membership.objects.create(tenant=cls.other_tenant, user=cls.other, name="Outra", role="owner")
        cls.shop = Shop.objects.create(tenant=cls.tenant, name="Matriz")
        Shop.objects.create(tenant=cls.other_tenant, name="Matriz")
        cls.plan = Plan.objects.get(slug="essential")
        cls.management = Plan.objects.get(slug="management")
        cls.sub = Subscription.objects.create(tenant=cls.tenant, plan=cls.plan, status="trial",
                                              trial_ends_at=timezone.now() + timedelta(days=14))
        Subscription.objects.create(tenant=cls.other_tenant, plan=cls.plan, status="trial",
                                    trial_ends_at=timezone.now() + timedelta(days=14))
        cls.product = Product.objects.create(tenant=cls.tenant, sku="ARROZ", name="Arroz", price_cents=999)
        cls.foreign_product = Product.objects.create(tenant=cls.other_tenant, sku="FEIJAO",
                                                     name="Feijão", price_cents=1099)

    def api(self, verb, path, body=None, *, tenant=None, client=None, **headers):
        if tenant is not False:
            headers.setdefault("HTTP_X_TENANT_ID", str(tenant or self.tenant.id))
        return (client or self.client).generic(verb, path,
            data=json.dumps(body) if body is not None else None,
            content_type="application/json", **headers)

    def test_password_shape_and_sql_login_throttle(self):
        self.assertTrue(password_policy("Synthetic-42!"))
        self.assertFalse(password_policy("synthetic42"))
        for _ in range(5):
            response = self.api("POST", "/api/v1/auth/login",
                                {"email": "owner@example.test", "password": "wrong"}, tenant=False)
            self.assertEqual(response.status_code, 401)
        self.assertEqual(self.api("POST", "/api/v1/auth/login",
                                  {"email": "owner@example.test", "password": "wrong"}, tenant=False).status_code, 429)
        self.assertEqual(RateLimit.objects.count(), 1)

    @override_settings(PLATFORM_MFA_REQUIRED=True)
    def test_platform_login_requires_totp_enrollment_and_second_factor(self):
        User = get_user_model()
        root = User.objects.create_superuser(username="root-mfa@example.test",
                                              email="root-mfa@example.test",
                                              password="Root-Mfa-42!")
        first = self.api("POST", "/api/v1/auth/login",
                         {"email": root.email, "password": "Root-Mfa-42!"}, tenant=False)
        self.assertEqual(first.status_code, 200, first.content)
        self.assertTrue(first.json()["mfa_setup_required"])
        setup = self.api("GET", "/api/v1/auth/mfa/setup", tenant=False)
        self.assertEqual(setup.status_code, 200, setup.content)
        code = pyotp.TOTP(setup.json()["secret"]).now()
        confirmed = self.api("POST", "/api/v1/auth/mfa/setup/confirm", {"code": code}, tenant=False)
        self.assertEqual(confirmed.status_code, 200, confirmed.content)
        self.assertEqual(len(confirmed.json()["recovery_codes"]), 10)
        self.assertTrue(PlatformMFA.objects.get(user=root).enabled)
        self.assertEqual(self.api("GET", "/api/v1/platform/tenants", tenant=False).status_code, 200)
        self.api("POST", "/api/v1/auth/logout", tenant=False)
        second = self.api("POST", "/api/v1/auth/login",
                          {"email": root.email, "password": "Root-Mfa-42!"}, tenant=False)
        self.assertTrue(second.json()["mfa_required"])
        self.assertEqual(self.api("GET", "/api/v1/platform/tenants", tenant=False).status_code, 401)
        verified = self.api("POST", "/api/v1/auth/mfa/verify",
                            {"code": pyotp.TOTP(setup.json()["secret"]).now()}, tenant=False)
        self.assertEqual(verified.status_code, 200, verified.content)
        self.assertEqual(self.api("GET", "/api/v1/platform/tenants", tenant=False).status_code, 200)

    @override_settings(PLATFORM_MFA_REQUIRED=False)
    def test_platform_security_menu_can_enroll_and_rotate_recovery_codes(self):
        User = get_user_model()
        root = User.objects.create_superuser(username="root-security@example.test",
                                              email="root-security@example.test",
                                              password="Root-Security-42!")
        self.client.force_login(root)
        status = self.api("GET", "/api/v1/auth/mfa/status", tenant=False)
        self.assertEqual(status.status_code, 200, status.content)
        self.assertFalse(status.json()["enabled"])
        enrolled = self.api("POST", "/api/v1/auth/mfa/enroll", {}, tenant=False)
        self.assertEqual(enrolled.status_code, 200, enrolled.content)
        confirmed = self.api("POST", "/api/v1/auth/mfa/setup/confirm",
                             {"code": pyotp.TOTP(enrolled.json()["secret"]).now()}, tenant=False)
        self.assertEqual(confirmed.status_code, 200, confirmed.content)
        rotated = self.api("POST", "/api/v1/auth/mfa/recovery-codes", {}, tenant=False)
        self.assertEqual(rotated.status_code, 200, rotated.content)
        self.assertEqual(len(rotated.json()["recovery_codes"]), 10)

    def test_proxy_throttle_uses_last_forwarded_ip_only_from_trusted_peer(self):
        trust = (ip_network("172.16.0.0/12"),)
        with override_settings(PDV_TRUST_PROXY=True, PDV_TRUSTED_PROXY_NETWORKS=trust):
            headers = {"REMOTE_ADDR": "172.18.0.4",
                       "HTTP_X_FORWARDED_FOR": "192.0.2.200, 198.51.100.10"}
            body = {"email": "owner@example.test", "password": "wrong"}
            for _ in range(5):
                self.assertEqual(self.api("POST", "/api/v1/auth/login", body,
                                          tenant=False, **headers).status_code, 401)
            self.assertEqual(self.api("POST", "/api/v1/auth/login", body,
                                      tenant=False, **headers).status_code, 429)
            headers["HTTP_X_FORWARDED_FOR"] = "192.0.2.200, 198.51.100.11"
            self.assertEqual(self.api("POST", "/api/v1/auth/login", body,
                                      tenant=False, **headers).status_code, 401)
            direct = RequestFactory().get("/", REMOTE_ADDR="203.0.113.9",
                HTTP_X_FORWARDED_FOR="192.0.2.200, 198.51.100.88")
            self.assertEqual(client_ip(direct), "203.0.113.9")
        with override_settings(PDV_TRUST_PROXY=True, PDV_TRUSTED_PROXY_NETWORKS=()):
            direct = RequestFactory().get("/", REMOTE_ADDR="172.18.0.4",
                HTTP_X_FORWARDED_FOR="198.51.100.88")
            self.assertEqual(client_ip(direct), "172.18.0.4")

    def test_tenant_and_foreign_product_are_isolated(self):
        self.client.force_login(self.owner)
        self.assertEqual(self.api("GET", f"/api/v1/products/{self.product.id}").status_code, 200)
        self.assertEqual(self.api("GET", f"/api/v1/products/{self.foreign_product.id}").status_code, 404)
        self.assertEqual(self.api("GET", "/api/v1/products", tenant=self.other_tenant.id).status_code, 403)
        response = self.api("POST", "/api/v1/stock/movements",
                            {"shop_id": str(self.shop.id), "product_id": str(self.foreign_product.id),
                             "delta_milli": 1000, "reason": "Carga", "idempotency_key": "foreign"})
        self.assertEqual(response.status_code, 404)
        fractional = self.api("POST", "/api/v1/stock/movements",
                              {"shop_id": str(self.shop.id), "product_id": str(self.product.id),
                               "delta_milli": 500, "reason": "Carga", "idempotency_key": "half-unit"})
        self.assertEqual(fractional.status_code, 400)

    def test_product_gets_local_image_default_and_accepts_custom_image(self):
        self.client.force_login(self.owner)
        rejected_cost = self.api("POST", "/api/v1/products",
                                 {"name": "Custo no cadastro", "price_cents": 499, "cost_cents": 300})
        self.assertEqual(rejected_cost.status_code, 400)
        created = self.api("POST", "/api/v1/products",
                           {"name": "Leite integral", "price_cents": 499,
                            "category_id": None, "unit": "KG", "scale_code": "54321"})
        self.assertEqual(created.status_code, 201, created.content)
        self.assertTrue(created.json()["image_url"].endswith("/beverages.svg"))
        self.assertEqual(created.json()["scale_code"], "54321")
        product_id = created.json()["id"]
        updated = self.api("PATCH", f"/api/v1/products/{product_id}",
                           {"image_url": "https://cdn.example.test/leite.png"})
        self.assertEqual(updated.status_code, 200, updated.content)
        self.assertEqual(updated.json()["image_url"], "https://cdn.example.test/leite.png")

    def test_product_can_be_marked_as_company_production_without_stock_control(self):
        self.client.force_login(self.owner)
        created = self.api("POST", "/api/v1/products",
                           {"name": "Pão francês", "price_cents": 1290,
                            "unit": "KG", "stock_controlled": False})
        self.assertEqual(created.status_code, 201, created.content)
        self.assertFalse(created.json()["stock_controlled"])
        product_id = created.json()["id"]
        listed = self.api("GET", "/api/v1/products").json()["results"]
        self.assertFalse(next(row for row in listed if row["id"] == product_id)["stock_controlled"])
        updated = self.api("PATCH", f"/api/v1/products/{product_id}", {"stock_controlled": True})
        self.assertEqual(updated.status_code, 200, updated.content)
        self.assertTrue(updated.json()["stock_controlled"])

    def test_last_owner_and_plan_features(self):
        self.client.force_login(self.owner)
        self.assertEqual(self.api("PATCH", f"/api/v1/users/{self.member.id}", {"active": False}).status_code, 409)
        self.assertTrue(Membership.objects.get(pk=self.member.pk).active)
        self.assertEqual(self.api("GET", "/api/v1/finance").status_code, 403)
        self.assertEqual(self.api("GET", "/api/v1/dashboard?from=2026-01-01&to=2026-01-31").status_code, 403)
        self.sub.plan = self.management
        self.sub.save(update_fields=["plan"])
        self.assertEqual(self.api("GET", "/api/v1/finance").status_code, 200)

    def test_terminal_reactivation_respects_plan_limit(self):
        self.client.force_login(self.owner)
        Terminal.objects.create(tenant=self.tenant, shop=self.shop, name="Caixa ativo")
        revoked = Terminal.objects.create(tenant=self.tenant, shop=self.shop, name="Caixa revogado", revoked=True)
        self.assertEqual(self.api("PATCH", f"/api/v1/terminals/{revoked.id}", {"revoked": False}).status_code, 409)
        self.assertTrue(Terminal.objects.get(pk=revoked.pk).revoked)
        self.assertEqual(self.api("PATCH", f"/api/v1/terminals/{revoked.id}", {"reissue_pairing": []}).status_code, 400)

    def test_expired_subscription_cannot_renew_forever(self):
        self.sub.status = "active"
        self.sub.current_period_end = timezone.now() - timedelta(days=4)
        self.sub.save(update_fields=["status", "current_period_end"])
        code = "test-pair-code"
        terminal = Terminal.objects.create(tenant=self.tenant, shop=self.shop, name="Caixa",
            pairing_code_hash=hashlib.sha256(code.encode()).hexdigest(),
            pairing_expires_at=timezone.now() + timedelta(minutes=10))
        body = {"license_key": self.sub.license_key, "code": code,
                "device_id": "device-1", "version": "1.0"}
        self.assertEqual(self.api("POST", "/api/v1/terminal/pair", body, tenant=False).status_code, 403)
        self.sub.current_period_end = timezone.now() - timedelta(hours=24)
        self.sub.save(update_fields=["current_period_end"])
        private, public = generate_keypair()
        with patch.dict(os.environ, {"PDV_LICENSE_PRIVATE_KEY": private, "PDV_LICENSE_PUBLIC_KEY": public,
                                    "PDV_LICENSE_PRIVATE_KEY_FILE": "", "PDV_LICENSE_PUBLIC_KEY_FILE": ""}):
            paired = self.api("POST", "/api/v1/terminal/pair", body, tenant=False)
        self.assertEqual(paired.status_code, 200, paired.content)
        claims = verify_lease(paired.json()["lease"], public,
                              expected={"terminal_id": terminal.id, "device_id": "device-1"})
        self.assertEqual(claims["status"], "past_due")
        self.assertLessEqual(claims["expires_at"], int(self.sub.current_period_end.timestamp()) + 72 * 3600)

    def test_license_is_stable_company_scoped_and_visible_to_administrators(self):
        self.assertTrue(self.sub.license_key.startswith("PDV2-"))
        other_key = Subscription.objects.get(tenant=self.other_tenant).license_key
        self.assertNotEqual(self.sub.license_key, other_key)
        self.client.force_login(self.owner)
        before = self.api("GET", "/api/v1/subscription").json()
        self.assertEqual(before["license"]["key"], self.sub.license_key)
        self.assertEqual(before["license"]["status"], "trial")
        self.assertTrue(before["license"]["can_activate"])
        self.assertEqual(before["license"]["terminal_limit"], 1)
        self.assertEqual(before["license"]["terminal_used"], 0)
        self.assertEqual(before["license"]["terminal_available"], 1)
        Terminal.objects.create(tenant=self.tenant, shop=self.shop, name="Caixa 1")
        after = self.api("GET", "/api/v1/subscription").json()
        self.assertEqual(after["license"]["terminal_used"], 1)
        self.assertEqual(after["license"]["terminal_available"], 0)
        cashier = get_user_model().objects.create_user(username="cashier@example.test",
                                                        password="Cashier-42!")
        Membership.objects.create(tenant=self.tenant, user=cashier, name="Caixa", role="cashier")
        self.client.force_login(cashier)
        self.assertIsNone(self.api("GET", "/api/v1/subscription").json()["license"]["key"])
        self.assertEqual(self.api("GET", "/api/v1/subscription", tenant=self.other_tenant.id).status_code, 403)

    def test_subscription_panel_reports_effective_expiry(self):
        self.client.force_login(self.owner)
        response = self.api("GET", "/api/v1/subscription")
        self.assertEqual(response["Cache-Control"], "private, no-store")
        self.sub.trial_ends_at = timezone.now() - timedelta(minutes=1)
        self.sub.save(update_fields=["trial_ends_at"])
        data = self.api("GET", "/api/v1/subscription").json()
        self.assertEqual(data["license"]["status"], "expired")
        self.assertFalse(data["license"]["can_activate"])
        self.sub.status = "active"
        self.sub.current_period_end = timezone.now() - timedelta(hours=1)
        self.sub.save(update_fields=["status", "current_period_end"])
        self.assertEqual(self.api("GET", "/api/v1/subscription").json()["license"]["status"], "past_due")
        self.sub.current_period_end = timezone.now() - timedelta(days=4)
        self.sub.save(update_fields=["current_period_end"])
        self.assertEqual(self.api("GET", "/api/v1/subscription").json()["license"]["status"], "expired")

    def test_owner_monitoring_snapshot_is_tenant_scoped_and_owner_only(self):
        online = Terminal.objects.create(tenant=self.tenant, shop=self.shop, name="Caixa 1",
                                         device_id="device-1", token_hash="a" * 64,
                                         last_seen_at=timezone.now())
        offline = Terminal.objects.create(tenant=self.tenant, shop=self.shop, name="Caixa 2",
                                         device_id="device-2", last_seen_at=timezone.now() - timedelta(minutes=8))
        sale = Sale.objects.create(id=uuid.uuid4(), tenant=self.tenant, shop=self.shop,
                                   terminal=online, request_id="monitoring-sale-1",
                                   content_hash="b" * 64, gross_cents=1250, total_cents=1200,
                                   discount_cents=50, change_cents=0,
                                   created_at=timezone.now())
        SalePayment.objects.create(sale=sale, line_number=0, method="pix", amount_cents=1200)
        self.client.force_login(self.owner)
        response = self.api("GET", "/api/v1/owner/monitoring")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response["Cache-Control"], "private, no-store")
        data = response.json()
        self.assertEqual(data["terminals_total"], 2)
        self.assertEqual(data["terminals_online"], 1)
        self.assertEqual(data["sales"]["count"], 1)
        self.assertEqual(data["sales"]["revenue_cents"], 1200)
        self.assertEqual(data["payments_cents"]["pix"], 1200)
        self.assertEqual(len(data["sales_by_terminal"]), 1)
        manager = get_user_model().objects.create_user(username="manager-monitoring@example.test",
                                                       password="Manager-42!")
        Membership.objects.create(tenant=self.tenant, user=manager, name="Gerente", role="manager")
        self.client.force_login(manager)
        self.assertEqual(self.api("GET", "/api/v1/owner/monitoring").status_code, 403)

    def test_pair_requires_matching_license_and_one_time_code(self):
        code = "pair-company-1"
        terminal = Terminal.objects.create(tenant=self.tenant, shop=self.shop, name="Caixa",
            pairing_code_hash=hashlib.sha256(code.encode()).hexdigest(),
            pairing_expires_at=timezone.now() + timedelta(minutes=10))
        body = {"code": code, "device_id": "device-1", "version": "1.0"}
        self.assertEqual(self.api("POST", "/api/v1/terminal/pair", body, tenant=False).status_code, 400)
        body["license_key"] = Subscription.objects.get(tenant=self.other_tenant).license_key
        self.assertEqual(self.api("POST", "/api/v1/terminal/pair", body, tenant=False).status_code, 401)
        self.assertIsNotNone(Terminal.objects.get(pk=terminal.pk).pairing_code_hash)
        body["license_key"] = self.sub.license_key.lower()
        private, public = generate_keypair()
        with patch.dict(os.environ, {"PDV_LICENSE_PRIVATE_KEY": private, "PDV_LICENSE_PUBLIC_KEY": public,
                                    "PDV_LICENSE_PRIVATE_KEY_FILE": "", "PDV_LICENSE_PUBLIC_KEY_FILE": ""}):
            result = self.api("POST", "/api/v1/terminal/pair", body, tenant=False)
            bootstrap = self.api("GET", "/api/v1/terminal/bootstrap", tenant=False,
                                 HTTP_AUTHORIZATION="Bearer " + result.json()["token"])
        self.assertEqual(result.status_code, 200, result.content)
        self.assertEqual(result.json()["license_hint"], self.sub.license_key[-4:])
        self.assertEqual(bootstrap.status_code, 200, bootstrap.content)
        self.assertEqual(bootstrap.json()["license_hint"], self.sub.license_key[-4:])
        self.assertEqual(self.api("POST", "/api/v1/terminal/pair", body, tenant=False).status_code, 401)

    def test_vip_requires_platform_root_is_audited_and_clears_on_manual_edit(self):
        User = get_user_model()
        root = User.objects.create_superuser(username="root-vip@example.test",
                                             email="root-vip@example.test", password="Root-42!")
        end = (timezone.now() + timedelta(days=365)).replace(microsecond=0)
        body = {"plan_id": str(self.management.id), "current_period_end": end.isoformat(),
                "reason": "Piloto comercial"}
        path = f"/api/v1/platform/subscriptions/{self.sub.id}/vip"
        self.client.force_login(self.owner)
        self.assertEqual(self.api("POST", path, body, tenant=False).status_code, 403)
        self.client.force_login(root)
        granted = self.api("POST", path, body, tenant=False)
        self.assertEqual(granted.status_code, 200, granted.content)
        self.sub.refresh_from_db()
        self.assertEqual(self.sub.license_source, "vip")
        self.assertEqual(self.sub.status, "active")
        self.assertEqual(self.sub.plan_id, self.management.id)
        self.assertEqual(granted.json()["license_source"], "vip")
        self.assertEqual(AuditEvent.objects.filter(action="platform.vip_granted").count(), 1)
        self.assertEqual(self.api("POST", path, body, tenant=False).status_code, 200)
        self.assertEqual(AuditEvent.objects.filter(action="platform.vip_granted").count(), 1)
        cancelled = self.api("PATCH", f"/api/v1/platform/subscriptions/{self.sub.id}",
                             {"status": "cancelled"}, tenant=False)
        self.assertEqual(cancelled.status_code, 200, cancelled.content)
        self.sub.refresh_from_db()
        self.assertEqual(self.sub.license_source, "manual")
        self.assertEqual(self.sub.status, "cancelled")
        with patch.dict(os.environ, {"PDV_LICENSE_PRIVATE_KEY": "", "PDV_LICENSE_PRIVATE_KEY_FILE": ""}):
            self.assertEqual(self.api("POST", "/api/v1/terminals",
                {"shop_id": str(self.shop.id), "name": "Caixa"}).status_code, 403)

    def test_vip_rejects_active_recurring_charge(self):
        root = get_user_model().objects.create_superuser(
            username="root-billing@example.test", email="root-billing@example.test",
            password="Root-42!")
        self.client.force_login(root)
        binding = BillingBinding.objects.create(tenant=self.tenant, plan=self.plan,
            external_id="sub-vip-block", customer_id="customer-1", cycle="MONTHLY",
            amount_cents=self.plan.price_month_cents)
        body = {"plan_id": str(self.plan.id),
                "current_period_end": (timezone.now() + timedelta(days=30)).isoformat()}
        path = f"/api/v1/platform/subscriptions/{self.sub.id}/vip"
        provider = Mock()
        provider.get_subscription.return_value = {"id": binding.external_id,
            "customer": binding.customer_id, "status": "ACTIVE"}
        with patch("core.views_platform.account", return_value=provider):
            result = self.api("POST", path, body, tenant=False)
        self.assertEqual(result.status_code, 409, result.content)
        self.assertIn("Pause", result.json()["error"])
        self.assertEqual(Subscription.objects.get(pk=self.sub.pk).license_source, "trial")
        provider.get_subscription.return_value["status"] = "INACTIVE"
        with patch("core.views_platform.account", return_value=provider):
            self.assertEqual(self.api("POST", path, body, tenant=False).status_code, 200)
        binding.refresh_from_db()
        self.assertFalse(binding.active)
        self.assertEqual(Subscription.objects.get(pk=self.sub.pk).license_source, "vip")
        self.assertEqual(AuditEvent.objects.filter(action="platform.billing_paused_verified").count(), 1)

    def test_vip_blocks_pending_billing_creation(self):
        root = get_user_model().objects.create_superuser(
            username="root-pending@example.test", email="root-pending@example.test",
            password="Root-42!")
        self.client.force_login(root)
        job = enqueue("billing.subscribe", self.tenant, "vip-pending", {"plan_id": str(self.plan.id)})
        body = {"plan_id": str(self.plan.id),
                "current_period_end": (timezone.now() + timedelta(days=30)).isoformat()}
        path = f"/api/v1/platform/subscriptions/{self.sub.id}/vip"
        result = self.api("POST", path, body, tenant=False)
        self.assertEqual(result.status_code, 409, result.content)
        self.assertEqual(Subscription.objects.get(pk=self.sub.pk).license_source, "trial")
        job.status = "needs_review"
        job.save(update_fields=["status"])
        self.assertEqual(self.api("POST", path, body, tenant=False).status_code, 409)

    def test_vip_expires_on_selected_date_without_paid_grace(self):
        from .api_common import APIError, subscription_window
        self.client.force_login(self.owner)
        self.sub.status = "active"
        self.sub.license_source = "vip"
        self.sub.current_period_end = timezone.now() - timedelta(minutes=1)
        self.sub.save()
        response = self.api("GET", "/api/v1/subscription")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["license"]["status"], "expired")
        with self.assertRaises(APIError):
            subscription_window(self.tenant)

    def test_invalid_terminal_batch_rolls_back_and_replay_is_idempotent(self):
        token = "terminal-token-" + uuid.uuid4().hex
        terminal = Terminal.objects.create(tenant=self.tenant, shop=self.shop, name="Caixa",
            device_id="test-device", token_hash=hashlib.sha256(token.encode()).hexdigest())
        identity = {"tenant_id": str(self.tenant.id), "shop_id": str(self.shop.id),
                    "terminal_id": str(terminal.id)}
        now_ms = int(time.time() * 1000)
        opened = {"event_id": uuid.uuid4().hex, "type": "cash.opened", "aggregate_id": uuid.uuid4().hex,
                  "identity": identity, "data": {"opening_cents": 0, "operator_id": str(self.member.id),
                                                "created_at": now_ms}}
        invalid = {"event_id": uuid.uuid4().hex, "type": "cash.closed", "aggregate_id": opened["aggregate_id"],
                   "identity": identity, "data": {"counted_cents": 100, "expected_cents": 50,
                                                 "difference_cents": 0, "created_at": now_ms}}
        headers = {"HTTP_AUTHORIZATION": f"Bearer {token}"}
        self.assertEqual(self.api("POST", "/api/v1/terminal/sync", {"events": [opened, invalid]},
                                  tenant=False, **headers).status_code, 400)
        self.assertEqual(SyncEvent.objects.count(), 0)
        self.assertEqual(self.api("POST", "/api/v1/terminal/sync", {"events": [opened]},
                                  tenant=False, **headers).status_code, 200)
        self.assertEqual(self.api("POST", "/api/v1/terminal/sync", {"events": [opened]},
                                  tenant=False, **headers).status_code, 200)
        self.assertEqual(SyncEvent.objects.count(), 1)

    def test_offline_negative_stock_creates_resolvable_conflict(self):
        token = "terminal-token-" + uuid.uuid4().hex
        terminal = Terminal.objects.create(tenant=self.tenant, shop=self.shop, name="Caixa",
            device_id="conflict-device", token_hash=hashlib.sha256(token.encode()).hexdigest())
        identity = {"tenant_id": str(self.tenant.id), "shop_id": str(self.shop.id),
                    "terminal_id": str(terminal.id)}
        event = {"event_id": uuid.uuid4().hex, "type": "sale.completed", "aggregate_id": str(uuid.uuid4()),
                 "identity": identity,
                 "data": {"request_id": uuid.uuid4().hex, "cash_session_id": uuid.uuid4().hex,
                          "created_at": int(time.time() * 1000), "customer_id": None,
                          "customer_name": "", "customer_document": "", "gross_cents": 999,
                          "discount_cents": 0, "total_cents": 999, "change_cents": 0,
                          "items": [{"product_id": str(self.product.id), "name": self.product.name,
                                     "unit": "UN", "quantity_milli": 1000, "unit_price_cents": 999,
                                     "gross_cents": 999, "discount_cents": 0, "line_total_cents": 999}],
                          "payments": [{"method": "card_credit", "amount_cents": 999}]}}
        headers = {"HTTP_AUTHORIZATION": f"Bearer {token}"}
        response = self.api("POST", "/api/v1/terminal/sync", {"events": [event]},
                            tenant=False, **headers)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(len(response.json()["conflicts"]), 1)
        conflict = SyncConflict.objects.get(event_id=event["event_id"])
        self.assertEqual(conflict.status, "open")
        self.client.force_login(self.owner)
        listing = self.api("GET", "/api/v1/sync/conflicts?status=open")
        self.assertEqual(listing.status_code, 200)
        self.assertEqual(len(listing.json()["results"]), 1)
        resolved = self.api("PATCH", f"/api/v1/sync/conflicts/{conflict.id}",
                            {"status": "resolved", "resolution": "Entrada conferida"})
        self.assertEqual(resolved.status_code, 200)
        self.assertEqual(resolved.json()["status"], "resolved")

    def test_company_production_sale_does_not_create_stock_movement(self):
        production = Product.objects.create(tenant=self.tenant, sku="PAO-KG", name="Pão francês",
                                             unit="KG", price_cents=1290, stock_controlled=False)
        token = "terminal-token-" + uuid.uuid4().hex
        terminal = Terminal.objects.create(tenant=self.tenant, shop=self.shop, name="Caixa",
            device_id="production-device", token_hash=hashlib.sha256(token.encode()).hexdigest())
        identity = {"tenant_id": str(self.tenant.id), "shop_id": str(self.shop.id),
                    "terminal_id": str(terminal.id)}
        sale_id = uuid.uuid4()
        event = {"event_id": uuid.uuid4().hex, "type": "sale.completed", "aggregate_id": str(sale_id),
                 "identity": identity,
                 "data": {"request_id": uuid.uuid4().hex, "cash_session_id": uuid.uuid4().hex,
                          "created_at": int(time.time() * 1000), "customer_id": None,
                          "customer_name": "", "customer_document": "", "gross_cents": 452,
                          "discount_cents": 0, "total_cents": 452, "change_cents": 0,
                          "items": [{"product_id": str(production.id), "name": production.name,
                                     "unit": "KG", "quantity_milli": 350, "unit_price_cents": 1290,
                                     "gross_cents": 452, "discount_cents": 0, "line_total_cents": 452}],
                          "payments": [{"method": "cash", "amount_cents": 452}]}}
        response = self.api("POST", "/api/v1/terminal/sync", {"events": [event]}, tenant=False,
                            HTTP_AUTHORIZATION=f"Bearer {token}")
        self.assertEqual(response.status_code, 200, response.content)
        sale = Sale.objects.get(pk=sale_id)
        self.assertEqual(sale.stock_status, "ok")
        self.assertFalse(StockMovement.objects.filter(product=production).exists())

    def test_sale_keeps_operator_and_seller_and_filters_by_them(self):
        seller_user = get_user_model().objects.create_user(username="seller@example.test",
                                                           email="seller@example.test", password="Synthetic-44!")
        seller = Membership.objects.create(tenant=self.tenant, user=seller_user, name="Vendedora", role="cashier")
        foreign = Membership.objects.get(tenant=self.other_tenant)
        token = "terminal-token-" + uuid.uuid4().hex
        terminal = Terminal.objects.create(tenant=self.tenant, shop=self.shop, name="Caixa",
            device_id="seller-device", token_hash=hashlib.sha256(token.encode()).hexdigest())
        identity = {"tenant_id": str(self.tenant.id), "shop_id": str(self.shop.id),
                    "terminal_id": str(terminal.id)}
        headers = {"HTTP_AUTHORIZATION": f"Bearer {token}"}

        def sale_event(**people):
            data = {"request_id": uuid.uuid4().hex, "cash_session_id": uuid.uuid4().hex,
                    "created_at": int(time.time() * 1000), "customer_id": None,
                    "customer_name": "", "customer_document": "", "gross_cents": 999,
                    "discount_cents": 0, "total_cents": 999, "change_cents": 1,
                    "items": [{"product_id": str(self.product.id), "name": self.product.name,
                               "unit": "UN", "quantity_milli": 1000, "unit_price_cents": 999,
                               "gross_cents": 999, "discount_cents": 0, "line_total_cents": 999,
                               # Sent by current terminals; rejecting it stalled the sync queue.
                               "stock_controlled": True}],
                    "payments": [{"method": "cash", "amount_cents": 1000}], **people}
            return {"event_id": uuid.uuid4().hex, "type": "sale.completed",
                    "aggregate_id": str(uuid.uuid4()), "identity": identity, "data": data}

        credited = sale_event(operator_id=str(self.member.id), seller_id=str(seller.id))
        legacy = sale_event()  # queued by a terminal released before operator/seller existed
        response = self.api("POST", "/api/v1/terminal/sync", {"events": [credited, legacy]},
                            tenant=False, **headers)
        self.assertEqual(response.status_code, 200, response.content)
        sale = Sale.objects.get(pk=credited["aggregate_id"])
        self.assertEqual((sale.operator_id, sale.seller_id), (self.member.id, seller.id))
        self.assertEqual((sale.operator_name_snapshot, sale.seller_name_snapshot), ("Dona", "Vendedora"))
        old = Sale.objects.get(pk=legacy["aggregate_id"])
        self.assertEqual((old.operator_id, old.seller_id), (None, None))

        intruder = sale_event(operator_id=str(self.member.id), seller_id=str(foreign.id))
        response = self.api("POST", "/api/v1/terminal/sync", {"events": [intruder]}, tenant=False, **headers)
        self.assertEqual(response.status_code, 400, response.content)
        self.assertFalse(Sale.objects.filter(pk=intruder["aggregate_id"]).exists())

        self.client.force_login(self.owner)
        listing = self.api("GET", f"/api/v1/sales?seller_id={seller.id}").json()["results"]
        self.assertEqual([row["id"] for row in listing], [str(sale.id)])
        self.assertEqual((listing[0]["operator_name"], listing[0]["seller_name"]), ("Dona", "Vendedora"))
        self.assertEqual(len(self.api("GET", f"/api/v1/sales?operator_id={self.member.id}").json()["results"]), 1)
        people = {row["name"]: row for row in self.api("GET", "/api/v1/sales/people").json()["results"]}
        self.assertEqual(set(people), {"Dona", "Vendedora"})
        self.assertTrue(people["Dona"]["operator"] and not people["Dona"]["seller"])
        self.assertTrue(people["Vendedora"]["seller"] and not people["Vendedora"]["operator"])
        self.assertEqual(self.api("GET", f"/api/v1/sales?seller_id={foreign.id}").status_code, 404)

    def test_cash_shift_is_materialized_with_breakdown_and_shortage(self):
        manager_user = get_user_model().objects.create_user(username="gerente-caixa@example.test",
                                                            email="gerente-caixa@example.test", password="Synthetic-45!")
        manager = Membership.objects.create(tenant=self.tenant, user=manager_user, name="Gerente", role="manager")
        token = "terminal-token-" + uuid.uuid4().hex
        terminal = Terminal.objects.create(tenant=self.tenant, shop=self.shop, name="Caixa 1",
            device_id="cash-device", token_hash=hashlib.sha256(token.encode()).hexdigest())
        identity = {"tenant_id": str(self.tenant.id), "shop_id": str(self.shop.id), "terminal_id": str(terminal.id)}
        headers = {"HTTP_AUTHORIZATION": f"Bearer {token}"}
        now = int(time.time() * 1000)
        session_id = uuid.uuid4().hex

        def event(kind, aggregate, data):
            return {"event_id": uuid.uuid4().hex, "type": kind, "aggregate_id": aggregate,
                    "identity": identity, "data": data}

        def sale(payments, change):
            return event("sale.completed", str(uuid.uuid4()), {
                "request_id": uuid.uuid4().hex, "cash_session_id": session_id, "created_at": now,
                "customer_id": None, "customer_name": "", "customer_document": "",
                "operator_id": str(self.member.id), "gross_cents": 999, "discount_cents": 0,
                "total_cents": 999, "change_cents": change,
                "items": [{"product_id": str(self.product.id), "name": "Arroz", "unit": "UN",
                           "quantity_milli": 1000, "unit_price_cents": 999, "gross_cents": 999,
                           "discount_cents": 0, "line_total_cents": 999, "stock_controlled": True}],
                "payments": payments})

        events = [
            event("cash.opened", session_id, {"opening_cents": 5000, "operator_id": str(self.member.id), "created_at": now}),
            sale([{"method": "cash", "amount_cents": 1000}], 1),
            sale([{"method": "pix", "amount_cents": 999}], 0),
            event("cash.withdrawn", uuid.uuid4().hex, {"cash_session_id": session_id, "amount_cents": 2000,
                  "reason": "Depósito", "operator_id": str(self.member.id), "created_at": now + 1}),
            event("cash.closed", session_id, {"counted_cents": 3900, "expected_cents": 3999, "difference_cents": -99,
                  "created_at": now + 2, "operator_id": str(self.member.id), "supervisor_id": str(manager.id)}),
            # Movement whose opening never reached the cloud must not block the queue.
            event("cash.supplied", uuid.uuid4().hex, {"cash_session_id": uuid.uuid4().hex, "amount_cents": 100,
                  "reason": "Troco", "operator_id": str(self.member.id), "created_at": now}),
        ]
        response = self.api("POST", "/api/v1/terminal/sync", {"events": events}, tenant=False, **headers)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(self.api("POST", "/api/v1/terminal/sync", {"events": events}, tenant=False,
                                  **headers).status_code, 200)

        self.client.force_login(self.owner)
        listing = self.api("GET", "/api/v1/cash-sessions?status=shortage").json()["results"]
        self.assertEqual(len(listing), 1)
        row = listing[0]
        self.assertEqual((row["operator_name"], row["closed_by_name"], row["supervisor_name"]), ("Dona", "Dona", "Gerente"))
        self.assertEqual((row["sales_count"], row["sales_total_cents"], row["withdrawn_cents"]), (2, 1998, 2000))
        self.assertEqual(row["by_method"], {"cash": 999, "pix": 999})
        self.assertEqual(row["difference_cents"], -99)
        detail = self.api("GET", f"/api/v1/cash-sessions/{row['id']}").json()
        self.assertEqual([m["reason"] for m in detail["movements"]], ["Depósito"])
        orphan = [r for r in self.api("GET", "/api/v1/cash-sessions?status=open").json()["results"]]
        self.assertEqual(len(orphan), 1)
        self.assertTrue(orphan[0]["incomplete"])
        self.assertEqual(self.api("GET", "/api/v1/cash-sessions", tenant=self.other_tenant.id).status_code, 403)

    def test_commission_accrues_by_category_reverses_and_pays(self):
        from .models import Category, Commission, FinanceEntry
        seller_user = get_user_model().objects.create_user(username="comissao@example.test",
                                                           email="comissao@example.test", password="Synthetic-46!")
        seller = Membership.objects.create(tenant=self.tenant, user=seller_user, name="Vera", role="cashier",
                                           commission_bps=500)
        drinks = Category.objects.create(tenant=self.tenant, name="Bebidas", commission_bps=1000)
        soda = Product.objects.create(tenant=self.tenant, sku="REFRI", name="Refri", price_cents=1000, category=drinks)
        token = "terminal-token-" + uuid.uuid4().hex
        terminal = Terminal.objects.create(tenant=self.tenant, shop=self.shop, name="Caixa",
            device_id="commission-device", token_hash=hashlib.sha256(token.encode()).hexdigest())
        identity = {"tenant_id": str(self.tenant.id), "shop_id": str(self.shop.id), "terminal_id": str(terminal.id)}
        headers = {"HTTP_AUTHORIZATION": f"Bearer {token}"}

        def item(product, price):
            return {"product_id": str(product.id), "name": product.name, "unit": "UN", "quantity_milli": 1000,
                    "unit_price_cents": price, "gross_cents": price, "discount_cents": 0, "line_total_cents": price}

        def sale_event():
            return {"event_id": uuid.uuid4().hex, "type": "sale.completed", "aggregate_id": str(uuid.uuid4()),
                    "identity": identity, "data": {
                        "request_id": uuid.uuid4().hex, "cash_session_id": uuid.uuid4().hex,
                        "created_at": int(time.time() * 1000), "customer_id": None, "customer_name": "",
                        "customer_document": "", "operator_id": str(self.member.id), "seller_id": str(seller.id),
                        "gross_cents": 1999, "discount_cents": 0, "total_cents": 1999, "change_cents": 0,
                        "items": [item(self.product, 999), item(soda, 1000)],
                        "payments": [{"method": "pix", "amount_cents": 1999}]}}

        first, second, third = sale_event(), sale_event(), sale_event()
        self.assertEqual(self.api("POST", "/api/v1/terminal/sync", {"events": [first, second, third]},
                                  tenant=False, **headers).status_code, 200)
        accrued = Commission.objects.get(sale_id=first["aggregate_id"])
        # 5% of 9.99 (seller rate) + 10% of 10.00 (category rate) = 0.50 + 1.00
        self.assertEqual((accrued.base_cents, accrued.amount_cents, accrued.seller_id), (1999, 150, seller.id))

        self.client.force_login(self.owner)
        self.assertEqual(self.api("GET", "/api/v1/commissions/summary").status_code, 403)  # Essencial sem financeiro
        Subscription.objects.filter(tenant=self.tenant).update(plan=self.management)

        self.assertEqual(self.api("POST", f"/api/v1/sales/{first['aggregate_id']}/cancel",
                                  {"reason": "Cliente desistiu"}).status_code, 200)
        self.assertEqual(Commission.objects.get(pk=accrued.pk).status, "cancelled")

        paid = self.api("POST", "/api/v1/commissions/pay", {"seller_id": str(seller.id), "due_date": "2026-11-05"})
        self.assertEqual(paid.status_code, 201, paid.content)
        self.assertEqual((paid.json()["amount_cents"], paid.json()["count"]), (300, 2))
        entry = FinanceEntry.objects.get(pk=paid.json()["finance_entry_id"])
        self.assertEqual((entry.kind, entry.status, entry.amount_cents), ("payable", "open", 300))
        self.assertEqual(self.api("POST", "/api/v1/commissions/pay",
                                  {"seller_id": str(seller.id), "due_date": "2026-11-05"}).status_code, 409)

        self.assertEqual(self.api("POST", f"/api/v1/sales/{second['aggregate_id']}/cancel",
                                  {"reason": "Devolvido"}).status_code, 200)
        reversal = Commission.objects.get(sale_id=second["aggregate_id"], reversal=True)
        self.assertEqual((reversal.amount_cents, reversal.status), (-150, "pending"))
        summary = self.api("GET", "/api/v1/commissions/summary").json()["results"]
        self.assertEqual([(r["seller_name"], r["sales"], r["pending_cents"], r["paid_cents"]) for r in summary],
                         [("Vera", 2, -150, 300)])
        self.assertEqual(self.api("PATCH", f"/api/v1/users/{seller.id}", {"commission_bps": 10001}).status_code, 400)
        self.assertEqual(self.api("PATCH", f"/api/v1/users/{seller.id}", {"commission_bps": 250}).json()["commission_bps"], 250)

    def test_reports_compute_totals_abc_restock_and_export_csv(self):
        from .models import SaleItem
        Product.objects.filter(pk=self.product.pk).update(cost_cents=600, stock_minimum_milli=5000)
        cheap = Product.objects.create(tenant=self.tenant, sku="SAL", name="Sal", price_cents=100, cost_cents=50)
        StockMovement.objects.create(tenant=self.tenant, shop=self.shop, product=self.product, delta_milli=3000,
                                     source="manual", reason="Inventário")
        StockMovement.objects.create(tenant=self.tenant, shop=self.shop, product=cheap, delta_milli=50000,
                                     source="manual", reason="Inventário")
        StockMovement.objects.create(tenant=self.tenant, shop=self.shop, product=cheap, delta_milli=-2000,
                                     source="manual", reason="Avaria")

        def sale(lines, payments, change=0, status="completed"):
            total = sum(price * qty // 1000 for _, price, qty in lines)
            row = Sale.objects.create(id=uuid.uuid4(), tenant=self.tenant, shop=self.shop, request_id=uuid.uuid4().hex,
                                      content_hash="x", gross_cents=total, total_cents=total, change_cents=change,
                                      status=status, created_at=timezone.now(), operator_name_snapshot="Dona")
            for n, (product, price, qty) in enumerate(lines):
                SaleItem.objects.create(sale=row, line_number=n, product=product, name=product.name, unit="UN",
                                        quantity_milli=qty, unit_price_cents=price, gross_cents=price * qty // 1000,
                                        discount_cents=0, line_total_cents=price * qty // 1000)
            for n, (method, amount) in enumerate(payments):
                SalePayment.objects.create(sale=row, line_number=n, method=method, amount_cents=amount)
            return row

        sale([(self.product, 999, 2000)], [("cash", 2000)], change=2)
        sale([(self.product, 999, 1000), (cheap, 100, 1000)], [("pix", 1099)])
        sale([(self.product, 999, 1000)], [("pix", 999)], status="cancelled")

        self.client.force_login(self.owner)
        self.assertEqual(self.api("GET", "/api/v1/reports/sales_by_day").status_code, 403)
        Subscription.objects.filter(tenant=self.tenant).update(plan=self.management)

        day = self.api("GET", "/api/v1/reports/sales_by_day").json()
        self.assertEqual((day["totals"]["count"], day["totals"]["total"]), (2, 3097))
        payment = {r["method"]: r["total"] for r in self.api("GET", "/api/v1/reports/sales_by_payment").json()["rows"]}
        self.assertEqual(payment, {"Dinheiro": 1998, "Pix": 1099})
        abc = self.api("GET", "/api/v1/reports/products_abc").json()
        self.assertEqual([(r["sku"], r["abc"], r["profit"]) for r in abc["rows"]], [("ARROZ", "A", 2997 - 1800), ("SAL", "C", 50)])
        operator = self.api("GET", "/api/v1/reports/sales_by_operator").json()["rows"]
        self.assertEqual([(r["person"], r["count"]) for r in operator], [("Dona", 2)])
        restock = {r["sku"]: r for r in self.api("GET", "/api/v1/reports/low_stock").json()["rows"]}
        self.assertEqual(set(restock), {"ARROZ"})
        self.assertEqual((restock["ARROZ"]["balance"], restock["ARROZ"]["suggested"]), (3000, 2000))
        losses = self.api("GET", "/api/v1/reports/stock_losses").json()["rows"]
        self.assertEqual([(r["reason"], r["qty"], r["cost"]) for r in losses], [("Avaria", 2000, 100)])
        self.assertEqual(self.api("GET", "/api/v1/reports/cash_flow").json()["totals"]["sales"], 3097)

        csv_response = self.api("GET", "/api/v1/reports/sales_by_day?format=csv")
        body = csv_response.content.decode("utf-8")
        self.assertTrue(body.startswith("\ufeffDia;Vendas;Faturamento"))
        self.assertIn("Total;2;30,97", body)
        self.assertEqual(self.api("GET", "/api/v1/reports/nope").status_code, 404)
        self.assertEqual(self.api("GET", "/api/v1/reports/sales_by_day?from=2026-01-01&to=2025-01-01").status_code, 400)

    def test_fiado_store_credit_and_partial_returns(self):
        from .models import Customer, CustomerCredit, FinanceEntry, SaleReturn
        customer = Customer.objects.create(tenant=self.tenant, name="Dona Rosa", credit_limit_cents=10000)
        token = "terminal-token-" + uuid.uuid4().hex
        terminal = Terminal.objects.create(tenant=self.tenant, shop=self.shop, name="Caixa",
            device_id="fiado-device", token_hash=hashlib.sha256(token.encode()).hexdigest())
        identity = {"tenant_id": str(self.tenant.id), "shop_id": str(self.shop.id), "terminal_id": str(terminal.id)}
        headers = {"HTTP_AUTHORIZATION": f"Bearer {token}"}
        session_id = uuid.uuid4().hex
        now = int(time.time() * 1000)

        def sync(*events, status=200):
            response = self.api("POST", "/api/v1/terminal/sync", {"events": list(events)}, tenant=False, **headers)
            self.assertEqual(response.status_code, status, response.content)
            return response

        def sale(payments, qty=2, customer_id=str(customer.id)):
            total = 999 * qty
            return {"event_id": uuid.uuid4().hex, "type": "sale.completed", "aggregate_id": str(uuid.uuid4()),
                    "identity": identity, "data": {
                        "request_id": uuid.uuid4().hex, "cash_session_id": session_id, "created_at": now,
                        "customer_id": customer_id, "customer_name": "", "customer_document": "",
                        "operator_id": str(self.member.id), "gross_cents": total, "discount_cents": 0,
                        "total_cents": total, "change_cents": 0,
                        "items": [{"product_id": str(self.product.id), "name": "Arroz", "unit": "UN",
                                   "quantity_milli": qty * 1000, "unit_price_cents": 999, "gross_cents": total,
                                   "discount_cents": 0, "line_total_cents": total}],
                        "payments": [{"method": m, "amount_cents": a} for m, a in payments]}}

        def returned(sale_event, qty, refund):
            return {"event_id": uuid.uuid4().hex, "type": "sale.returned", "aggregate_id": str(uuid.uuid4()),
                    "identity": identity, "data": {
                        "sale_id": sale_event["aggregate_id"], "refund_method": refund, "reason": "Produto avariado",
                        "operator_id": str(self.member.id), "cash_session_id": session_id, "created_at": now + 1,
                        "items": [{"product_id": str(self.product.id), "quantity_milli": qty * 1000, "amount_cents": 999 * qty}],
                        "total_cents": 999 * qty}}

        # Fiado: one receivable per credit-account payment, due after the tenant term.
        fiado = sale([("credit_account", 1998)])
        sync(fiado)
        entry = FinanceEntry.objects.get(sale_id=fiado["aggregate_id"])
        self.assertEqual((entry.kind, entry.status, entry.amount_cents, entry.customer_id), ("receivable", "open", 1998, customer.id))
        self.assertEqual((entry.due_date - timezone.localdate()).days, 30)
        sync(sale([("credit_account", 999)], qty=1, customer_id=None), status=400)

        # Partial return abates the open fiado; a later return after payment becomes store credit.
        sync(returned(fiado, 1, "account"))
        entry.refresh_from_db()
        self.assertEqual(entry.amount_cents, 999)
        self.client.force_login(self.owner)
        statement = self.api("GET", f"/api/v1/customers/{customer.id}/statement").json()
        self.assertEqual((statement["open_credit_cents"], statement["available_credit_cents"]), (999, 9001))
        received = self.api("POST", f"/api/v1/customers/{customer.id}/receive", {"entry_ids": [str(entry.id)]})
        self.assertEqual(received.json()["received_cents"], 999)
        sync(returned(fiado, 1, "account"))
        self.assertEqual(sum(CustomerCredit.objects.filter(customer=customer).values_list("delta_cents", flat=True)), 999)
        # Nothing left to return: the counter already refunded, so it is accepted and flagged for the admin.
        over = sync(returned(fiado, 1, "account"))
        self.assertEqual([c["reason"] for c in over.json()["conflicts"]], ["return_exceeds_sold"])

        # Store credit pays a later sale; a cash return leaves the drawer.
        paid_with_credit = sale([("store_credit", 999)], qty=1)
        pix = sale([("pix", 1998)])
        sync(paid_with_credit, pix, returned(pix, 1, "cash"))
        summary = self.api("GET", "/api/v1/customers/credit-summary").json()["results"]
        # Fiado fully received; the accepted over-return abated a paid fiado, so it became store credit.
        self.assertEqual([(r["open_credit_cents"], r["store_credit_cents"]) for r in summary], [(0, 999)])
        self.assertEqual(StockMovement.objects.filter(source="return").count(), 4)  # incl. the flagged over-return
        self.assertEqual(self.api("POST", f"/api/v1/sales/{pix['aggregate_id']}/cancel",
                                  {"reason": "Teste"}).status_code, 409)
        self.assertEqual(SaleReturn.objects.filter(refund_method="cash").count(), 1)

        # Cancelling the credit-paid sale gives the credit back; terminals get balances on bootstrap.
        self.assertEqual(self.api("POST", f"/api/v1/sales/{paid_with_credit['aggregate_id']}/cancel",
                                  {"reason": "Desistiu"}).status_code, 200)
        private, public = generate_keypair()
        with patch.dict(os.environ, {"PDV_LICENSE_PRIVATE_KEY": private, "PDV_LICENSE_PUBLIC_KEY": public,
                                    "PDV_LICENSE_PRIVATE_KEY_FILE": "", "PDV_LICENSE_PUBLIC_KEY_FILE": ""}):
            boot = self.api("GET", "/api/v1/terminal/bootstrap", tenant=False, **headers).json()
        mine = next(c for c in boot["customers"] if c["id"] == str(customer.id))
        self.assertEqual((mine["credit_limit_cents"], mine["open_credit_cents"], mine["store_credit_cents"]), (10000, 0, 1998))
        Subscription.objects.filter(tenant=self.tenant).update(plan=self.management)
        another = sale([("credit_account", 999)], qty=1)
        sync(another)
        open_entry = FinanceEntry.objects.get(sale_id=another["aggregate_id"])
        self.assertEqual(self.api("PATCH", f"/api/v1/finance/{open_entry.id}", {"amount_cents": 1}).status_code, 409)
        self.assertEqual(self.api("PATCH", f"/api/v1/finance/{open_entry.id}", {"due_date": "2026-12-20"}).status_code, 200)
        from .stock_balance import balance, drift
        self.assertEqual(drift(self.tenant), [])
        self.assertEqual(balance(self.tenant, self.shop, self.product),
                         StockMovement.objects.filter(product=self.product).aggregate(v=Sum("delta_milli"))["v"])

    def test_reconcile_stock_reports_and_fixes_drift(self):
        from io import StringIO
        from django.core.management import call_command
        from .models import StockBalance
        StockMovement.objects.create(tenant=self.tenant, shop=self.shop, product=self.product, delta_milli=4000,
                                     source="manual", reason="Inventário")
        StockMovement.objects.create(tenant=self.tenant, shop=self.shop, product=self.product, delta_milli=-1500,
                                     source="manual", reason="Avaria")
        row = StockBalance.objects.get(shop=self.shop, product=self.product)
        self.assertEqual(row.quantity_milli, 2500)
        StockBalance.objects.filter(pk=row.pk).update(quantity_milli=99)
        with self.assertRaises(SystemExit):
            call_command("reconcile_stock", stdout=StringIO())
        out = StringIO()
        call_command("reconcile_stock", "--fix", stdout=out)
        self.assertIn("corrigida", out.getvalue())
        self.assertEqual(StockBalance.objects.get(pk=row.pk).quantity_milli, 2500)
        call_command("reconcile_stock", stdout=StringIO())  # clean now: no SystemExit

    def test_payment_methods_config_custom_sale_and_net_report(self):
        from .models import PaymentMethod
        self.client.force_login(self.owner)
        rows = {r["kind"]: r for r in self.api("GET", "/api/v1/payment-methods").json()["results"]}
        self.assertEqual(set(rows), {"cash", "pix", "card_credit", "card_debit", "credit_account", "store_credit"})
        self.assertEqual(self.api("PATCH", f"/api/v1/payment-methods/{rows['cash']['id']}", {"active": False}).status_code, 409)
        self.assertEqual(self.api("PATCH", f"/api/v1/payment-methods/{rows['card_credit']['id']}",
                                  {"fee_bps": 350, "settlement_days": 30}).json()["fee_bps"], 350)
        voucher = self.api("POST", "/api/v1/payment-methods", {"name": "Vale-refeição", "fee_bps": 600})
        self.assertEqual(voucher.status_code, 201, voucher.content)
        voucher = voucher.json()
        foreign = PaymentMethod.objects.create(tenant=self.other_tenant, kind="other", name="Alheio")
        token = "terminal-token-" + uuid.uuid4().hex
        terminal = Terminal.objects.create(tenant=self.tenant, shop=self.shop, name="Caixa",
            device_id="payment-device", token_hash=hashlib.sha256(token.encode()).hexdigest())
        identity = {"tenant_id": str(self.tenant.id), "shop_id": str(self.shop.id), "terminal_id": str(terminal.id)}
        headers = {"HTTP_AUTHORIZATION": f"Bearer {token}"}

        def sale(payments):
            return {"event_id": uuid.uuid4().hex, "type": "sale.completed", "aggregate_id": str(uuid.uuid4()),
                    "identity": identity, "data": {
                        "request_id": uuid.uuid4().hex, "cash_session_id": uuid.uuid4().hex,
                        "created_at": int(time.time() * 1000), "customer_id": None, "customer_name": "",
                        "customer_document": "", "gross_cents": 1000, "discount_cents": 0, "total_cents": 1000,
                        "change_cents": 0, "items": [{"product_id": str(self.product.id), "name": "Arroz", "unit": "UN",
                        "quantity_milli": 1000, "unit_price_cents": 1000, "gross_cents": 1000, "discount_cents": 0,
                        "line_total_cents": 1000}], "payments": payments}}

        Product.objects.filter(pk=self.product.pk).update(price_cents=1000)
        # Deactivated after the offline sale happened: still accepted.
        self.api("PATCH", f"/api/v1/payment-methods/{voucher['id']}", {"active": False})
        ok = sale([{"method": "other", "payment_method_id": voucher["id"], "amount_cents": 1000}])
        card = sale([{"method": "card_credit", "amount_cents": 1000}])
        sync = lambda e: self.api("POST", "/api/v1/terminal/sync", {"events": [e]}, tenant=False, **headers)
        self.assertEqual(sync(ok).status_code, 200, sync(ok).content)
        self.assertEqual(sync(card).status_code, 200)
        self.assertEqual(sync(sale([{"method": "other", "payment_method_id": str(foreign.id), "amount_cents": 1000}])).status_code, 404)
        self.assertEqual(sync(sale([{"method": "pix", "payment_method_id": voucher["id"], "amount_cents": 1000}])).status_code, 400)
        detail = self.api("GET", f"/api/v1/sales/{ok['aggregate_id']}").json()
        self.assertEqual(detail["payments"][0]["name"], "Vale-refeição")
        Subscription.objects.filter(tenant=self.tenant).update(plan=self.management)
        report = {r["method"]: r for r in self.api("GET", "/api/v1/reports/sales_by_payment").json()["rows"]}
        self.assertEqual((report["Vale-refeição"]["fee"], report["Vale-refeição"]["net"]), (60, 940))
        self.assertEqual((report["Crédito"]["fee"], report["Crédito"]["net"]), (35, 965))

    def test_product_group_generates_and_syncs_variants(self):
        self.client.force_login(self.owner)
        attrs = [{"name": "Cor", "values": ["Azul", "Preto"]}, {"name": "Tamanho", "values": ["P", "M", "G"]}]
        created = self.api("POST", "/api/v1/product-groups", {"name": "Camiseta Básica", "attributes": attrs, "price_cents": 4990})
        self.assertEqual(created.status_code, 201, created.content)
        group = created.json()
        variants = group["variants"]
        self.assertEqual(len(variants), 6)
        self.assertEqual(len({v["sku"] for v in variants}), 6)
        self.assertEqual(len({v["barcode"] for v in variants}), 6)
        self.assertIn("Camiseta Básica - Azul / M", {v["name"] for v in variants})
        self.assertEqual(self.api("POST", "/api/v1/product-groups", {"name": "Camiseta Básica", "attributes": attrs,
                                                                     "price_cents": 1}).status_code, 409)
        attrs[1]["values"] = ["P", "M", "GG"]  # G removed, GG added
        updated = self.api("PATCH", f"/api/v1/product-groups/{group['id']}", {"attributes": attrs, "price_cents": 5490,
                                                                               "apply_price_to_all": True}).json()
        active = {v["variant"]: v for v in updated["variants"] if v["active"]}
        self.assertEqual(set(active), {"Azul / P", "Azul / M", "Azul / GG", "Preto / P", "Preto / M", "Preto / GG"})
        self.assertEqual({v["price_cents"] for v in active.values()}, {5490})
        self.assertEqual(len(updated["variants"]), 8)  # G variants kept, inactive, for history
        too_big = [{"name": "A", "values": [str(i) for i in range(30)]}, {"name": "B", "values": [str(i) for i in range(10)]}]
        self.assertEqual(self.api("POST", "/api/v1/product-groups", {"name": "Grande", "attributes": too_big,
                                                                     "price_cents": 1}).status_code, 400)

    def test_nfe_import_maps_creates_converts_and_creates_payables(self):
        from .models import FinanceEntry, StockReceipt, Supplier, SupplierProductCode
        from .nfe import parse
        from .stock_balance import balance
        Product.objects.filter(pk=self.product.pk).update(barcode="7891000100103")
        key = "33261012345678000195550010000012341000012345"

        def item(n, code, ean, name, unit, qty, price, extra=""):
            return (f'<det nItem="{n}"><prod><cProd>{code}</cProd><cEAN>{ean}</cEAN><xProd>{name}</xProd>'
                    f'<NCM>10063021</NCM><uCom>{unit}</uCom><qCom>{qty}</qCom><vUnCom>{price}</vUnCom>'
                    f'<vProd>{float(qty) * float(price):.2f}</vProd><cEANTrib>{ean}</cEANTrib>{extra}</prod>'
                    '<imposto><ICMS><ICMS10><vICMSST>1.50</vICMSST></ICMS10></ICMS><IPI><IPITrib><vIPI>0.50</vIPI>'
                    '</IPITrib></IPI></imposto></det>')

        def xml(nf_key, number):
            return (f'<?xml version="1.0" encoding="UTF-8"?><nfeProc xmlns="http://www.portalfiscal.inf.br/nfe">'
                    f'<NFe><infNFe Id="NFe{nf_key}"><ide><mod>55</mod><serie>1</serie><nNF>{number}</nNF>'
                    '<dhEmi>2026-10-20T10:00:00-03:00</dhEmi></ide><emit><CNPJ>12345678000195</CNPJ>'
                    '<xNome>Distribuidora Exemplo Ltda</xNome></emit><dest><CNPJ>99999999000199</CNPJ></dest>'
                    + item(1, "ARZ-5KG", "7891000100103", "ARROZ TIPO 1 FD 12X1KG", "FD", "2", "60.00", "<vDesc>10.00</vDesc>")
                    + item(2, "FEI-1", "SEM GTIN", "FEIJAO CARIOCA 1KG", "UN", "10", "8.00")
                    + item(3, "BRINDE", "SEM GTIN", "BRINDE CANETA", "UN", "1", "0.01")
                    + '<total><ICMSTot><vNF>216.01</vNF></ICMSTot></total><cobr>'
                    '<dup><nDup>001</nDup><dVenc>2026-11-20</dVenc><vDup>108.00</vDup></dup>'
                    '<dup><nDup>002</nDup><dVenc>2026-12-20</dVenc><vDup>108.01</vDup></dup></cobr>'
                    '</infNFe></NFe><protNFe><infProt><chNFe>' + nf_key + '</chNFe><nProt>133260000000001</nProt></infProt></protNFe></nfeProc>')

        parsed = parse(xml(key, 1234))
        # 120.00 - 10.00 discount + 1.50 ST + 0.50 IPI
        self.assertEqual([i["total_cost_cents"] for i in parsed["items"]], [11200, 8200, 201])
        self.assertEqual([d["amount_cents"] for d in parsed["installments"]], [10800, 10801])
        with self.assertRaises(APIError):
            parse('<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "b">]><x>&a;</x>')

        self.client.force_login(self.owner)
        preview = self.api("POST", "/api/v1/purchases/nfe/preview", {"xml": xml(key, 1234)}).json()
        self.assertIsNone(preview["supplier_id"])
        self.assertEqual([i["suggestion"]["source"] for i in preview["items"]], ["barcode", None, None])
        lines = [{"n_item": 1, "action": "map", "product_id": str(self.product.id), "factor_milli": 12000},
                 {"n_item": 2, "action": "new", "factor_milli": 1000, "new_product": {"price_cents": 1190}},
                 {"n_item": 3, "action": "skip"}]
        bad = self.api("POST", "/api/v1/purchases/nfe/import",
                       {"xml": xml(key, 1234), "shop_id": str(self.shop.id), "lines": lines[:2]})
        self.assertEqual(bad.status_code, 400)  # every item needs a decision
        done = self.api("POST", "/api/v1/purchases/nfe/import", {"xml": xml(key, 1234), "shop_id": str(self.shop.id), "lines": lines})
        self.assertEqual(done.status_code, 201, done.content)
        self.assertEqual(balance(self.tenant, self.shop, self.product), 24000)  # 2 fardos x 12
        self.product.refresh_from_db()
        self.assertEqual(self.product.cost_cents, 467)  # 112.00 / 24 un
        beans = Product.objects.get(tenant=self.tenant, name="FEIJAO CARIOCA 1KG")
        self.assertEqual((beans.price_cents, beans.cost_cents, beans.ncm), (1190, 820, "10063021"))
        supplier = Supplier.objects.get(tenant=self.tenant, document="12345678000195")
        payables = FinanceEntry.objects.filter(supplier=supplier, kind="payable").order_by("due_date")
        self.assertEqual([p.amount_cents for p in payables], [10800, 10801])
        self.assertEqual(StockReceipt.objects.get(pk=done.json()["receipt_id"]).reference, "NF-e 1234 série 1")
        self.assertEqual(self.api("POST", "/api/v1/purchases/nfe/import",
                                  {"xml": xml(key, 1234), "shop_id": str(self.shop.id), "lines": lines}).status_code, 409)
        self.assertTrue(self.api("POST", "/api/v1/purchases/nfe/preview", {"xml": xml(key, 1234)}).json()["already_imported"])
        # Next note from the same supplier: codes are remembered with their conversion factor.
        again = self.api("POST", "/api/v1/purchases/nfe/preview", {"xml": xml(key[:-4] + "9999", 1300)}).json()
        self.assertEqual(again["supplier_id"], str(supplier.id))
        self.assertEqual([(i["suggestion"]["source"], i["suggestion"]["factor_milli"]) for i in again["items"][:2]],
                         [("memory", 12000), ("memory", 1000)])
        self.assertEqual(SupplierProductCode.objects.filter(supplier=supplier).count(), 2)

    def test_inventory_count_blind_mobile_and_adjusts_only_true_difference(self):
        from .stock_balance import balance
        from .models import Category
        Product.objects.filter(pk=self.product.pk).update(barcode="7891000100103", cost_cents=500)
        salt = Product.objects.create(tenant=self.tenant, sku="SAL", name="Sal", price_cents=300, cost_cents=200, barcode="7891000100110")
        drinks = Category.objects.create(tenant=self.tenant, name="Bebidas")
        Product.objects.create(tenant=self.tenant, sku="REF", name="Refri", price_cents=900, category=drinks)
        for product, qty in ((self.product, 10000), (salt, 5000)):
            StockMovement.objects.create(tenant=self.tenant, shop=self.shop, product=product, delta_milli=qty,
                                         source="manual", reason="Saldo inicial")
        cashier_user = get_user_model().objects.create_user(username="contador@example.test", email="contador@example.test",
                                                            password="Synthetic-47!")
        Membership.objects.create(tenant=self.tenant, user=cashier_user, name="Contador", role="cashier")
        self.client.force_login(self.owner)
        opened = self.api("POST", "/api/v1/inventories", {"shop_id": str(self.shop.id)})
        self.assertEqual(opened.status_code, 201, opened.content)
        count_id = opened.json()["id"]
        self.assertEqual(self.api("POST", "/api/v1/inventories", {"shop_id": str(self.shop.id)}).status_code, 409)
        counter = Client()
        counter.force_login(cashier_user)
        scan = lambda body: self.api("POST", f"/api/v1/inventories/{count_id}/count", body, client=counter)
        for _ in range(3):  # one scan per unit
            first = scan({"code": "7891000100103", "quantity_milli": 1000})
        self.assertEqual(first.json()["counted_milli"], 3000)
        self.assertNotIn("difference_milli", first.json())  # blind: counter never sees the expected balance
        scan({"code": "7891000100103", "quantity_milli": 8000, "mode": "set"})
        self.assertEqual(scan({"code": "0000", "quantity_milli": 1000}).status_code, 404)
        # A sale after the rice was counted must not become a fake loss.
        StockMovement.objects.create(tenant=self.tenant, shop=self.shop, product=self.product, delta_milli=-1000,
                                     source="manual", reason="Venda depois da contagem")
        detail = self.api("GET", f"/api/v1/inventories/{count_id}").json()
        self.assertEqual([(i["sku"], i["difference_milli"], i["difference_cents"]) for i in detail["items"]], [("ARROZ", -2000, -1000)])
        self.assertEqual({p["sku"] for p in detail["pending"]}, {"SAL", "REF"})
        self.assertEqual(self.api("POST", f"/api/v1/inventories/{count_id}/close", {}, client=counter).status_code, 403)
        closed = self.api("POST", f"/api/v1/inventories/{count_id}/close", {"uncounted": "zero"}).json()
        self.assertEqual((closed["status"], closed["adjustments"], closed["loss_cents"]), ("closed", 2, -2000))
        self.assertEqual(balance(self.tenant, self.shop, self.product), 7000)  # 8 counted − 1 sold afterwards
        self.assertEqual(balance(self.tenant, self.shop, salt), 0)  # not counted, zeroed on request
        self.assertEqual(scan({"code": "7891000100110", "quantity_milli": 1000}).status_code, 409)

    def test_transfer_send_partial_receive_and_cancel(self):
        from .stock_balance import balance
        branch = Shop.objects.create(tenant=self.tenant, name="Filial")
        StockMovement.objects.create(tenant=self.tenant, shop=self.shop, product=self.product, delta_milli=10000,
                                     source="manual", reason="Saldo inicial")
        self.client.force_login(self.owner)
        body = lambda qty, key: {"from_shop_id": str(self.shop.id), "to_shop_id": str(branch.id), "idempotency_key": key,
                                 "items": [{"product_id": str(self.product.id), "quantity_milli": qty}]}
        self.assertEqual(self.api("POST", "/api/v1/transfers", body(11000, "t0")).status_code, 409)
        sent = self.api("POST", "/api/v1/transfers", body(4000, "t1"))
        self.assertEqual(sent.status_code, 201, sent.content)
        self.assertEqual(self.api("POST", "/api/v1/transfers", body(4000, "t1")).json()["id"], sent.json()["id"])  # replay
        self.assertEqual((balance(self.tenant, self.shop, self.product), balance(self.tenant, branch, self.product)), (6000, 0))
        got = self.api("POST", f"/api/v1/transfers/{sent.json()['id']}/receive",
                       {"items": [{"product_id": str(self.product.id), "received_milli": 3000}]}).json()
        self.assertEqual((got["status"], got["items"][0]["received_milli"]), ("received", 3000))
        self.assertEqual(balance(self.tenant, branch, self.product), 3000)
        self.assertEqual(self.api("POST", f"/api/v1/transfers/{sent.json()['id']}/cancel").status_code, 409)
        other = self.api("POST", "/api/v1/transfers", body(2000, "t2")).json()
        self.assertEqual(self.api("POST", f"/api/v1/transfers/{other['id']}/cancel").json()["status"], "cancelled")
        self.assertEqual(balance(self.tenant, self.shop, self.product), 6000)

    def test_fiado_received_at_terminal_settles_oldest_and_splits(self):
        from .models import CashSession, Customer, CustomerCredit, FinanceEntry
        from .cash_logic import session_summary
        customer = Customer.objects.create(tenant=self.tenant, name="Seu Zé", credit_limit_cents=50000)
        token = "terminal-token-" + uuid.uuid4().hex
        terminal = Terminal.objects.create(tenant=self.tenant, shop=self.shop, name="Caixa",
            device_id="receive-device", token_hash=hashlib.sha256(token.encode()).hexdigest())
        identity = {"tenant_id": str(self.tenant.id), "shop_id": str(self.shop.id), "terminal_id": str(terminal.id)}
        headers = {"HTTP_AUTHORIZATION": f"Bearer {token}"}
        session_id = uuid.uuid4().hex
        now = int(time.time() * 1000)
        sync = lambda *events: self.api("POST", "/api/v1/terminal/sync", {"events": list(events)}, tenant=False, **headers)

        def fiado(cents):
            Product.objects.filter(pk=self.product.pk).update(price_cents=cents)
            return {"event_id": uuid.uuid4().hex, "type": "sale.completed", "aggregate_id": str(uuid.uuid4()),
                    "identity": identity, "data": {"request_id": uuid.uuid4().hex, "cash_session_id": session_id,
                    "created_at": now, "customer_id": str(customer.id), "customer_name": "", "customer_document": "",
                    "gross_cents": cents, "discount_cents": 0, "total_cents": cents, "change_cents": 0,
                    "items": [{"product_id": str(self.product.id), "name": "Arroz", "unit": "UN", "quantity_milli": 1000,
                               "unit_price_cents": cents, "gross_cents": cents, "discount_cents": 0, "line_total_cents": cents}],
                    "payments": [{"method": "credit_account", "amount_cents": cents}]}}

        def receipt(cents, method="cash"):
            return {"event_id": uuid.uuid4().hex, "type": "credit.received", "aggregate_id": str(uuid.uuid4()),
                    "identity": identity, "data": {"customer_id": str(customer.id), "amount_cents": cents, "method": method,
                    "operator_id": str(self.member.id), "cash_session_id": session_id, "created_at": now + 5}}

        opened = {"event_id": uuid.uuid4().hex, "type": "cash.opened", "aggregate_id": session_id, "identity": identity,
                  "data": {"opening_cents": 0, "operator_id": str(self.member.id), "created_at": now}}
        self.assertEqual(sync(opened, fiado(1000), fiado(500)).status_code, 200)
        paid = receipt(1200)
        self.assertEqual(sync(paid).status_code, 200)
        self.assertEqual(sync(paid).status_code, 200)  # replay
        entries = FinanceEntry.objects.filter(customer=customer).order_by("amount_cents")
        self.assertEqual(sorted((e.amount_cents, e.status) for e in entries),
                         [(200, "settled"), (300, "open"), (1000, "settled")])  # 500 split: 200 paid, 300 open
        self.assertEqual(sync(receipt(500, "pix")).status_code, 200)  # 300 open + 200 above it
        self.assertEqual(sum(CustomerCredit.objects.filter(customer=customer).values_list("delta_cents", flat=True)), 200)
        summary = session_summary(CashSession.objects.get(pk=uuid.UUID(session_id)))
        self.assertEqual(summary["credit_received"], {"cash": 1200, "pix": 500})

    def test_panel_return_external_and_terminal_over_return_conflict(self):
        from .models import CustomerCredit, SaleReturn, SyncConflict
        from .stock_balance import balance
        token = "terminal-token-" + uuid.uuid4().hex
        terminal = Terminal.objects.create(tenant=self.tenant, shop=self.shop, name="Caixa",
            device_id="panel-return-device", token_hash=hashlib.sha256(token.encode()).hexdigest())
        identity = {"tenant_id": str(self.tenant.id), "shop_id": str(self.shop.id), "terminal_id": str(terminal.id)}
        headers = {"HTTP_AUTHORIZATION": f"Bearer {token}"}
        sale_id = str(uuid.uuid4())
        sale = {"event_id": uuid.uuid4().hex, "type": "sale.completed", "aggregate_id": sale_id, "identity": identity,
                "data": {"request_id": uuid.uuid4().hex, "cash_session_id": uuid.uuid4().hex, "created_at": int(time.time() * 1000),
                         "customer_id": None, "customer_name": "", "customer_document": "", "gross_cents": 1998,
                         "discount_cents": 0, "total_cents": 1998, "change_cents": 0,
                         "items": [{"product_id": str(self.product.id), "name": "Arroz", "unit": "UN", "quantity_milli": 2000,
                                    "unit_price_cents": 999, "gross_cents": 1998, "discount_cents": 0, "line_total_cents": 1998}],
                         "payments": [{"method": "pix", "amount_cents": 1998}]}}
        self.assertEqual(self.api("POST", "/api/v1/terminal/sync", {"events": [sale]}, tenant=False, **headers).status_code, 200)
        self.client.force_login(self.owner)
        item = lambda qty: [{"product_id": str(self.product.id), "quantity_milli": qty}]
        self.assertEqual(self.api("POST", f"/api/v1/sales/{sale_id}/return",
                                  {"items": item(1000), "refund_method": "store_credit", "reason": "Troca"}).status_code, 400)
        self.assertEqual(self.api("POST", f"/api/v1/sales/{sale_id}/return",
                                  {"items": item(1000), "refund_method": "cash", "reason": "x"}).status_code, 400)
        done = self.api("POST", f"/api/v1/sales/{sale_id}/return",
                        {"items": item(2000), "refund_method": "external", "reason": "Pix devolvido ao cliente"})
        self.assertEqual(done.status_code, 201, done.content)
        self.assertEqual(done.json()["total_cents"], 1998)
        ret = SaleReturn.objects.get(pk=done.json()["id"])
        self.assertIsNone(ret.terminal_id)
        self.assertEqual(ret.created_by, self.owner)
        self.assertFalse(CustomerCredit.objects.exists())
        self.assertEqual(balance(self.tenant, self.shop, self.product), 0)  # -2 sold +2 returned
        self.assertEqual(self.api("POST", f"/api/v1/sales/{sale_id}/return",
                                  {"items": item(1000), "refund_method": "external", "reason": "De novo"}).status_code, 409)
        # The same item returned offline at the counter afterwards: accepted, flagged as a conflict.
        late = {"event_id": uuid.uuid4().hex, "type": "sale.returned", "aggregate_id": str(uuid.uuid4()), "identity": identity,
                "data": {"sale_id": sale_id, "refund_method": "cash", "reason": "Avaria", "operator_id": str(self.member.id),
                         "created_at": int(time.time() * 1000), "total_cents": 999,
                         "items": [{"product_id": str(self.product.id), "quantity_milli": 1000, "amount_cents": 999}]}}
        response = self.api("POST", "/api/v1/terminal/sync", {"events": [late]}, tenant=False, **headers)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(SyncConflict.objects.get(event_id=late["event_id"]).reason, "return_exceeds_sold")

    def test_promotions_crud_sync_and_report(self):
        from .models import Promotion, SaleItem
        self.client.force_login(self.owner)
        bad = self.api("POST", "/api/v1/promotions", {"name": "X", "kind": "buy_x_pay_y", "product_id": str(self.product.id),
                                                      "buy_quantity": 2, "pay_quantity": 2})
        self.assertEqual(bad.status_code, 400)
        self.assertEqual(self.api("POST", "/api/v1/promotions", {"name": "X", "kind": "price", "product_id": str(self.product.id),
                                                                 "price_cents": 1200}).status_code, 400)  # not cheaper
        created = self.api("POST", "/api/v1/promotions", {"name": "Arroz 3x2", "kind": "buy_x_pay_y", "product_id": str(self.product.id),
                                                          "buy_quantity": 3, "pay_quantity": 2, "ends_at": "2099-01-01T00:00"})
        self.assertEqual(created.status_code, 201, created.content)
        self.assertEqual((created.json()["label"], created.json()["status"]), ("Leve 3 pague 2", "running"))
        promo = Promotion.objects.get(pk=created.json()["id"])
        self.tenant.max_discount_bps = 1000
        self.tenant.save(update_fields=["max_discount_bps"])
        token = "terminal-token-" + uuid.uuid4().hex
        terminal = Terminal.objects.create(tenant=self.tenant, shop=self.shop, name="Caixa",
            device_id="promo-device", token_hash=hashlib.sha256(token.encode()).hexdigest())
        identity = {"tenant_id": str(self.tenant.id), "shop_id": str(self.shop.id), "terminal_id": str(terminal.id)}

        def sale(promo_cents, discount):
            total = 2997 - promo_cents - discount
            return {"event_id": uuid.uuid4().hex, "type": "sale.completed", "aggregate_id": str(uuid.uuid4()), "identity": identity,
                    "data": {"request_id": uuid.uuid4().hex, "cash_session_id": uuid.uuid4().hex, "created_at": int(time.time() * 1000),
                             "customer_id": None, "customer_name": "", "customer_document": "", "gross_cents": 2997,
                             "promo_discount_cents": promo_cents, "discount_cents": discount, "total_cents": total, "change_cents": 0,
                             "items": [{"product_id": str(self.product.id), "name": "Arroz", "unit": "UN", "quantity_milli": 3000,
                                        "unit_price_cents": 999, "gross_cents": 2997, "promo_discount_cents": promo_cents,
                                        "promotion_id": str(promo.id), "promotion_name": "Arroz 3x2",
                                        "discount_cents": discount, "line_total_cents": total}],
                             "payments": [{"method": "cash", "amount_cents": total}]}}

        sync = lambda e: self.api("POST", "/api/v1/terminal/sync", {"events": [e]}, tenant=False, HTTP_AUTHORIZATION=f"Bearer {token}")
        ok = sale(999, 199)  # promo of 33% is fine; the 10% limit is checked on the 19.98 promotional price
        self.assertEqual(sync(ok).status_code, 200, sync(ok).content)
        self.assertEqual(sync(sale(999, 201)).status_code, 403)  # manual discount above 10% of the promo price
        line = SaleItem.objects.get(sale_id=ok["aggregate_id"])
        self.assertEqual((line.promo_discount_cents, line.promotion_id, line.line_total_cents), (999, promo.id, 1799))
        Subscription.objects.filter(tenant=self.tenant).update(plan=self.management)
        report = self.api("GET", "/api/v1/reports/promotions").json()
        self.assertEqual([(r["promotion"], r["uses"], r["discount"]) for r in report["rows"]], [("Arroz 3x2", 1, 999)])

    def test_terminal_heartbeat_updates_presence_without_sales(self):
        token = "terminal-token-" + uuid.uuid4().hex
        terminal = Terminal.objects.create(tenant=self.tenant, shop=self.shop, name="Caixa",
            device_id="heartbeat-device", token_hash=hashlib.sha256(token.encode()).hexdigest())
        headers = {"HTTP_AUTHORIZATION": f"Bearer {token}"}
        response = self.api("POST", "/api/v1/terminal/heartbeat",
                            {"version": "2.0-test", "pending_events": 0, "last_error": "", "client_time": int(time.time())},
                            tenant=False, **headers)
        self.assertEqual(response.status_code, 200, response.content)
        saved = Terminal.objects.get(pk=terminal.pk)
        self.assertIsNotNone(saved.last_seen_at)
        self.assertEqual(saved.heartbeat_pending_events, 0)
        self.assertEqual(saved.heartbeat_error, "")

    def test_label_settings_default_validation_and_isolation(self):
        self.client.force_login(self.owner)
        self.assertEqual(self.api("GET", "/api/v1/label-settings").json(),
                         {"width_mm": 50, "height_mm": 30, "gap_mm": 2, "sensor": "gap"})
        saved = self.api("PATCH", "/api/v1/label-settings",
                         {"width_mm": 40, "height_mm": 25.5, "gap_mm": 3, "sensor": "gap"})
        self.assertEqual(saved.status_code, 200, saved.content)
        self.assertEqual(Tenant.objects.get(pk=self.tenant.pk).label_settings,
                         {"width_mm": 40, "height_mm": 25.5, "gap_mm": 3, "sensor": "gap"})
        continuous = self.api("PATCH", "/api/v1/label-settings",
                              {"width_mm": 58, "height_mm": 40, "gap_mm": 3, "sensor": "continuous"})
        self.assertEqual(continuous.json()["gap_mm"], 0)
        for body in ({"width_mm": 200, "height_mm": 30, "gap_mm": 2, "sensor": "gap"},
                     {"width_mm": 50, "height_mm": 30, "gap_mm": 2, "sensor": "laser"},
                     {"width_mm": True, "height_mm": 30, "gap_mm": 2, "sensor": "gap"},
                     {"width_mm": 50, "height_mm": 30, "sensor": "gap"}):
            self.assertEqual(self.api("PATCH", "/api/v1/label-settings", body).status_code, 400, body)
        self.assertEqual(self.api("GET", "/api/v1/label-settings", tenant=self.other_tenant.id).status_code, 403)

    def test_platform_staff_scope(self):
        User = get_user_model()
        root = User.objects.create_superuser(username="root@example.test", email="root@example.test",
                                             password="Platform-42!")
        self.client.force_login(root)
        created = self.api("POST", "/api/v1/platform/users",
                           {"name": "Suporte", "email": "support@example.test",
                            "password": "Support-42!", "roles": ["platform_support"]}, tenant=False)
        self.assertEqual(created.status_code, 201, created.content)
        staff = User.objects.get(username="support@example.test")
        cashier = User.objects.create_user(username="cashier@example.test", email="cashier@example.test",
                                           password="Cashier-42!")
        Membership.objects.create(tenant=self.tenant, user=cashier, name="Caixa", role="cashier")
        listed = self.api("GET", "/api/v1/platform/users", tenant=False)
        self.assertEqual(listed.status_code, 200, listed.content)
        rows = listed.json()["results"]
        self.assertEqual({row["email"] for row in rows}, {
            "root@example.test", "support@example.test", "owner@example.test", "other@example.test"})
        self.assertNotIn("cashier@example.test", {row["email"] for row in rows})
        root_row = next(row for row in rows if row["email"] == "root@example.test")
        self.assertTrue(root_row["superuser"])
        self.assertTrue(root_row["protected"])
        self.assertEqual(self.api("PATCH", f"/api/v1/platform/users/{root.pk}",
                                  {"name": "Não alterar"}, tenant=False).status_code, 409)
        owner_row = next(row for row in rows if row["email"] == "owner@example.test")
        self.assertEqual(owner_row["memberships"][0]["role"], "owner")
        promoted = self.api("PATCH", f"/api/v1/platform/users/{self.owner.pk}",
                            {"roles": ["platform_ops"]}, tenant=False)
        self.assertEqual(promoted.status_code, 200, promoted.content)
        self.owner.refresh_from_db()
        self.assertTrue(self.owner.is_staff)
        self.assertEqual(promoted.json()["roles"], ["platform_ops"])
        demoted = self.api("PATCH", f"/api/v1/platform/users/{self.owner.pk}",
                           {"roles": []}, tenant=False)
        self.assertEqual(demoted.status_code, 200, demoted.content)
        self.owner.refresh_from_db()
        self.assertFalse(self.owner.is_staff)
        self.assertEqual(self.api("PATCH", f"/api/v1/platform/users/{cashier.pk}",
                                  {"roles": ["platform_support"]}, tenant=False).status_code, 403)
        self.client.force_login(staff)
        self.assertEqual(self.api("GET", "/api/v1/platform/tenants", tenant=False).status_code, 200)
        self.assertEqual(self.api("GET", "/api/v1/platform/plans", tenant=False).status_code, 403)
        self.assertEqual(self.api("GET", "/api/v1/platform/users", tenant=False).status_code, 403)
        self.assertEqual(self.api("GET", "/api/v1/products").status_code, 403)

    def test_session_mutation_requires_csrf_token(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.owner)
        token = client.get("/api/v1/csrf").json()["csrfToken"]
        self.assertEqual(self.api("POST", "/api/v1/categories", {"name": "Secos"}, client=client).status_code, 403)
        allowed = self.api("POST", "/api/v1/categories", {"name": "Secos"},
                           client=client, HTTP_X_CSRFTOKEN=token)
        self.assertEqual(allowed.status_code, 201, allowed.content)

    def test_stock_receipt_tracks_purchase_file_and_updates_cost(self):
        self.client.force_login(self.owner)
        body = {
            "shop_id": str(self.shop.id), "reference": "compra-mercado.csv",
            "idempotency_key": "purchase-file-1",
            "source_file": {"name": "compra-mercado.csv", "mime": "text/csv",
                            "size": 128, "sha256": "a" * 64},
            "items": [{"product_id": str(self.product.id), "quantity_milli": 3000,
                       "unit_cost_cents": 725}],
        }
        response = self.api("POST", "/api/v1/stock/receipts", body)
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(response.json()["source_file"]["name"], "compra-mercado.csv")
        self.product.refresh_from_db()
        self.assertEqual(self.product.cost_cents, 725)

    def test_stock_receipt_requires_cost_and_builds_weighted_average_history(self):
        self.client.force_login(self.owner)
        missing_cost = self.api("POST", "/api/v1/stock/receipts", {
            "shop_id": str(self.shop.id), "reference": "NF-omitted-cost",
            "idempotency_key": "purchase-catalog-cost",
            "items": [{"product_id": str(self.product.id), "quantity_milli": 3000}],
        })
        self.assertEqual(missing_cost.status_code, 400)
        first = self.api("POST", "/api/v1/stock/receipts", {
            "shop_id": str(self.shop.id), "reference": "NF-725",
            "idempotency_key": "purchase-average-1",
            "items": [{"product_id": str(self.product.id), "quantity_milli": 3000,
                       "unit_cost_cents": 725}],
        })
        self.assertEqual(first.status_code, 201, first.content)
        self.assertEqual(first.json()["items"][0]["average_cost_cents"], 725)
        second = self.api("POST", "/api/v1/stock/receipts", {
            "shop_id": str(self.shop.id), "reference": "NF-900",
            "idempotency_key": "purchase-average-2",
            "items": [{"product_id": str(self.product.id), "quantity_milli": 1000,
                       "unit_cost_cents": 900}],
        })
        self.assertEqual(second.status_code, 201, second.content)
        self.assertEqual(second.json()["items"][0]["average_cost_cents"], 769)
        self.product.refresh_from_db()
        self.assertEqual(self.product.cost_cents, 769)


class LicenseBackfillMigrationTests(TransactionTestCase):
    def test_existing_subscriptions_keep_individual_keys_and_sources(self):
        before = [("core", "0006_advanced_terminal_limit"),
                  ("operations", "0002_productrelease")]
        after = [("core", "0007_subscription_license"),
                 ("operations", "0002_productrelease")]
        executor = MigrationExecutor(connection)
        executor.migrate(before)
        try:
            old_apps = executor.loader.project_state(before).apps
            PlanOld = old_apps.get_model("core", "Plan")
            TenantOld = old_apps.get_model("core", "Tenant")
            SubscriptionOld = old_apps.get_model("core", "Subscription")
            plan = PlanOld.objects.create(slug="migration-check", name="Migração")
            rows = []
            for name, status, external_id in (("Teste", "trial", ""),
                                              ("Pago", "active", "provider-123"),
                                              ("Manual", "active", "")):
                tenant = TenantOld.objects.create(name=name)
                rows.append(SubscriptionOld.objects.create(
                    tenant=tenant, plan=plan, status=status, external_id=external_id).pk)
            executor = MigrationExecutor(connection)
            executor.migrate(after)
            new_apps = executor.loader.project_state(after).apps
            SubscriptionNew = new_apps.get_model("core", "Subscription")
            licenses = list(SubscriptionNew.objects.filter(pk__in=rows)
                            .order_by("tenant__name").values_list("license_key", "license_source"))
            self.assertEqual(len(licenses), 3)
            self.assertEqual(len({key for key, _ in licenses}), 3)
            self.assertTrue(all(key.startswith("PDV2-") and len(key) == 32 for key, _ in licenses))
            self.assertEqual({source for _, source in licenses}, {"trial", "subscription", "manual"})
        finally:
            MigrationExecutor(connection).migrate(after)
