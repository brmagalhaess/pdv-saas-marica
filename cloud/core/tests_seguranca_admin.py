"""Django admin (/control/) hardening: MFA gate, opt-in in production, read-only money data."""
import importlib
import json
import os
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

from django import forms
from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import clear_url_caches
from django.utils import timezone

from .admin import platform_admin
from .models import (AuditEvent, BillingInvoice, FinanceEntry, FiscalDocument, Membership, Plan, Sale,
                     Shop, Subscription, Tenant, Terminal)
from .views_auth import MFA_VERIFIED

PASSWORD = "Synthetic-42!"
# The manifest storage needs collectstatic output, which only the image build produces.
PRODUCTION_LIKE = {"DEBUG": False, "PLATFORM_MFA_REQUIRED": True, "STORAGES": {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}}}


class AdminBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.root = User.objects.create_superuser(username="root@example.test", email="root@example.test",
                                                 password=PASSWORD)
        cls.staff = User.objects.create_user(username="staff@example.test", email="staff@example.test",
                                             password=PASSWORD, is_staff=True)
        cls.owner = User.objects.create_user(username="owner@example.test", email="owner@example.test",
                                             password=PASSWORD)
        cls.plan = Plan.objects.get(slug="essential")
        cls.tenant = Tenant.objects.create(name="Mercado Um")
        cls.shop = Shop.objects.create(tenant=cls.tenant, name="Matriz")
        cls.member = Membership.objects.create(tenant=cls.tenant, user=cls.owner, name="Dona", role="owner",
                                               pin_hash="pbkdf2_sha256$fake$pin-hash-marker")
        cls.sub = Subscription.objects.create(tenant=cls.tenant, plan=cls.plan, status="trial",
                                              trial_ends_at=timezone.now() + timedelta(days=14))

    def sign_in(self, user, mfa=True):
        self.client.force_login(user)
        if mfa:
            session = self.client.session
            session[MFA_VERIFIED] = True
            session.save()

    def form_data(self, url, **changes):
        """Current values of an admin change form, ready to be posted back."""
        form = self.client.get(url).context["adminform"].form
        data = {}
        for name, field in form.fields.items():
            value = form[name].value()
            if isinstance(value, (list, dict)):
                value = json.dumps(value)
            elif isinstance(field, forms.BooleanField):
                if not value:
                    continue
                value = "on"
            elif value is None:
                value = ""
            data[name] = value
        data.update(changes)
        return data


@override_settings(**PRODUCTION_LIKE)
class AdminAccessTests(AdminBase):
    def test_admin_login_never_authenticates_with_password_alone(self):
        for attempt in range(8):
            response = self.client.post("/control/login/", {"username": "root@example.test",
                                                            "password": PASSWORD, "next": "/control/"})
            self.assertEqual(response.status_code, 302)
            self.assertEqual(response["Location"], "/login/")
        self.assertNotIn("_auth_user_id", self.client.session)
        index = self.client.get("/control/")
        self.assertEqual(index.status_code, 302)
        self.assertTrue(index["Location"].startswith("/control/login/"))

    def test_login_page_redirects_to_platform_login(self):
        response = self.client.get("/control/login/?next=/control/")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "/login/")

    def test_superuser_without_mfa_session_is_refused(self):
        self.sign_in(self.root, mfa=False)
        response = self.client.get("/control/", follow=True)
        self.assertEqual(response.redirect_chain[-1][0], "/login/")
        self.assertEqual(self.client.get("/control/core/subscription/").status_code, 302)

    def test_superuser_with_mfa_session_opens_admin(self):
        self.sign_in(self.root)
        self.assertEqual(self.client.get("/control/").status_code, 200)
        self.assertEqual(self.client.get("/control/core/subscription/").status_code, 200)
        already_in = self.client.get("/control/login/")
        self.assertEqual(already_in["Location"], "/control/")

    def test_staff_and_company_owner_are_refused_even_with_mfa(self):
        for user in (self.staff, self.owner):
            self.sign_in(user)
            self.assertEqual(self.client.get("/control/").status_code, 302, user.username)
            self.client.logout()

    def test_mfa_is_demanded_outside_debug_even_when_the_requirement_is_off(self):
        self.sign_in(self.root, mfa=False)
        with override_settings(DEBUG=False, PLATFORM_MFA_REQUIRED=False):
            self.assertEqual(self.client.get("/control/").status_code, 302)
        with override_settings(DEBUG=True, PLATFORM_MFA_REQUIRED=True):
            self.assertEqual(self.client.get("/control/").status_code, 302)
        with override_settings(DEBUG=True, PLATFORM_MFA_REQUIRED=False):
            self.assertEqual(self.client.get("/control/").status_code, 200)

    def test_users_and_groups_are_not_exposed(self):
        self.sign_in(self.root)
        self.assertEqual(self.client.get("/control/auth/user/").status_code, 404)
        self.assertEqual(self.client.get("/control/auth/user/add/").status_code, 404)
        self.assertEqual(self.client.get("/control/auth/group/").status_code, 404)
        registered = {model._meta.label for model in platform_admin._registry}
        self.assertFalse({"auth.User", "auth.Group", "core.PlatformMFA"} & registered)

    def test_admin_routes_do_not_exist_when_disabled(self):
        import pdvcloud.urls as urls

        def restore():
            importlib.reload(urls)
            clear_url_caches()

        self.addCleanup(restore)
        self.sign_in(self.root)
        with override_settings(PDV_ENABLE_DJANGO_ADMIN=False):
            importlib.reload(urls)
            clear_url_caches()
            self.assertEqual(self.client.get("/control/").status_code, 404)
            self.assertEqual(self.client.get("/control/login/").status_code, 404)
            self.assertEqual(self.client.post("/control/login/", {"username": "root@example.test",
                                                                  "password": PASSWORD}).status_code, 404)

    def test_production_settings_enable_admin_only_on_request(self):
        root = Path(settings.ROOT_DIR)
        base = {key: value for key, value in os.environ.items()
                if not key.startswith(("PDV_", "DJANGO_", "DATABASE_URL"))}
        base.update({
            "PDV_ENV": "production", "PDV_DEBUG": "1", "PDV_SECRET_KEY": "k" * 64,
            "PDV_ALLOWED_HOSTS": "pdv.example.test", "PDV_LICENSE_PRIVATE_KEY": "test-only",
            "DATABASE_URL": "postgresql://pdv2:unused@127.0.0.1:1/pdv2",
            "DJANGO_SETTINGS_MODULE": "pdvcloud.settings",
            "PYTHONPATH": os.pathsep.join([str(root / "cloud"), str(root)]),
        })
        script = ("import django; django.setup()\n"
                  "from django.conf import settings\n"
                  "from django.urls import Resolver404, resolve\n"
                  "try:\n    resolve('/control/'); routed = True\n"
                  "except Resolver404:\n    routed = False\n"
                  "print(settings.DEBUG, settings.PDV_ENABLE_DJANGO_ADMIN, routed)\n")
        for flag, expected in ((None, "False False False"), ("0", "False False False"),
                               ("1", "False True True")):
            env = dict(base)
            if flag is not None:
                env["PDV_ENABLE_DJANGO_ADMIN"] = flag
            result = subprocess.run([sys.executable, "-c", script], env=env, capture_output=True,
                                    text=True, timeout=120)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip().splitlines()[-1], expected, f"flag={flag}")


