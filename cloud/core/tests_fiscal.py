"""Fiscal registration: company data, tax profiles, product readiness, engine API, CSV import and terminal sync."""
import hashlib
import json
import os
import tempfile
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TestCase, TransactionTestCase
from django.utils import timezone

from shared.licensing import generate_keypair
from .models import Category, IbptRate, Membership, Plan, Product, Shop, Subscription, TaxProfile, Tenant, Terminal

COMPANY = {"name": "Mercado Fiscal Ltda", "document": "11.222.333/0001-81", "address_state": "SP",
           "state_registration": "110.042.490.114", "address_zip": "01310-100", "address_line": "Av. Paulista",
           "address_number": "1000", "address_neighborhood": "Bela Vista", "address_city": "São Paulo",
           "city_ibge_code": "3550308", "cnae": "4711-3/02", "crt": 1}


class FiscalAPITests(TestCase):
    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.tenant = Tenant.objects.create(name="Mercado Fiscal")
        cls.other_tenant = Tenant.objects.create(name="Outro Mercado")
        cls.users = {}
        for role in ("owner", "admin", "manager", "inventory", "finance", "cashier", "viewer"):
            user = User.objects.create_user(username=f"{role}@fiscal.test", email=f"{role}@fiscal.test", password="Synthetic-42!")
            Membership.objects.create(tenant=cls.tenant, user=user, name=role, role=role)
            cls.users[role] = user
        other = User.objects.create_user(username="other@fiscal.test", password="Synthetic-42!")
        Membership.objects.create(tenant=cls.other_tenant, user=other, name="Outra", role="owner")
        cls.other_user = other
        cls.shop = Shop.objects.create(tenant=cls.tenant, name="Matriz")
        plan = Plan.objects.get(slug="essential")
        cls.sub = Subscription.objects.create(tenant=cls.tenant, plan=plan, status="trial",
                                              trial_ends_at=timezone.now() + timedelta(days=14))
        Subscription.objects.create(tenant=cls.other_tenant, plan=plan, status="trial",
                                    trial_ends_at=timezone.now() + timedelta(days=14))
        cls.product = Product.objects.create(tenant=cls.tenant, sku="REFRI", name="Refrigerante 2L", price_cents=899,
                                             ncm="22021000", barcode="7891000315507")

    def api(self, verb, path, body=None, role="owner", tenant=None):
        self.client.force_login(self.users[role] if role else self.other_user)
        return self.client.generic(verb, path, data=json.dumps(body) if body is not None else None,
                                   content_type="application/json",
                                   HTTP_X_TENANT_ID=str(tenant or self.tenant.id))

    # ---------------------------------------------------------------- company

    def test_company_fiscal_data_is_validated(self):
        response = self.api("PATCH", "/api/v1/ops/company", COMPANY)
        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        self.assertEqual((body["crt"], body["tax_regime"], body["document"], body["state_registration"]),
                         (1, "simples_nacional", "11222333000181", "110042490114"))
        self.assertEqual((body["cnae"], body["city_ibge_code"]), ("4711302", "3550308"))
        self.assertTrue(body["fiscal_status"]["ready"], body["fiscal_status"])
        self.assertIn("cnpj", body["tech_responsible"])
        for patch_body, message in (({"document": "11.222.333/0001-82"}, "CNPJ"),
                                    ({"state_registration": "110.042.490.115"}, "Inscrição estadual inválida para SP"),
                                    ({"city_ibge_code": "3304557"}, "IBGE"),
                                    ({"cnae": "123"}, "CNAE"), ({"crt": 5}, "CRT"), ({"crt": "1"}, "CRT"),
                                    ({"address_state": "XX"}, "UF"), ({"simples_credit_rate": "101"}, "pCredSN"),
                                    ({"special_tax_regime": "9"}, "Regime especial")):
            with self.subTest(body=patch_body):
                refused = self.api("PATCH", "/api/v1/ops/company", patch_body)
                self.assertEqual(refused.status_code, 400)
                self.assertIn(message, refused.json()["error"])
        self.assertEqual(self.api("PATCH", "/api/v1/ops/company", {"crt": 3}).json()["tax_regime"], "regime_normal")
        # Older clients that still send the text regime move the CRT as well.
        legacy = self.api("PATCH", "/api/v1/ops/company", {"tax_regime": "mei"}).json()
        self.assertEqual((legacy["crt"], legacy["tax_regime"]), (4, "mei"))
        rate = self.api("PATCH", "/api/v1/ops/company", {"simples_credit_rate": "2,56", "approx_tax_fallback_rate": 31.45}).json()
        self.assertEqual((rate["simples_credit_rate"], rate["approx_tax_fallback_rate"]), ("2.56", "31.45"))
        self.assertEqual(self.api("PATCH", "/api/v1/ops/company", {"crt": 1}, role="manager").status_code, 403)
        alphanumeric = self.api("PATCH", "/api/v1/ops/company", {"document": "12.ABC.345/01DE-35"})
        self.assertEqual(alphanumeric.json()["document"], "12ABC34501DE35")

    def test_company_readiness_lists_what_is_missing(self):
        body = self.api("GET", "/api/v1/ops/company").json()
        self.assertFalse(body["fiscal_status"]["ready"])
        self.assertIn("Falta o CNPJ da empresa", body["fiscal_status"]["missing"])

    # ---------------------------------------------------------------- profiles

    def test_default_profile_presets_and_crud(self):
        listed = self.api("GET", "/api/v1/tax-profiles").json()["results"]
        self.assertEqual(len(listed), 1)
        self.assertTrue(listed[0]["is_default"])
        self.assertEqual(listed[0]["fields"]["icms_csosn"], "102")
        presets = self.api("GET", "/api/v1/tax-profiles/presets").json()
        self.assertEqual(presets["crt"], 1)
        self.assertIn("simples_st", {p["slug"] for p in presets["results"] if p["applies"]})
        created = self.api("POST", "/api/v1/tax-profiles", {"preset": "simples_st", "fields": {"cest": "0300700"}})
        self.assertEqual(created.status_code, 201, created.content)
        st = created.json()
        self.assertEqual((st["fields"]["icms_csosn"], st["fields"]["cfop"], st["fields"]["cest"]), ("500", "5405", "0300700"))
        self.assertEqual(self.api("POST", "/api/v1/tax-profiles", {"preset": "simples_st"}).status_code, 409)
        bad = self.api("POST", "/api/v1/tax-profiles", {"name": "Errado", "fields": {"icms_cst": "61"}})
        self.assertEqual(bad.status_code, 400)
        self.assertEqual(bad.json()["field"], "icms_cst")
        self.assertEqual(self.api("POST", "/api/v1/tax-profiles", {"preset": "nada"}).status_code, 400)
        # Switch the default, then the old default may be deactivated (no product uses it explicitly).
        switched = self.api("PATCH", f"/api/v1/tax-profiles/{st['id']}", {"is_default": True})
        self.assertTrue(switched.json()["is_default"])
        self.assertEqual(TaxProfile.objects.filter(tenant=self.tenant, is_default=True).count(), 1)
        self.assertEqual(self.api("DELETE", f"/api/v1/tax-profiles/{st['id']}").status_code, 409)
        Product.objects.filter(pk=self.product.pk).update(tax_profile_id=listed[0]["id"])
        in_use = self.api("DELETE", f"/api/v1/tax-profiles/{listed[0]['id']}")
        self.assertEqual(in_use.status_code, 409)
        self.assertIn("em uso", in_use.json()["error"])
        replaced = self.api("PATCH", f"/api/v1/tax-profiles/{st['id']}", {"fields": {"icms_csosn": "102", "cfop": "5102"}})
        self.assertEqual(replaced.json()["fields"], {"icms_csosn": "102", "cfop": "5102"})
        isento = self.api("POST", "/api/v1/tax-profiles", {"preset": "isento"}).json()
        self.assertEqual(isento["fields"]["icms_csosn"], "400")

    def test_profile_permissions_and_isolation(self):
        for role, status in (("viewer", 200), ("inventory", 200), ("finance", 200), ("cashier", 403)):
            self.assertEqual(self.api("GET", "/api/v1/tax-profiles", role=role).status_code, status, role)
        for role, status in (("manager", 201), ("finance", 201), ("inventory", 403), ("viewer", 403), ("cashier", 403)):
            response = self.api("POST", "/api/v1/tax-profiles", {"name": f"Perfil {role}", "fields": {"icms_csosn": "102"}}, role=role)
            self.assertEqual(response.status_code, status, role)
        mine = TaxProfile.objects.filter(tenant=self.tenant).first()
        self.assertEqual(self.api("GET", f"/api/v1/tax-profiles/{mine.id}", role=None, tenant=self.other_tenant.id).status_code, 404)
        self.assertEqual(self.api("PATCH", f"/api/v1/products/{self.product.id}",
                                  {"tax_profile_id": str(mine.id)}, role=None, tenant=self.other_tenant.id).status_code, 404)

    # ---------------------------------------------------------------- products

    def test_product_profile_overrides_and_readiness_badge(self):
        created = self.api("POST", "/api/v1/products", {"name": "Arroz 5kg", "price_cents": 2599, "ncm": "10063021"})
        self.assertEqual(created.status_code, 201, created.content)
        body = created.json()
        default = TaxProfile.objects.get(tenant=self.tenant, is_default=True)
        self.assertEqual(body["tax_profile_id"], str(default.id))
        self.assertTrue(body["fiscal"]["ready"], body["fiscal"])
        st = self.api("POST", "/api/v1/tax-profiles", {"preset": "simples_st"}).json()
        moved = self.api("PATCH", f"/api/v1/products/{self.product.id}", {"tax_profile_id": st["id"]}).json()
        self.assertFalse(moved["fiscal"]["ready"])
        self.assertTrue(any(m.startswith("Falta CEST") for m in moved["fiscal"]["missing"]))
        fixed = self.api("PATCH", f"/api/v1/products/{self.product.id}",
                         {"fiscal_overrides": {"cest": "03.007.00", "st_retained_bc_unit": "10.5", "st_retained_rate": "18"}})
        self.assertEqual(fixed.status_code, 200, fixed.content)
        self.assertEqual(fixed.json()["fiscal_overrides"]["cest"], "0300700")
        self.assertTrue(fixed.json()["fiscal"]["ready"])
        listed = self.api("GET", "/api/v1/products?q=Refri").json()["results"][0]
        self.assertEqual(listed["fiscal"]["profile_name"], st["name"])
        bad = self.api("PATCH", f"/api/v1/products/{self.product.id}", {"fiscal_overrides": {"cfop": "51O2"}})
        self.assertEqual((bad.status_code, bad.json()["field"]), (400, "cfop"))
        self.assertEqual(self.api("PATCH", f"/api/v1/products/{self.product.id}",
                                  {"fiscal_overrides": {"ncm": "22021000"}}).status_code, 400)
        TaxProfile.objects.filter(pk=st["id"]).update(active=False)
        self.assertEqual(self.api("PATCH", f"/api/v1/products/{self.product.id}", {"tax_profile_id": st["id"]}).status_code, 409)
        self.assertEqual(self.api("PATCH", f"/api/v1/products/{self.product.id}", {"tax_profile_id": None}).json()["tax_profile_id"], None)
        cashier = self.api("GET", f"/api/v1/products/{self.product.id}", role="cashier").json()
        self.assertIn("fiscal", cashier)
        self.assertNotIn("cost_cents", cashier)

    def test_legacy_tax_fields_still_feed_the_profile(self):
        Product.objects.filter(pk=self.product.pk).update(tax_fields={"csosn": "500", "cfop": "5405", "cest": "0300700"})
        body = self.api("GET", f"/api/v1/products/{self.product.id}").json()
        self.assertTrue(body["fiscal"]["ready"], body["fiscal"])

    def test_status_and_simulation(self):
        self.api("PATCH", "/api/v1/ops/company", COMPANY)
        Product.objects.create(tenant=self.tenant, sku="SEMNCM", name="Sem NCM", price_cents=100)
        status = self.api("GET", "/api/v1/fiscal/status", role="finance").json()
        self.assertTrue(status["company"]["ready"])
        self.assertEqual((status["products"]["ready"], status["products"]["pending"]), (1, 1))
        self.assertEqual(status["products"]["top_missing"][0]["count"], 1)
        IbptRate.objects.create(uf="SP", ncm="22021000", federal_national=Decimal("13.45"), federal_imported=Decimal("15.45"),
                                state=Decimal("18"), municipal=Decimal("0"), version="26.2.A")
        result = self.api("POST", "/api/v1/fiscal/simulate",
                          {"items": [{"product_id": str(self.product.id), "quantity_milli": 2000}], "discount_cents": 98})
        self.assertEqual(result.status_code, 200, result.content)
        doc = result.json()
        self.assertEqual(doc["schema"], "pdv2.fiscal.v1")
        self.assertEqual(doc["totals"]["products_cents"], 1798)
        self.assertEqual(doc["totals"]["total_cents"], 1700)
        self.assertEqual(doc["items"][0]["icms"]["group"], "ICMSSN102")
        self.assertEqual(doc["items"][0]["approx_tax"]["source"], "ibpt")
        self.assertEqual(doc["totals"]["approx_tax_cents"], 229 + 306)  # 1700 x 13.45% + 1700 x 18%
        self.assertEqual(self.api("POST", "/api/v1/fiscal/simulate", {"items": []}).status_code, 400)
        self.assertEqual(self.api("POST", "/api/v1/fiscal/simulate", {"items": [{"product_id": str(self.product.id),
                                                                                  "quantity_milli": 1000}]},
                                  role="cashier").status_code, 403)
        reference = self.api("GET", "/api/v1/fiscal/reference", role="cashier").json()
        self.assertIn({"code": "5405", "label": reference["nfce_cfops"][5]["label"]}, reference["nfce_cfops"])
        self.assertEqual(len(reference["ufs"]), 27)

    def test_load_ibpt_command(self):
        content = ("codigo;ex;tipo;descricao;nacionalfederal;importadosfederal;estadual;municipal;vigenciainicio;"
                   "vigenciafim;chave;versao;fonte\n22021000;;0;Refrigerante;13.45;15.45;18.00;0.00;01/10/2026;"
                   "31/12/2099;X;26.2.A;IBPT\n")
        with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, encoding="utf-8") as handle:
            handle.write(content)
        try:
            call_command("load_ibpt", handle.name, uf="sp", stdout=open(os.devnull, "w"))
            call_command("load_ibpt", handle.name, uf="SP", stdout=open(os.devnull, "w"))
        finally:
            os.unlink(handle.name)
        self.assertEqual(IbptRate.objects.filter(uf="SP").count(), 1)

    # ---------------------------------------------------------------- CSV import

    def test_csv_import_with_fiscal_columns(self):
        Category.objects.create(tenant=self.tenant, name="Bebidas")
        st = self.api("POST", "/api/v1/tax-profiles", {"preset": "simples_st"}).json()
        csv_text = ("nome;sku;ean;preço;unidade;categoria;NCM;CEST;CFOP;CSOSN;perfil tributário;origem\n"
                    f"Refrigerante 2L;REFRI;;9,49;UN;Bebidas;22021000;0300700;;;{st['name']};0\n"
                    "Cerveja lata;;7894900011517;4,99;UN;Bebidas;22030000;0302100;5405;500;;0\n"
                    "Pão francês;;;15,90;KG;;19052090;;;;;\n")
        preview = self.api("POST", "/api/v1/products/import", {"csv": csv_text, "dry_run": True})
        self.assertEqual(preview.status_code, 200, preview.content)
        self.assertEqual((preview.json()["created"], preview.json()["updated"], preview.json()["errors"]), (2, 1, []))
        self.assertEqual(Product.objects.filter(tenant=self.tenant).count(), 1)
        applied = self.api("POST", "/api/v1/products/import", {"csv": csv_text})
        self.assertEqual(applied.status_code, 200, applied.content)
        refri = Product.objects.get(pk=self.product.pk)
        self.assertEqual((refri.price_cents, str(refri.tax_profile_id), refri.fiscal_overrides["cest"]),
                         (949, st["id"], "0300700"))
        beer = Product.objects.get(tenant=self.tenant, barcode="7894900011517")
        self.assertEqual((beer.ncm, beer.fiscal_overrides["icms_csosn"], beer.category.name), ("22030000", "500", "Bebidas"))
        bread = Product.objects.get(tenant=self.tenant, name="Pão francês")
        self.assertEqual((bread.unit, bread.price_cents, bread.tax_profile.is_default), ("KG", 1590, True))
        broken = ("nome;preco;ncm;cst\nX;1,00;123;00\nY;abc;22021000;\n;;;\nZ;2,00;22021000;61\n")
        refused = self.api("POST", "/api/v1/products/import", {"csv": broken})
        self.assertEqual(refused.status_code, 400)
        self.assertEqual([e["line"] for e in refused.json()["errors"]], [2, 3, 5])
        self.assertEqual(self.api("POST", "/api/v1/products/import", {"csv": csv_text}, role="finance").status_code, 403)


class FiscalTerminalSyncTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.tenant = Tenant.objects.create(name="Mercado Sync", document="11222333000181", address_state="SP",
                                           crt=1, city_ibge_code="3550308")
        cls.shop = Shop.objects.create(tenant=cls.tenant, name="Matriz")
        cls.sub = Subscription.objects.create(tenant=cls.tenant, plan=Plan.objects.get(slug="essential"), status="trial",
                                              trial_ends_at=timezone.now() + timedelta(days=14))
        cls.ready = Product.objects.create(tenant=cls.tenant, sku="OK", name="Arroz", price_cents=999, ncm="10063021")
        cls.pending = Product.objects.create(tenant=cls.tenant, sku="NO", name="Sem NCM", price_cents=100)

    def test_bootstrap_carries_resolved_fiscal_data_and_issuer(self):
        code = "fiscal-pair-1"
        Terminal.objects.create(tenant=self.tenant, shop=self.shop, name="Caixa",
                                pairing_code_hash=hashlib.sha256(code.encode()).hexdigest(),
                                pairing_expires_at=timezone.now() + timedelta(minutes=10))
        private, public = generate_keypair()
        with patch.dict(os.environ, {"PDV_LICENSE_PRIVATE_KEY": private, "PDV_LICENSE_PUBLIC_KEY": public,
                                    "PDV_LICENSE_PRIVATE_KEY_FILE": "", "PDV_LICENSE_PUBLIC_KEY_FILE": ""}):
            paired = self.client.post("/api/v1/terminal/pair", json.dumps(
                {"code": code, "device_id": "device-fiscal", "version": "2.1", "license_key": self.sub.license_key}),
                content_type="application/json")
            self.assertEqual(paired.status_code, 200, paired.content)
            boot = self.client.get("/api/v1/terminal/bootstrap", HTTP_AUTHORIZATION="Bearer " + paired.json()["token"])
        self.assertEqual(boot.status_code, 200, boot.content)
        body = boot.json()
        products = {p["sku"]: p for p in body["products"]}
        self.assertTrue(products["OK"]["fiscal"]["ready"])
        self.assertEqual(products["OK"]["fiscal"]["fields"]["icms_csosn"], "102")
        self.assertEqual(products["OK"]["fiscal"]["fields"]["ncm"], "10063021")
        self.assertFalse(products["NO"]["fiscal"]["ready"])
        self.assertTrue(products["NO"]["fiscal"]["missing"][0].startswith("Falta NCM"))
        self.assertEqual(body["issuer"]["crt"], 1)
        self.assertEqual(body["issuer"]["city_ibge_code"], "3550308")
        self.assertIn("tech_responsible", body["issuer"])
        self.assertIn("fiscal_ready", body["issuer"])


