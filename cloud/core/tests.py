"""API contract tests for tenant isolation, RBAC, licensing and atomic intake."""
import hashlib
import json
import os
import time
import uuid
from datetime import timedelta
from ipaddress import ip_network
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import Client, RequestFactory, TestCase, override_settings
from django.utils import timezone

from shared.licensing import generate_keypair, verify_lease
from .api_common import client_ip, password_policy
from .models import Membership, Plan, Product, RateLimit, Shop, Subscription, SyncEvent, Tenant, Terminal


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
        body = {"code": code, "device_id": "device-1", "version": "1.0"}
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