@override_settings(**PRODUCTION_LIKE)
class AdminReadOnlyTests(AdminBase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.terminal = Terminal.objects.create(tenant=cls.tenant, shop=cls.shop, name="Caixa 1",
                                               token_hash="a" * 64)
        cls.sale = Sale.objects.create(id="11111111-1111-4111-8111-111111111111", tenant=cls.tenant,
                                       shop=cls.shop, request_id="sale-1", content_hash="b" * 64,
                                       gross_cents=1000, total_cents=1000, created_at=timezone.now())
        cls.entry = FinanceEntry.objects.create(tenant=cls.tenant, kind="receivable", description="Fiado",
                                                amount_cents=1000, due_date=date(2026, 12, 1), sale=cls.sale)
        cls.invoice = BillingInvoice.objects.create(tenant=cls.tenant, subscription=cls.sub,
                                                    amount_cents=14900, due_date=date(2026, 12, 1))
        cls.document = FiscalDocument.objects.create(tenant=cls.tenant, shop=cls.shop, sale=cls.sale,
                                                     status="authorized")

    def setUp(self):
        self.sign_in(self.root)

    def test_only_plan_and_company_accept_changes(self):
        request = self.client.get("/control/").wsgi_request
        editable = {model._meta.label for model, model_admin in platform_admin._registry.items()
                    if model_admin.has_change_permission(request) or model_admin.has_add_permission(request)
                    or model_admin.has_delete_permission(request)}
        self.assertEqual(editable, {"core.Plan", "core.Tenant"})
        for model in (Subscription, BillingInvoice, FinanceEntry, Membership, Sale, Terminal, FiscalDocument):
            self.assertIn(model, platform_admin._registry)
        for model in (Plan, Tenant):
            self.assertFalse(platform_admin._registry[model].has_delete_permission(request))

    def test_sensitive_records_cannot_be_changed_added_or_deleted(self):
        rows = (("subscription", self.sub), ("billinginvoice", self.invoice), ("financeentry", self.entry),
                ("membership", self.member), ("sale", self.sale), ("terminal", self.terminal),
                ("fiscaldocument", self.document))
        for name, row in rows:
            with self.subTest(model=name):
                base = f"/control/core/{name}/"
                self.assertEqual(self.client.get(f"{base}{row.pk}/change/").status_code, 200)
                changed = self.client.post(f"{base}{row.pk}/change/", {"status": "active", "role": "viewer",
                                                                       "revoked": "on", "amount_cents": "1"})
                self.assertEqual(changed.status_code, 403)
                self.assertEqual(self.client.get(f"{base}add/").status_code, 403)
                self.assertEqual(self.client.post(f"{base}add/", {"status": "active"}).status_code, 403)
                self.assertEqual(self.client.post(f"{base}{row.pk}/delete/", {"post": "yes"}).status_code, 403)
                bulk = self.client.post(base, {"action": "delete_selected", "_selected_action": [str(row.pk)],
                                               "post": "yes"})
                self.assertNotEqual(bulk.status_code, 302)
                self.assertTrue(type(row).objects.filter(pk=row.pk).exists())
        self.sub.refresh_from_db()
        self.entry.refresh_from_db()
        self.member.refresh_from_db()
        self.assertEqual((self.sub.status, self.entry.status, self.entry.amount_cents, self.member.role),
                         ("trial", "open", 1000, "owner"))

    def test_secret_hashes_are_not_displayed(self):
        member = self.client.get(f"/control/core/membership/{self.member.pk}/change/")
        self.assertNotContains(member, "pin-hash-marker")
        terminal = self.client.get(f"/control/core/terminal/{self.terminal.pk}/change/")
        self.assertNotContains(terminal, "a" * 64)


@override_settings(**PRODUCTION_LIKE)
class AdminValidationTests(AdminBase):
    def setUp(self):
        self.sign_in(self.root)
        self.plan_url = f"/control/core/plan/{self.plan.pk}/change/"
        self.tenant_url = f"/control/core/tenant/{self.tenant.pk}/change/"

    def test_plan_features_must_be_a_list_of_short_strings(self):
        original = list(self.plan.features)
        invalid = ('{"finance": true}', '"finance"', '["finance", 3]', json.dumps(["x" * 81]),
                   json.dumps(["f"] * 101), '[""]')
        for value in invalid:
            with self.subTest(features=value[:30]):
                response = self.client.post(self.plan_url, self.form_data(self.plan_url, features=value))
                self.assertEqual(response.status_code, 200)
                self.assertIn("features", response.context["adminform"].form.errors)
                self.plan.refresh_from_db()
                self.assertEqual(self.plan.features, original)

    def test_valid_plan_change_is_saved_and_audited(self):
        data = self.form_data(self.plan_url, features='["finance", "fiscal"]', slug="renamed")
        response = self.client.post(self.plan_url, data)
        self.assertEqual(response.status_code, 302, getattr(response, "context", None)
                         and response.context["adminform"].form.errors)
        self.plan.refresh_from_db()
        self.assertEqual(self.plan.features, ["finance", "fiscal"])
        self.assertEqual(self.plan.slug, "essential")
        event = AuditEvent.objects.get(action="platform.plan_updated", subject_id=str(self.plan.pk))
        self.assertEqual(event.actor, self.root)
        self.assertEqual(event.detail["via"], "django_admin")
        emptied = self.client.post(self.plan_url, self.form_data(self.plan_url, features="[]"))
        self.assertEqual(emptied.status_code, 302)
        self.plan.refresh_from_db()
        self.assertEqual(self.plan.features, [])

    def test_plan_limits_cannot_drop_below_current_use(self):
        for index in range(2):
            Terminal.objects.create(tenant=self.tenant, shop=self.shop, name=f"Caixa {index}")
        Shop.objects.create(tenant=self.tenant, name="Filial")
        Plan.objects.filter(pk=self.plan.pk).update(terminal_limit=5, shop_limit=5)
        for field in ("terminal_limit", "shop_limit"):
            with self.subTest(field=field):
                response = self.client.post(self.plan_url, self.form_data(self.plan_url, **{field: "1"}))
                self.assertEqual(response.status_code, 200)
                self.assertIn(field, response.context["adminform"].form.errors)
        response = self.client.post(self.plan_url, self.form_data(self.plan_url, terminal_limit="2",
                                                                  shop_limit="2"))
        self.assertEqual(response.status_code, 302)
        self.plan.refresh_from_db()
        self.assertEqual((self.plan.terminal_limit, self.plan.shop_limit), (2, 2))

    def test_company_discount_ceiling_is_limited_to_100_percent(self):
        response = self.client.post(self.tenant_url, self.form_data(self.tenant_url, max_discount_bps="10001"))
        self.assertEqual(response.status_code, 200)
        self.assertIn("max_discount_bps", response.context["adminform"].form.errors)
        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.max_discount_bps, 10000)
        response = self.client.post(self.tenant_url, self.form_data(self.tenant_url, max_discount_bps="1500"))
        self.assertEqual(response.status_code, 302)
        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.max_discount_bps, 1500)
        self.assertTrue(AuditEvent.objects.filter(action="platform.tenant_updated", tenant=self.tenant,
                                                  actor=self.root).exists())

    def test_plan_and_company_cannot_be_deleted_or_company_created(self):
        self.assertEqual(self.client.post(f"/control/core/plan/{self.plan.pk}/delete/",
                                          {"post": "yes"}).status_code, 403)
        self.assertEqual(self.client.post(f"/control/core/tenant/{self.tenant.pk}/delete/",
                                          {"post": "yes"}).status_code, 403)
        self.assertEqual(self.client.get("/control/core/tenant/add/").status_code, 403)
        self.assertTrue(Plan.objects.filter(pk=self.plan.pk).exists())
