"""Cloud loose ends closed after the audit waves were merged: name ordering,
unit on returned items, version in auth/me, robots/sitemap, response headers
and the worker logging the jobs it sends to review."""
import hashlib
import json
import time
import uuid
from datetime import timedelta
from unittest.mock import Mock, patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone

from operations.services import enqueue, run_one

from .models import Category, Customer, Membership, Plan, Product, Shop, Subscription, Supplier, Tenant, Terminal


class IntegracaoOndasTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.owner = User.objects.create_user(username="dono@example.test", email="dono@example.test",
                                             password="Synthetic-42!", first_name="Dono")
        cls.tenant = Tenant.objects.create(name="Mercado Ordem")
        cls.member = Membership.objects.create(tenant=cls.tenant, user=cls.owner, name="Dono", role="owner")
        cls.shop = Shop.objects.create(tenant=cls.tenant, name="Matriz")
        Subscription.objects.create(tenant=cls.tenant, plan=Plan.objects.get(slug="management"), status="trial",
                                    trial_ends_at=timezone.now() + timedelta(days=14))
        cls.token = "terminal-token-" + uuid.uuid4().hex
        cls.terminal = Terminal.objects.create(tenant=cls.tenant, shop=cls.shop, name="Caixa 1", device_id="ondas-device",
                                               token_hash=hashlib.sha256(cls.token.encode()).hexdigest())

    def panel(self, verb, path, body=None):
        self.client.force_login(self.owner)
        return self.client.generic(verb, path, data=json.dumps(body) if body is not None else None,
                                   content_type="application/json", HTTP_X_TENANT_ID=str(self.tenant.id))

    def names(self, kind):
        response = self.panel("GET", f"/api/v1/{kind}")
        self.assertEqual(response.status_code, 200, response.content)
        return [row["name"] for row in response.json()["results"]]

    def test_catalog_and_contacts_are_listed_by_name(self):
        # Created out of order, so neither insertion order nor the random UUID gives the answer.
        for index, name in enumerate(("Mamão", "Abacaxi", "Uva", "Banana", "Laranja", "Caju")):
            Product.objects.create(tenant=self.tenant, sku=f"P{index}", name=name, price_cents=100)
            Category.objects.create(tenant=self.tenant, name=name)
            Customer.objects.create(tenant=self.tenant, name=name)
            Supplier.objects.create(tenant=self.tenant, name=name)
        expected = ["Abacaxi", "Banana", "Caju", "Laranja", "Mamão", "Uva"]
        for kind in ("products", "categories", "customers", "suppliers"):
            with self.subTest(kind=kind):
                self.assertEqual(self.names(kind), expected)
        first = self.panel("GET", "/api/v1/products?limit=4").json()
        rest = self.panel("GET", f"/api/v1/products?limit=4&cursor={first['next_cursor']}").json()
        self.assertEqual([row["name"] for row in first["results"] + rest["results"]], expected)

    def test_same_name_keeps_a_stable_order_between_pages(self):
        for _ in range(5):
            Customer.objects.create(tenant=self.tenant, name="Maria")
        ids = [row["id"] for row in self.panel("GET", "/api/v1/customers").json()["results"]]
        self.assertEqual(len(ids), 5)
        paged = []
        cursor = "0"
        while cursor is not None:
            body = self.panel("GET", f"/api/v1/customers?limit=2&cursor={cursor}").json()
            paged += [row["id"] for row in body["results"]]
            cursor = body["next_cursor"]
        self.assertEqual(paged, ids)

    def test_returned_items_carry_the_unit_they_were_sold_in(self):
        product = Product.objects.create(tenant=self.tenant, sku="QUEIJO", name="Queijo", unit="KG", price_cents=4000)
        now = int(time.time() * 1000)
        sale_id = str(uuid.uuid4())
        item = {"product_id": str(product.id), "name": "Queijo", "unit": "KG", "quantity_milli": 1500,
                "unit_price_cents": 4000, "gross_cents": 6000, "discount_cents": 0, "line_total_cents": 6000}
        event = {"event_id": uuid.uuid4().hex, "type": "sale.completed", "aggregate_id": sale_id,
                 "identity": {"tenant_id": str(self.tenant.id), "shop_id": str(self.shop.id),
                              "terminal_id": str(self.terminal.id)},
                 "data": {"request_id": uuid.uuid4().hex, "cash_session_id": uuid.uuid4().hex, "created_at": now,
                          "gross_cents": 6000, "discount_cents": 0, "total_cents": 6000, "change_cents": 0,
                          "items": [item], "payments": [{"method": "cash", "amount_cents": 6000}]}}
        synced = self.client.generic("POST", "/api/v1/terminal/sync", data=json.dumps({"events": [event]}),
                                     content_type="application/json", HTTP_AUTHORIZATION=f"Bearer {self.token}")
        self.assertEqual(synced.status_code, 200, synced.content)
        self.assertEqual(synced.json()["quarantined"], [])
        returned = self.panel("POST", f"/api/v1/sales/{sale_id}/return", {
            "items": [{"product_id": str(product.id), "quantity_milli": 500}],
            "refund_method": "external", "reason": "Produto avariado"})
        self.assertIn(returned.status_code, (200, 201), returned.content)
        # The registration changes afterwards; the return still reads in the unit of the sale.
        Product.objects.filter(pk=product.pk).update(unit="UN")
        detail = self.panel("GET", f"/api/v1/sales/{sale_id}").json()
        self.assertEqual(len(detail["returns"]), 1)
        self.assertEqual(detail["returns"][0]["items"],
                         [{"product_id": str(product.id), "name": "Queijo", "unit": "KG",
                           "quantity_milli": 500, "amount_cents": 2000}])

    def test_auth_me_exposes_the_running_version(self):
        self.client.force_login(self.owner)
        body = self.client.get("/api/v1/auth/me").json()
        self.assertEqual(body["version"], settings.PDV_VERSION)
        self.assertTrue(body["version"])
        self.assertEqual(body["user"]["email"], "dono@example.test")
        with override_settings(PDV_VERSION="9.9.9"):
            self.assertEqual(self.client.get("/api/v1/auth/me").json()["version"], "9.9.9")
        self.client.logout()
        anonymous = self.client.get("/api/v1/auth/me")
        self.assertEqual(anonymous.status_code, 401)
        self.assertNotIn("version", anonymous.json())

    def test_robots_allows_the_landing_and_blocks_private_areas(self):
        response = self.client.get("/robots.txt")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response["Content-Type"].startswith("text/plain"))
        lines = response.content.decode().splitlines()
        self.assertEqual(lines[0], "User-agent: *")
        self.assertIn("Allow: /", lines)
        for path in ("/app/", "/platform/", "/login/", "/api/", "/control/"):
            self.assertIn(f"Disallow: {path}", lines)
        self.assertNotIn("Disallow: /", lines)
        self.assertIn("Sitemap: https://pdv.crowntech.com.br/sitemap.xml", lines)

    def test_sitemap_lists_only_the_landing(self):
        response = self.client.get("/sitemap.xml")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response["Content-Type"].startswith("application/xml"))
        body = response.content.decode()
        self.assertTrue(body.startswith('<?xml version="1.0" encoding="UTF-8"?>'))
        self.assertEqual(body.count("<loc>"), 1)
        self.assertIn("<loc>https://pdv.crowntech.com.br/</loc>", body)
        with override_settings(PDV_PUBLIC_URL="https://loja.example.test/"):
            self.assertIn("<loc>https://loja.example.test/</loc>", self.client.get("/sitemap.xml").content.decode())
            self.assertIn("Sitemap: https://loja.example.test/sitemap.xml",
                          self.client.get("/robots.txt").content.decode())

    def test_camera_is_allowed_for_the_panel_and_the_rest_stays_blocked(self):
        for path in ("/", "/app/", "/api/v1/plans", "/robots.txt"):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response["Permissions-Policy"], "camera=(self), microphone=(), geolocation=()")
                policy = dict(part.strip().split(" ", 1) for part in response["Content-Security-Policy"].split(";"))
                self.assertEqual(policy["img-src"], "'self' data: https:")
                self.assertEqual(policy["default-src"], "'self'")
                self.assertEqual(policy["script-src"], "'self'")
                self.assertEqual(policy["connect-src"], "'self'")
                self.assertEqual(policy["object-src"], "'none'")
                self.assertEqual(policy["frame-ancestors"], "'none'")
                self.assertNotIn("http:", response["Content-Security-Policy"])
                self.assertNotIn("*", response["Content-Security-Policy"])


class WorkerJobLoggingTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Mercado Fila")

    def failing(self, handler, tenant):
        job = enqueue("billing.reconcile", tenant, "job-" + uuid.uuid4().hex, {"payment_id": "pay_1"})
        with patch.dict("operations.services.HANDLERS", {"billing.reconcile": handler}):
            with self.assertLogs("operations.services", level="ERROR") as logs:
                self.assertTrue(run_one())
        job.refresh_from_db()
        return job, logs.records

    def test_unexpected_failure_is_logged_with_job_kind_and_tenant(self):
        job, records = self.failing(Mock(side_effect=KeyError("billingType")), self.tenant)
        self.assertEqual(job.status, "needs_review")
        self.assertEqual(job.last_error, "Falha interna ao executar tarefa; consulte o suporte")
        self.assertEqual(len(records), 1)
        message = records[0].getMessage()
        for expected in (str(job.pk), "billing.reconcile", str(self.tenant.id)):
            self.assertIn(expected, message)
        self.assertIs(records[0].exc_info[0], KeyError)

    def test_traceback_is_not_repeated_when_the_worker_already_logged_it(self):
        handler = Mock(side_effect=KeyError("billingType"))
        handler.logs_failures = True
        job, records = self.failing(handler, self.tenant)
        self.assertEqual(len(records), 1)
        self.assertIn(str(self.tenant.id), records[0].getMessage())
        self.assertFalse(records[0].exc_info)

    def test_job_without_tenant_is_still_logged(self):
        job, records = self.failing(Mock(side_effect=RuntimeError("sem empresa")), None)
        self.assertEqual(job.status, "needs_review")
        self.assertIn(str(job.pk), records[0].getMessage())
