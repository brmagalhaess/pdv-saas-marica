"""Long offline lease: plan-configured term, capability header and the catalog-free renewal route."""
import hashlib
import json
import os
import time
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.utils import timezone

from shared.licensing import MAX_LEASE_SECONDS, generate_keypair, verify_lease
from .models import Plan, Shop, Subscription, Tenant, Terminal

DAY = 86400
LONG = {"HTTP_X_PDV_CAPABILITIES": "long-lease"}


class LongLeaseTests(TestCase):
    def setUp(self):
        self.private, self.public = generate_keypair()
        self.release_private, self.release_public = generate_keypair()
        env = patch.dict(os.environ, {"PDV_LICENSE_PRIVATE_KEY": self.private, "PDV_LICENSE_PUBLIC_KEY": self.public,
                                      "PDV_LICENSE_PRIVATE_KEY_FILE": "", "PDV_LICENSE_PUBLIC_KEY_FILE": "",
                                      "PDV_RELEASE_PRIVATE_KEY": self.release_private,
                                      "PDV_RELEASE_PUBLIC_KEY": self.release_public,
                                      "PDV_RELEASE_PRIVATE_KEY_FILE": "", "PDV_RELEASE_PUBLIC_KEY_FILE": ""})
        env.start()
        self.addCleanup(env.stop)
        self.plan = Plan.objects.create(slug="longo", name="Longo", features=["stock"])
        self.tenant = Tenant.objects.create(name="Mercado Longo")
        self.shop = Shop.objects.create(tenant=self.tenant, name="Matriz")
        self.sub = Subscription.objects.create(tenant=self.tenant, plan=self.plan, status="active",
                                               current_period_end=timezone.now() + timedelta(days=90))
        self.token = "t" * 48
        self.terminal = Terminal.objects.create(tenant=self.tenant, shop=self.shop, name="Caixa", device_id="device-1",
                                                token_hash=hashlib.sha256(self.token.encode()).hexdigest())

    def call(self, verb, path, body=None, **headers):
        headers.setdefault("HTTP_AUTHORIZATION", "Bearer " + self.token)
        return self.client.generic(verb, path, data=json.dumps(body) if body is not None else None,
                                   content_type="application/json", **headers)

    def lease(self, **headers):
        response = self.call("POST", "/api/v1/terminal/lease", {}, **headers)
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()

    def term(self, data):
        claims = verify_lease(data["lease"], data["public_key"], expected={"terminal_id": str(self.terminal.id)})
        return claims["expires_at"] - claims["issued_at"], claims

    def test_plan_defaults_to_thirty_days(self):
        self.assertEqual(self.plan.offline_days, 30)
        self.assertEqual(Plan.objects.get(slug="essential").offline_days, 30)
        self.assertEqual(MAX_LEASE_SECONDS, 45 * DAY)

    def test_long_term_only_for_terminals_declaring_the_capability(self):
        seconds, _ = self.term(self.lease(**LONG))
        self.assertEqual(seconds, 30 * DAY)
        seconds, _ = self.term(self.lease())
        self.assertEqual(seconds, 72 * 3600)
        seconds, _ = self.term(self.lease(HTTP_X_PDV_CAPABILITIES="other, Long-Lease"))
        self.assertEqual(seconds, 30 * DAY)
        seconds, _ = self.term(self.lease(HTTP_X_PDV_CAPABILITIES="long-lease-v2"))
        self.assertEqual(seconds, 72 * 3600)

    def test_term_follows_the_plan(self):
        for days in (1, 10, 45):
            Plan.objects.filter(pk=self.plan.pk).update(offline_days=days)
            self.assertEqual(self.term(self.lease(**LONG))[0], days * DAY)
        # A row edited outside the API still cannot exceed what terminals accept.
        Plan.objects.filter(pk=self.plan.pk).update(offline_days=400)
        self.assertEqual(self.term(self.lease(**LONG))[0], MAX_LEASE_SECONDS)

    def test_term_is_capped_by_paid_period_plus_grace(self):
        end = timezone.now() + timedelta(days=5)
        Subscription.objects.filter(pk=self.sub.pk).update(current_period_end=end)
        seconds, claims = self.term(self.lease(**LONG))
        self.assertEqual(claims["expires_at"], int((end + timedelta(hours=72)).timestamp()))
        self.assertLess(seconds, 9 * DAY)
        # Complimentary access has no payment grace.
        Subscription.objects.filter(pk=self.sub.pk).update(license_source="vip")
        _, claims = self.term(self.lease(**LONG))
        self.assertEqual(claims["expires_at"], int(end.timestamp()))

    def test_trial_is_capped_by_the_trial_end(self):
        end = timezone.now() + timedelta(days=3)
        Subscription.objects.filter(pk=self.sub.pk).update(status="trial", trial_ends_at=end)
        _, claims = self.term(self.lease(**LONG))
        self.assertEqual(claims["expires_at"], int((end + timedelta(hours=24)).timestamp()))

    def test_past_due_keeps_the_short_term_even_with_capability(self):
        Subscription.objects.filter(pk=self.sub.pk).update(status="past_due")
        seconds, claims = self.term(self.lease(**LONG))
        self.assertEqual(claims["status"], "past_due")
        self.assertEqual(seconds, 24 * 3600)

    def test_expired_subscription_does_not_renew(self):
        Subscription.objects.filter(pk=self.sub.pk).update(current_period_end=timezone.now() - timedelta(hours=73))
        self.assertEqual(self.call("POST", "/api/v1/terminal/lease", {}, **LONG).status_code, 403)
        Subscription.objects.filter(pk=self.sub.pk).update(status="suspended")
        self.assertEqual(self.call("POST", "/api/v1/terminal/lease", {}, **LONG).status_code, 403)

    def test_lease_route_shape_and_authentication(self):
        before = int(time.time())
        data = self.lease(**LONG)
        self.assertEqual(set(data), {"lease", "public_key", "operators", "server_time", "release_public_key",
                                     "capabilities", "mirror_key"})
        self.assertEqual(data["public_key"], self.public)
        self.assertEqual(data["release_public_key"], self.release_public)
        self.assertIsInstance(data["server_time"], int)
        self.assertTrue(before <= data["server_time"] <= before + 5)
        self.assertEqual(self.call("GET", "/api/v1/terminal/lease").status_code, 405)
        self.assertEqual(self.call("POST", "/api/v1/terminal/lease", {}, HTTP_AUTHORIZATION="").status_code, 401)
        self.assertEqual(self.call("POST", "/api/v1/terminal/lease", {}, HTTP_AUTHORIZATION="Bearer " + "x" * 48).status_code, 401)
        Terminal.objects.filter(pk=self.terminal.pk).update(revoked=True)
        self.assertEqual(self.call("POST", "/api/v1/terminal/lease", {}, **LONG).status_code, 401)

    def test_lease_renews_when_the_catalog_bootstrap_fails(self):
        self.client.raise_request_exception = False
        with patch("core.views_terminal.stock_balances", side_effect=RuntimeError("catalog down")), \
                self.assertLogs("django.request", "ERROR"):
            self.assertEqual(self.call("GET", "/api/v1/terminal/bootstrap", **LONG).status_code, 500)
            self.assertEqual(self.term(self.lease(**LONG))[0], 30 * DAY)

    def test_lease_route_is_rate_limited_per_terminal(self):
        for _ in range(30):
            self.assertEqual(self.call("POST", "/api/v1/terminal/lease", {}).status_code, 200)
        self.assertEqual(self.call("POST", "/api/v1/terminal/lease", {}).status_code, 429)

    def test_bootstrap_and_heartbeat_carry_server_time_and_release_key(self):
        short = self.call("GET", "/api/v1/terminal/bootstrap").json()
        self.assertEqual(self.term(short)[0], 72 * 3600)
        data = self.call("GET", "/api/v1/terminal/bootstrap", **LONG).json()
        self.assertEqual(self.term(data)[0], 30 * DAY)
        self.assertEqual(data["release_public_key"], self.release_public)
        self.assertLessEqual(abs(data["server_time"] - int(time.time())), 5)
        beat = self.call("POST", "/api/v1/terminal/heartbeat", {}, **LONG)
        self.assertEqual(beat.status_code, 200, beat.content)
        self.assertLessEqual(abs(beat.json()["server_time"] - int(time.time())), 5)

    def test_release_key_falls_back_to_license_key_and_never_blocks_the_lease(self):
        with patch.dict(os.environ, {"PDV_RELEASE_PRIVATE_KEY": "", "PDV_RELEASE_PUBLIC_KEY": ""}):
            self.assertEqual(self.lease()["release_public_key"], self.public)
        with patch.dict(os.environ, {"PDV_RELEASE_PRIVATE_KEY": "not a key"}), self.assertLogs("core.views_terminal", "ERROR"):
            data = self.lease()
        self.assertIsNone(data["release_public_key"])
        self.term(data)

    def pair(self, code, device, **headers):
        Terminal.objects.create(tenant=self.tenant, shop=self.shop, name="Novo",
                                pairing_code_hash=hashlib.sha256(code.encode()).hexdigest(),
                                pairing_expires_at=timezone.now() + timedelta(minutes=10))
        Plan.objects.filter(pk=self.plan.pk).update(terminal_limit=5)
        response = self.client.post("/api/v1/terminal/pair", json.dumps(
            {"license_key": self.sub.license_key, "code": code, "device_id": device, "version": "2.1.0"}),
            content_type="application/json", **headers)
        self.assertEqual(response.status_code, 200, response.content)
        data = response.json()
        claims = verify_lease(data["lease"], data["public_key"])
        return data, claims["expires_at"] - claims["issued_at"]

    def test_pair_honours_the_capability_and_returns_release_key(self):
        data, seconds = self.pair("pair-long-1", "device-long", **LONG)
        self.assertEqual(seconds, 30 * DAY)
        self.assertEqual(data["release_public_key"], self.release_public)
        self.assertIsInstance(data["server_time"], int)
        _, seconds = self.pair("pair-short-1", "device-short")
        self.assertEqual(seconds, 72 * 3600)


