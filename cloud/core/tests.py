"""API contract tests for tenant isolation, RBAC, licensing and atomic intake."""
import hashlib
import json
import os
import time
import uuid
from datetime import timedelta
from ipaddress import ip_network
from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import Client, RequestFactory, TestCase, TransactionTestCase, override_settings
from django.utils import timezone

from shared.licensing import generate_keypair, verify_lease
from operations.models import BillingBinding
from operations.services import enqueue
from .api_common import client_ip, password_policy
from .models import AuditEvent, Membership, Plan, Product, RateLimit, Shop, Subscription, SyncEvent, Tenant, Terminal


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
        listed = self.api("GET", "/api/v1/platform/users", tenant=False)
        self.assertEqual(listed.status_code, 200, listed.content)
        rows = listed.json()["results"]
        self.assertEqual({row["email"] for row in rows}, {
            "root@example.test", "support@example.test", "owner@example.test", "other@example.test"})
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