class FiscalBackfillMigrationTests(TransactionTestCase):
    def test_existing_companies_get_crt_default_profile_and_legacy_fields(self):
        before = [("core", "0041_app_gestao")]
        after = [("core", "0043_fiscal_backfill")]
        executor = MigrationExecutor(connection)
        executor.migrate(before)
        try:
            old = executor.loader.project_state(before).apps
            TenantOld, ProductOld = old.get_model("core", "Tenant"), old.get_model("core", "Product")
            simples = TenantOld.objects.create(name="Simples", tax_regime="simples_nacional")
            normal = TenantOld.objects.create(name="Normal", tax_regime="regime_normal")
            excess = TenantOld.objects.create(name="Excesso", tax_regime="simples_excesso")
            a = ProductOld.objects.create(tenant=simples, sku="A", name="A", ncm="22021000",
                                          tax_fields={"CST": "60", "csosn": "500", "cest": "0300700", "x": "y"})
            b = ProductOld.objects.create(tenant=normal, sku="B", name="B", ncm="10063021",
                                          tax_fields={"aliquota_icms": "18,5", "cfop": "51O2"})
            c = ProductOld.objects.create(tenant=excess, sku="C", name="C")
            executor = MigrationExecutor(connection)
            executor.migrate(after)
            new = executor.loader.project_state(after).apps
            TenantNew, ProductNew, ProfileNew = (new.get_model("core", "Tenant"), new.get_model("core", "Product"),
                                                 new.get_model("core", "TaxProfile"))
            self.assertEqual([TenantNew.objects.get(pk=t.pk).crt for t in (simples, normal, excess)], [1, 3, 2])
            for tenant, code in ((simples, ("icms_csosn", "102")), (normal, ("icms_cst", "00")), (excess, ("icms_cst", "00"))):
                profile = ProfileNew.objects.get(tenant_id=tenant.pk, is_default=True)
                self.assertEqual(profile.fields[code[0]], code[1])
            product_a = ProductNew.objects.get(pk=a.pk)
            self.assertEqual(product_a.fiscal_overrides, {"icms_csosn": "500", "cest": "0300700"})
            self.assertEqual(product_a.ncm, "22021000")
            self.assertEqual(product_a.tax_fields["x"], "y")
            self.assertEqual(ProductNew.objects.get(pk=b.pk).fiscal_overrides, {"icms_rate": "18.5"})
            self.assertEqual(ProductNew.objects.get(pk=c.pk).tax_profile.tenant_id, excess.pk)
        finally:
            MigrationExecutor(connection).migrate(MigrationExecutor(connection).loader.graph.leaf_nodes())