class PlanOfflineDaysAPITests(TestCase):
    def setUp(self):
        root = get_user_model().objects.create_superuser(username="root-plano@example.test",
                                                         email="root-plano@example.test", password="Root-42!")
        self.client.force_login(root)
        self.body = {"slug": "offline", "name": "Offline", "price_month_cents": 1000, "price_year_cents": 10000,
                     "terminal_limit": 1, "shop_limit": 1, "features": []}

    def send(self, verb, path, body):
        return self.client.generic(verb, path, data=json.dumps(body), content_type="application/json")

    def test_plan_api_exposes_and_validates_offline_days(self):
        created = self.send("POST", "/api/v1/platform/plans", self.body)
        self.assertEqual(created.status_code, 201, created.content)
        self.assertEqual(created.json()["offline_days"], 30)
        path = "/api/v1/platform/plans/" + created.json()["id"]
        for invalid in (0, 46, -1, "30", 2.5, True, None):
            response = self.send("PATCH", path, {"offline_days": invalid})
            self.assertEqual(response.status_code, 400, (invalid, response.content))
            self.assertEqual(self.send("POST", "/api/v1/platform/plans",
                                       {**self.body, "slug": "outro", "offline_days": invalid}).status_code, 400)
        for valid in (1, 45):
            response = self.send("PATCH", path, {"offline_days": valid})
            self.assertEqual(response.status_code, 200, response.content)
            self.assertEqual(response.json()["offline_days"], valid)
        self.assertEqual(self.client.get(path).json()["offline_days"], 45)
        listed = self.client.get("/api/v1/platform/plans").json()
        self.assertTrue(all("offline_days" in row for row in listed["results"]))
        other = self.send("POST", "/api/v1/platform/plans", {**self.body, "slug": "sete", "offline_days": 7})
        self.assertEqual(other.json()["offline_days"], 7)

    def test_model_validators_reject_out_of_range(self):
        for invalid in (0, 46):
            with self.assertRaises(ValidationError):
                Plan(slug="x", name="X", offline_days=invalid).full_clean()
