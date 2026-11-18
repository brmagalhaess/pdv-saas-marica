"""NFC-e na nuvem: configuração, certificado, contrato dos dois backends, emissão pelo caixa,
contingência, cancelamento, inutilização, impressão por forma de pagamento e permissões.

Sem rede: a SEFAZ é ``shared.nfce.testing.FakeSefaz`` e a Focus NFe é ``FakeFocus``.
"""
import base64
import hashlib
import json
import os
import uuid
from datetime import datetime, timedelta, timezone as dt_timezone
from unittest.mock import patch

from cryptography.fernet import Fernet
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from integrations.base import ProviderError
from integrations.fiscal import FocusNFe
from operations.models import FiscalPayload, IntegrationAccount, ProviderJob
from operations.services import run_one
from shared.fiscal import compute_document
from shared.licensing import generate_keypair
from shared.nfce import build_nfce, keys, qrcode, signature
from shared.nfce.testing import (FakeSefaz, authorize_ok, make_certificate, ret_authorization, ret_consult, ret_event,
                                 ret_inutilization, ret_status)
from shared.nfce.xml import from_etree, parse, tostring
from .models import (FiscalConfig, FiscalDocument, FiscalEvent, FiscalInutilization, Membership, NfceReservation,
                     PaymentMethod, Plan, Product, Sale, SaleItem, SalePayment, Shop, Subscription, Tenant, Terminal)
from .nfce import service
from .nfce.backends import FocusBackend, IssueRequest, Ref, SefazBackend
from .nfce.certificate import load_pfx
from .nfce.transport import TransportError

COMPANY = dict(name="Mercado Fiscal Ltda", trade_name="Mercado Fiscal", document="11222333000181", address_state="SP",
               state_registration="110042490114", address_zip="01310100", address_line="Av. Paulista",
               address_number="1000", address_neighborhood="Bela Vista", address_city="Sao Paulo",
               city_ibge_code="3550308", cnae="4711302", crt=1, fiscal_environment="homologation")
CSC = "CSC0TESTE0123456789ABCDEF"


class FakeFocus:
    """Stand-in for integrations.fiscal.FocusNFe (no network)."""
    normalized_status = staticmethod(FocusNFe.normalized_status)

    def __init__(self):
        self.submitted, self.cancelled, self.state = [], [], {}
        self.timeout = (5, 35)

    def submit(self, reference, payload):
        self.submitted.append((reference, payload))
        key = keys.access_key("SP", "2611", "11222333000181", 65, payload.get("serie", 1), payload.get("numero", 1), 1, "12345670")
        self.state[reference] = {"status": "autorizado", "status_sefaz": "100", "mensagem_sefaz": "Autorizado o uso da NF-e",
                                 "chave_nfe": "NFe" + key, "numero": str(payload.get("numero", 1)),
                                 "serie": str(payload.get("serie", 1)), "numero_protocolo": "135260000000555",
                                 "caminho_xml_nota_fiscal": "/arquivos/" + key + "-nfe.xml",
                                 "qrcode_url": "https://www.homologacao.nfce.fazenda.sp.gov.br/qrcode?p=" + key + "|3|2",
                                 "url_consulta_nfce": "https://www.homologacao.nfce.fazenda.sp.gov.br/NFCeConsultaPublica"}
        return self.state[reference]

    def get(self, reference):
        if reference not in self.state:
            raise ProviderError("não encontrado", status=404)
        return self.state[reference]

    def cancel(self, reference, reason):
        self.cancelled.append((reference, reason))
        self.state[reference] = dict(self.state[reference], status="cancelado", protocolo_cancelamento="135260000000556")

    def inutilize(self, cnpj, series, start, end, reason):
        return {"status": "autorizado", "protocolo_sefaz": "135260000000557", "status_sefaz": "102"}

    def download_xml(self, path, key, protocol):
        return b"<nfeProc>" + key.encode() + b"</nfeProc>"

    def download_event_xml(self, path, key):
        return b"<procEventoNFe/>"


class NfceTestBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.tenant = Tenant.objects.create(**COMPANY)
        cls.users = {}
        cls.members = {}
        for role in ("owner", "admin", "manager", "cashier", "finance"):
            user = User.objects.create_user(username=f"{role}@nfce.test", email=f"{role}@nfce.test", password="Synthetic-42!")
            cls.members[role] = Membership.objects.create(tenant=cls.tenant, user=user, name=role, role=role)
            cls.users[role] = user
        cls.shop = Shop.objects.create(tenant=cls.tenant, name="Matriz")
        Subscription.objects.create(tenant=cls.tenant, plan=Plan.objects.get(slug="management"), status="trial",
                                    trial_ends_at=timezone.now() + timedelta(days=14))
        cls.product = Product.objects.create(tenant=cls.tenant, sku="BISC", name="Biscoito recheado", price_cents=899,
                                             ncm="19053100", barcode="7891000100103")
        cls.token = "terminal-token-" + uuid.uuid4().hex
        cls.terminal = Terminal.objects.create(tenant=cls.tenant, shop=cls.shop, name="Caixa 1", device_id="nfce-device",
                                               token_hash=hashlib.sha256(cls.token.encode()).hexdigest())
        cls.pfx, cls.password, cls.key, cls.cert = make_certificate()

    def setUp(self):
        self.env = patch.dict(os.environ, {"PDV_ENCRYPTION_KEY": Fernet.generate_key().decode()})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.sefaz = FakeSefaz(status=ret_status(), authorize=authorize_ok(), event=self._event_answer,
                               inutilize=ret_inutilization())
        self.focus = FakeFocus()
        hooks = patch.multiple(service, TRANSPORT_FACTORY=lambda certificate, timeout: self.sefaz,
                               FOCUS_FACTORY=lambda token, environment: self.focus)
        hooks.start()
        self.addCleanup(hooks.stop)

    @staticmethod
    def _event_answer(body):
        key = body.find("evento/infEvento/chNFe").text
        return ret_event(key)

    # helpers ------------------------------------------------------------------

    def api(self, verb, path, body=None, role="owner"):
        self.client.force_login(self.users[role])
        return self.client.generic(verb, path, data=json.dumps(body) if body is not None else None,
                                   content_type="application/json", HTTP_X_TENANT_ID=str(self.tenant.id))

    def terminal_api(self, verb, path, body=None):
        return self.client.generic(verb, "/api/v1/terminal/" + path, data=json.dumps(body) if body is not None else None,
                                   content_type="application/json", HTTP_AUTHORIZATION=f"Bearer {self.token}")

    def configure_sefaz(self, csc=True):
        response = self.api("POST", "/api/v1/ops/fiscal/certificate",
                            {"pfx_base64": base64.b64encode(self.pfx).decode(), "password": self.password})
        self.assertEqual(response.status_code, 200, response.content)
        body = {"mode": "sefaz"}
        if csc:
            body.update(csc_homologation_id="000001", csc_homologation=CSC)
        self.assertEqual(self.api("PATCH", "/api/v1/ops/fiscal/config", body).status_code, 200)

    def sale(self, payments=None, quantity=2000, discount=100, document=""):
        gross = 1798 * quantity // 2000
        total = gross - discount
        payments = payments or [{"method": "cash", "amount_cents": total + 202}]
        change = sum(p["amount_cents"] for p in payments) - total
        sale = Sale.objects.create(id=uuid.uuid4(), tenant=self.tenant, shop=self.shop, terminal=self.terminal,
                                   request_id=uuid.uuid4().hex, content_hash="x", gross_cents=gross, discount_cents=discount,
                                   total_cents=total, change_cents=change, created_at=timezone.now(),
                                   customer_document_snapshot=document)
        SaleItem.objects.create(sale=sale, line_number=1, product=self.product, name=self.product.name, unit="UN",
                                quantity_milli=quantity, unit_price_cents=899, gross_cents=gross, discount_cents=discount,
                                line_total_cents=total)
        for index, payment in enumerate(payments, 1):
            SalePayment.objects.create(sale=sale, line_number=index, method=payment["method"],
                                       amount_cents=payment["amount_cents"])
        return sale

    def run_jobs(self, limit=20):
        done = 0
        while done < limit:
            ProviderJob.objects.filter(status="queued").update(available_at=timezone.now())
            if not run_one():
                break
            done += 1
        return done


class ConfigTests(NfceTestBase):
    def test_only_owner_and_admin_configure(self):
        for role in ("manager", "cashier", "finance"):
            self.assertEqual(self.api("GET", "/api/v1/ops/fiscal/config", role=role).status_code, 403)
            self.assertEqual(self.api("PATCH", "/api/v1/ops/fiscal/config", {"mode": "sefaz"}, role=role).status_code, 403)
        self.assertEqual(self.api("GET", "/api/v1/ops/fiscal/config", role="admin").status_code, 200)

    def test_secrets_are_encrypted_and_never_returned(self):
        response = self.api("PATCH", "/api/v1/ops/fiscal/config", {
            "csc_homologation_id": "000001", "csc_homologation": CSC, "focus_token": "token-falso-de-teste",
            "accountant_email": "contador@example.test", "qr_version": 2})
        self.assertEqual(response.status_code, 200, response.content)
        text = response.content.decode()
        self.assertNotIn(CSC, text)
        self.assertNotIn("token-falso-de-teste", text)
        body = response.json()
        self.assertEqual(body["csc"]["homologation"], {"id": "000001", "configured": True})
        self.assertEqual((body["focus"]["configured"], body["accountant_email"], body["qr_version"]),
                         (True, "contador@example.test", 2))
        config = FiscalConfig.objects.get(tenant=self.tenant)
        self.assertNotIn(CSC, config.csc_homologation_encrypted)
        self.assertEqual(service.csc_for(config, "homologation"), ("000001", CSC))
        account = IntegrationAccount.objects.get(tenant=self.tenant, provider="focusnfe")
        self.assertNotIn("token-falso", account.token_encrypted)
        self.assertEqual(account.environment, "homologation")
        for bad in ({"csc_homologation": "curto"}, {"csc_homologation_id": "abc"}, {"qr_version": 4},
                    {"mode": "outro"}, {"accountant_email": "nao-e-email"}, {"series": 1000}):
            with self.subTest(bad=bad):
                self.assertEqual(self.api("PATCH", "/api/v1/ops/fiscal/config", bad).status_code, 400)

    def test_production_needs_typed_confirmation_and_platform_flag(self):
        refused = self.api("PATCH", "/api/v1/ops/fiscal/config", {"environment": "production"})
        self.assertEqual((refused.status_code, refused.json()["code"]), (400, "production_confirmation"))
        with patch.dict(os.environ, {"PDV_ALLOW_LIVE_FISCAL": "0"}):
            blocked = self.api("PATCH", "/api/v1/ops/fiscal/config",
                               {"environment": "production", "confirmation": "EMITIR EM PRODUCAO"})
            self.assertEqual(blocked.status_code, 409)
        with patch.dict(os.environ, {"PDV_ALLOW_LIVE_FISCAL": "1"}):
            ok = self.api("PATCH", "/api/v1/ops/fiscal/config",
                          {"environment": "production", "confirmation": "EMITIR EM PRODUCAO"})
            self.assertEqual(ok.json()["environment"], "production")
        # The old company form cannot switch production on by itself.
        self.api("PATCH", "/api/v1/ops/fiscal/config", {"environment": "homologation"})
        self.assertEqual(self.api("PATCH", "/api/v1/ops/company", {"fiscal_environment": "production"}).status_code, 409)
        with self.assertRaises(Exception):
            service.backend_for(self.tenant, environment="production")

    def test_numbers_are_never_reused(self):
        self.configure_sefaz()
        self.terminal_api("POST", "nfce", {"sale_id": str(self.sale().id)})
        self.assertEqual(FiscalDocument.objects.get().number, 1)
        refused = self.api("PATCH", "/api/v1/ops/fiscal/config", {"next_number": 1})
        self.assertEqual(refused.status_code, 409)
        self.assertIn("Nunca reutilize", refused.json()["error"])
        self.assertEqual(self.api("PATCH", "/api/v1/ops/company", {"nfce_next_number": 1}).status_code, 409)
        self.assertEqual(self.api("PATCH", "/api/v1/ops/fiscal/config", {"next_number": 50}).json()["next_number"], 50)


class CertificateTests(NfceTestBase):
    def test_upload_shows_holder_cnpj_and_validity(self):
        response = self.api("POST", "/api/v1/ops/fiscal/certificate",
                            {"pfx_base64": base64.b64encode(self.pfx).decode(), "password": self.password})
        self.assertEqual(response.status_code, 200, response.content)
        certificate = response.json()["certificate"]
        self.assertEqual(certificate["cnpj"], "11222333000181")
        self.assertIn("EMPRESA TESTE", certificate["subject"])
        self.assertEqual(certificate["state"], "valid")
        self.assertTrue(certificate["matches_company"])
        config = FiscalConfig.objects.get(tenant=self.tenant)
        self.assertNotIn(base64.b64encode(self.pfx).decode()[:40], config.certificate_encrypted)
        self.assertNotIn(self.password, config.certificate_password_encrypted)
        self.assertNotIn(self.password, response.content.decode())
        self.assertEqual(service.certificate_for(config).cnpj, "11222333000181")

    def test_expiring_wrong_password_and_other_company(self):
        expiring, password, _key, _cert = make_certificate(days=20)
        body = self.api("POST", "/api/v1/ops/fiscal/certificate",
                        {"pfx_base64": base64.b64encode(expiring).decode(), "password": password}).json()
        self.assertEqual(body["certificate"]["state"], "expiring")
        self.assertLessEqual(body["certificate"]["days_left"], 20)
        wrong = self.api("POST", "/api/v1/ops/fiscal/certificate",
                         {"pfx_base64": base64.b64encode(self.pfx).decode(), "password": "errada"})
        self.assertEqual(wrong.status_code, 400)
        other, password, _key, _cert = make_certificate(cnpj="11444777000161")
        mismatch = self.api("POST", "/api/v1/ops/fiscal/certificate",
                            {"pfx_base64": base64.b64encode(other).decode(), "password": password})
        self.assertEqual((mismatch.status_code, mismatch.json()["code"]), (409, "certificate_cnpj_mismatch"))
        expired, password, _key, _cert = make_certificate(days=1, not_before=datetime.now(dt_timezone.utc) - timedelta(days=5))
        self.assertEqual(self.api("POST", "/api/v1/ops/fiscal/certificate",
                                  {"pfx_base64": base64.b64encode(expired).decode(), "password": password}).status_code, 400)
        self.assertEqual(self.api("DELETE", "/api/v1/ops/fiscal/certificate").json()["certificate"], None)

    def test_communication_test(self):
        self.configure_sefaz()
        body = self.api("POST", "/api/v1/ops/fiscal/test").json()
        self.assertTrue(body["online"])
        self.assertIn("107", body["message"])
        self.assertTrue(self.sefaz.calls[-1]["url"].startswith("https://homologacao.nfce.fazenda.sp.gov.br/"))
        self.sefaz.answers["status"] = TransportError("sem rede", unreachable=True)
        self.assertFalse(self.api("POST", "/api/v1/ops/fiscal/test").json()["online"])
        self.api("PATCH", "/api/v1/ops/fiscal/config", {"mode": "focusnfe", "focus_token": "token-teste"})
        self.assertTrue(self.api("POST", "/api/v1/ops/fiscal/test").json()["online"])  # Focus answers 404 for the probe


class ContractTests(NfceTestBase):
    """The same contract on both backends, with simulated transports."""

    def request(self, number=7):
        doc = compute_document({"crt": 1, "uf": "SP"}, [{
            "product_id": "p1", "code": "BISC", "description": "Biscoito", "barcode": "", "unit": "UN",
            "quantity_milli": 1000, "unit_price_cents": 500, "fiscal": {"origin": "0", "ncm": "19053100", "cfop": "5102",
                                                                       "icms_csosn": "102", "pis_cst": "49", "cofins_cst": "49"}}], model=65)
        issuer = service.issuer_data(self.tenant)
        return IssueRequest(issuer=issuer, doc=doc, environment="homologation", reference="pdv2-contrato",
                            payments=[{"method": "pix", "amount_cents": 500}], issued_at=timezone.now(), series=1,
                            number=number, cnf="12345670")

    def backends(self):
        certificate = load_pfx(self.pfx, self.password)
        return [SefazBackend(certificate, self.sefaz, "SP", "homologation"), FocusBackend(self.focus)]

    def test_issue_consult_cancel_inutilize_status(self):
        for backend in self.backends():
            with self.subTest(backend=backend.name):
                self.assertTrue(backend.status()["online"])
                result = backend.issue(self.request())
                self.assertEqual(result.status, "authorized")
                self.assertTrue(keys.valid_key(result.key))
                self.assertTrue(result.protocol)
                self.assertTrue(result.qr_url.startswith("https://"))
                if backend.name == "sefaz":
                    self.assertIn(b"<nfeProc", result.proc_xml)
                    self.sefaz.answers["consult"] = ret_consult(result.key)
                else:
                    self.assertTrue(result.xml_path)
                found = backend.consult(Ref(reference="pdv2-contrato", key=result.key, protocol=result.protocol,
                                            nfe_xml=result.nfe_xml))
                self.assertEqual(found.status, "authorized")
                cancelled = backend.cancel(Ref(reference="pdv2-contrato", key=result.key, protocol=result.protocol),
                                           "Cliente desistiu da compra no caixa")
                self.assertEqual(cancelled.status, "registered")
                self.assertTrue(cancelled.protocol)
                gap = backend.inutilize(1, 8, 9, "Falha no sistema de emissao", 2026, "11222333000181")
                self.assertEqual(gap.status, "registered")

    def test_rejection_and_duplicity_on_sefaz(self):
        backend = self.backends()[0]
        self.sefaz.answers["authorize"] = lambda body: ret_authorization(
            body.find("NFe/infNFe").attrs["Id"][3:], "778", "Rejeicao: Informado NCM inexistente")
        rejected = backend.issue(self.request())
        self.assertEqual(rejected.status, "rejected")
        self.assertIn("Corrija o NCM", rejected.message)
        self.sefaz.answers["authorize"] = lambda body: ret_authorization(
            body.find("NFe/infNFe").attrs["Id"][3:], "204", "Rejeicao: Duplicidade de NF-e")
        self.sefaz.answers["consult"] = lambda body: ret_consult(body.find("chNFe").text)
        duplicate = backend.issue(self.request())
        self.assertEqual(duplicate.status, "authorized")
        self.assertIn(b"<protNFe", duplicate.proc_xml)
        self.sefaz.answers["authorize"] = lambda body: ret_authorization(
            body.find("NFe/infNFe").attrs["Id"][3:], "110", "Uso Denegado")
        self.assertTrue(backend.issue(self.request()).message.startswith("Uso denegado"))
        # The signed NFe sent to the SEFAZ verifies against the company certificate.
        sent = self.sefaz.sent("authorize")[0].find("NFe")
        self.assertEqual(signature.verify(sent, "infNFe").serial_number, self.cert.serial_number)

    def test_focus_payload_comes_from_the_engine(self):
        backend = FocusBackend(self.focus)
        backend.issue(self.request(number=9))
        reference, body = self.focus.submitted[-1]
        self.assertEqual(reference, "pdv2-contrato")
        self.assertEqual((body["cnpj_emitente"], body["valor_total"], body["numero"]), ("11222333000181", "5.00", 9))
        item = body["items"][0]
        self.assertEqual((item["codigo_ncm"], item["cfop"], item["icms_situacao_tributaria"]), ("19053100", "5102", "102"))
        self.assertEqual(body["formas_pagamento"], [{"forma_pagamento": "17", "valor_pagamento": "5.00"}])


class TerminalFlowTests(NfceTestBase):
    def test_terminal_issue_authorizes_and_stores_nfe_proc(self):
        self.configure_sefaz()
        sale = self.sale(document="52998224725")
        response = self.terminal_api("POST", "nfce", {"sale_id": str(sale.id)})
        self.assertEqual(response.status_code, 200, response.content)
        fiscal = response.json()["fiscal"]
        self.assertEqual((fiscal["status"], fiscal["state"], fiscal["number"], fiscal["series"]), ("authorized", "authorized", 1, 1))
        self.assertTrue(keys.valid_key(fiscal["access_key"]))
        self.assertTrue(fiscal["qr_url"].startswith("https://www.homologacao.nfce.fazenda.sp.gov.br/qrcode?p="))
        self.assertTrue(fiscal["qr_url"].endswith("|3|2"))  # QR v3 online
        self.assertEqual(fiscal["url_consulta"], "https://www.homologacao.nfce.fazenda.sp.gov.br/NFCeConsultaPublica")
        self.assertEqual(fiscal["protocol"], "135260000000001")
        document = FiscalDocument.objects.get(sale=sale)
        self.assertEqual((document.model, document.emission_type, document.environment), (65, 1, "homologation"))
        self.assertIsNotNone(document.issued_at)
        payload = FiscalPayload.objects.get(document=document)
        xml = bytes(payload.xml_content)
        self.assertEqual(payload.xml_sha256, hashlib.sha256(xml).hexdigest())
        proc = from_etree(parse(xml))
        self.assertEqual(proc.tag, "nfeProc")
        self.assertEqual(proc.findtext("protNFe/infProt/nProt"), "135260000000001")
        self.assertEqual(proc.findtext("NFe/infNFe/dest/CPF"), "52998224725")
        self.assertTrue(signature.verify(proc.find("NFe"), "infNFe"))
        self.assertEqual(Sale.objects.get(pk=sale.pk).fiscal_status, "authorized")
        # Idempotent by sale: no second authorization.
        again = self.terminal_api("POST", "nfce", {"sale_id": str(sale.id)}).json()
        self.assertEqual(again["fiscal"]["access_key"], fiscal["access_key"])
        self.assertEqual(len(self.sefaz.sent("authorize")), 1)
        self.assertEqual(self.terminal_api("GET", f"nfce/{sale.id}").json()["status"], "authorized")
        # The panel lists it with its state and lets the XML be downloaded.
        listed = self.api("GET", "/api/v1/ops/fiscal/documents?sale_id=" + str(sale.id)).json()["results"][0]
        self.assertEqual((listed["status"], listed["number"], listed["xml_archived"]), ("authorized", 1, True))
        download = self.api("GET", f"/api/v1/ops/fiscal/documents/{document.pk}/xml")
        self.assertEqual(download["X-Content-SHA256"], payload.xml_sha256)
        self.assertEqual(self.api("GET", f"/api/v1/ops/fiscal/documents/{document.pk}/xml", role="cashier").status_code, 403)
        sales = self.api("GET", "/api/v1/sales").json()["results"]
        self.assertEqual(sales[0]["fiscal"]["status"], "authorized")

    def test_unknown_sale_disabled_company_and_incomplete_data(self):
        self.configure_sefaz()
        self.assertEqual(self.terminal_api("POST", "nfce", {"sale_id": str(uuid.uuid4())}).json()["code"], "sale_not_synced")
        Product.objects.filter(pk=self.product.pk).update(ncm="")
        incomplete = self.terminal_api("POST", "nfce", {"sale_id": str(self.sale().id)})
        self.assertEqual((incomplete.status_code, incomplete.json()["code"]), (422, "fiscal_data_incomplete"))
        self.assertEqual(FiscalDocument.objects.count(), 0)  # no number spent
        Tenant.objects.filter(pk=self.tenant.pk).update(fiscal_environment="non_fiscal")
        self.assertEqual(self.terminal_api("POST", "nfce", {"sale_id": str(self.sale().id)}).json()["status"], "disabled")

    def test_rejection_then_fix_and_resend_with_same_number(self):
        self.configure_sefaz()
        self.sefaz.answers["authorize"] = [
            lambda body: ret_authorization(body.find("NFe/infNFe").attrs["Id"][3:], "591", "Rejeicao: CRT diverge"),
            authorize_ok()]
        sale = self.sale()
        fiscal = self.terminal_api("POST", "nfce", {"sale_id": str(sale.id)}).json()["fiscal"]
        self.assertEqual(fiscal["status"], "rejected")
        self.assertIn("regime tributário", fiscal["error"])
        document = FiscalDocument.objects.get(sale=sale)
        self.assertEqual(Sale.objects.get(pk=sale.pk).fiscal_status, "rejected")
        retried = self.api("POST", f"/api/v1/ops/fiscal/documents/{document.pk}/retry")
        self.assertEqual(retried.status_code, 202, retried.content)
        self.run_jobs()
        document.refresh_from_db()
        self.assertEqual((document.status, document.number), ("authorized", 1))
        self.assertEqual(self.tenant.__class__.objects.get(pk=self.tenant.pk).nfce_next_number, 2)

    def test_sefaz_unreachable_issues_contingency_in_the_cloud_and_transmits_later(self):
        self.configure_sefaz()
        self.sefaz.answers["authorize"] = [TransportError("sem rede", unreachable=True), authorize_ok()]
        sale = self.sale()
        fiscal = self.terminal_api("POST", "nfce", {"sale_id": str(sale.id)}).json()["fiscal"]
        self.assertEqual((fiscal["status"], fiscal["state"], fiscal["emission_type"]), ("contingency", "contingency", 9))
        self.assertEqual(fiscal["access_key"][34], "9")
        self.assertEqual(fiscal["number"], 1)  # nothing reached the SEFAZ: same number
        params = fiscal["qr_url"].split("?p=")[1].split("|")
        self.assertEqual(params[1:3], ["3", "2"])
        self.assertEqual(len(params), 8)  # v3 offline, signed with the certificate
        self.assertEqual(service.contingency_alerts(self.tenant), {"pending": 1, "overdue": 0})
        self.run_jobs()
        document = FiscalDocument.objects.get(sale=sale)
        self.assertEqual((document.status, document.emission_type), ("authorized", 9))
        self.assertIn(b"<tpEmis>9</tpEmis>", bytes(FiscalPayload.objects.get(document=document).xml_content))

    def test_lost_answer_is_replaced_by_contingency_and_old_number_inutilized(self):
        self.configure_sefaz()
        self.sefaz.answers["authorize"] = [TransportError("tempo esgotado", outcome_unknown=True), authorize_ok()]
        self.sefaz.answers["consult"] = [lambda body: ret_consult(body.find("chNFe").text, "217", "NF-e nao consta"),
                                         lambda body: ret_consult(body.find("chNFe").text, "217", "NF-e nao consta")]
        sale = self.sale()
        fiscal = self.terminal_api("POST", "nfce", {"sale_id": str(sale.id)}).json()["fiscal"]
        self.assertEqual((fiscal["status"], fiscal["number"]), ("contingency", 2))
        old = FiscalDocument.objects.get(sale=sale, number=1)
        self.assertIsNotNone(old.replaced_by_id)
        self.run_jobs()
        old.refresh_from_db()
        self.assertEqual(old.status, "rejected")
        gap = FiscalInutilization.objects.get(tenant=self.tenant)
        self.assertEqual((gap.start, gap.end, gap.status), (1, 1, "done"))
        self.assertIn(b"procInutNFe", bytes(gap.xml_content))
        self.assertEqual(FiscalDocument.objects.get(sale=sale, number=2).status, "authorized")
        self.assertEqual(Sale.objects.get(pk=sale.pk).fiscal_status, "authorized")

    def test_cancel_within_deadline_stores_proc_evento(self):
        self.configure_sefaz()
        sale = self.sale()
        self.terminal_api("POST", "nfce", {"sale_id": str(sale.id)})
        document = FiscalDocument.objects.get(sale=sale)
        short = self.api("POST", f"/api/v1/ops/fiscal/documents/{document.pk}/cancel", {"reason": "curto"})
        self.assertEqual(short.status_code, 400)
        response = self.terminal_api("POST", f"nfce/{sale.id}/cancel",
                                     {"reason": "Cliente desistiu da compra no caixa", "manager_id": str(self.members["manager"].pk)})
        self.assertEqual(response.status_code, 202, response.content)
        self.run_jobs()
        document.refresh_from_db()
        self.assertEqual(document.status, "cancelled")
        self.assertIsNotNone(document.cancelled_at)
        event = FiscalEvent.objects.get(document=document)
        self.assertEqual((event.status, event.event_type, event.protocol), ("registered", "110111", "135260000000099"))
        proc = from_etree(parse(bytes(event.xml_content)))
        self.assertEqual(proc.tag, "procEventoNFe")
        self.assertTrue(signature.verify(proc.find("evento"), "infEvento"))
        self.assertEqual(Sale.objects.get(pk=sale.pk).fiscal_status, "cancelled")
        self.assertEqual(self.api("GET", f"/api/v1/ops/fiscal/events/{event.pk}/xml").status_code, 200)
        cashier = self.terminal_api("POST", f"nfce/{sale.id}/cancel",
                                    {"reason": "Cliente desistiu da compra no caixa", "manager_id": str(self.members["cashier"].pk)})
        self.assertEqual(cashier.status_code, 403)

    def test_cancel_after_deadline_is_refused(self):
        self.configure_sefaz()
        sale = self.sale()
        self.terminal_api("POST", "nfce", {"sale_id": str(sale.id)})
        document = FiscalDocument.objects.get(sale=sale)
        FiscalDocument.objects.filter(pk=document.pk).update(authorized_at=timezone.now() - timedelta(minutes=31))
        refused = self.api("POST", f"/api/v1/ops/fiscal/documents/{document.pk}/cancel",
                           {"reason": "Cliente desistiu da compra no caixa"})
        self.assertEqual((refused.status_code, refused.json()["code"]), (409, "fiscal_cancel_expired"))

    def test_inutilization_from_the_panel(self):
        self.configure_sefaz()
        self.assertEqual(self.api("POST", "/api/v1/ops/fiscal/inutilize",
                                  {"start": 5, "end": 7, "reason": "curta"}).status_code, 400)
        response = self.api("POST", "/api/v1/ops/fiscal/inutilize",
                            {"start": 5, "end": 7, "reason": "Falha no sistema anterior de emissao"})
        self.assertEqual(response.status_code, 202, response.content)
        self.run_jobs()
        row = FiscalInutilization.objects.get()
        self.assertEqual((row.status, row.protocol), ("done", "135260000000077"))
        body = self.sefaz.sent("inutilize")[0]
        self.assertEqual(body.find("infInut").attrs["Id"], "ID35" + str(timezone.now().year)[-2:] + "1122233300018165001000000005000000007")
        self.assertEqual(self.api("POST", "/api/v1/ops/fiscal/inutilize",
                                  {"start": 5, "end": 7, "reason": "Falha no sistema anterior"}, role="manager").status_code, 403)

    def test_void_at_the_register_requests_the_cancellation(self):
        self.configure_sefaz()
        sale = self.sale()
        self.terminal_api("POST", "nfce", {"sale_id": str(sale.id)})
        from .sale_logic import void_from_terminal
        conflicts = []
        from django.db import transaction
        with transaction.atomic():
            void_from_terminal(self.terminal, {"aggregate_id": str(sale.id), "event_id": uuid.uuid4().hex,
                                               "type": "sale.voided",
                                               "data": {"sale_id": str(sale.id), "reason": "Cliente desistiu da compra",
                                                        "created_at": int(timezone.now().timestamp() * 1000)}}, conflicts)
        self.assertEqual(conflicts, [])
        self.run_jobs()
        self.assertEqual(FiscalDocument.objects.get(sale=sale).status, "cancelled")


class TerminalContingencyTests(NfceTestBase):
    def offline_xml(self, sale, reservation, number=None):
        """What the register builds with shared/nfce while offline (QR v2 with CSC)."""
        doc = compute_document({"crt": 1, "uf": "SP"}, [{
            "product_id": str(self.product.id), "code": "BISC", "description": self.product.name,
            "barcode": self.product.barcode, "unit": "UN", "quantity_milli": 2000, "unit_price_cents": 899,
            "discount_cents": 100, "fiscal": {"origin": "0", "ncm": "19053100", "cfop": "5102", "icms_csosn": "102",
                                              "pis_cst": "49", "cofins_cst": "49"}}], model=65)
        moment = timezone.now()
        draft = build_nfce(reservation["issuer"], doc, series=reservation["series"],
                           number=number or reservation["numbers"][0], issued_at=moment,
                           environment=reservation["environment"], emission_type=9,
                           contingency={"at": moment, "reason": "Caixa sem internet no momento da venda"},
                           payments=[{"method": "cash", "amount_cents": sale.total_cents + sale.change_cents}],
                           change_cents=sale.change_cents)
        qr = qrcode.build_v2(draft.key, draft.environment_code, reservation["csc_id"], reservation["csc"], uf="SP",
                             environment=reservation["environment"], offline=True, issued_at=draft.issued_at,
                             total_cents=draft.total_cents, digest_value=draft.digest_value)
        draft.supplement(qr, "https://www.homologacao.nfce.fazenda.sp.gov.br/NFCeConsultaPublica")
        return draft, tostring(draft.nfe)

    def test_reservation_requires_csc_and_sefaz_mode(self):
        self.configure_sefaz(csc=False)
        self.assertEqual(self.terminal_api("POST", "nfce/reserve").status_code, 409)
        self.api("PATCH", "/api/v1/ops/fiscal/config", {"csc_homologation_id": "000001", "csc_homologation": CSC})
        reservation = self.terminal_api("POST", "nfce/reserve").json()
        self.assertEqual((reservation["numbers"][0], len(reservation["numbers"]), reservation["qr_version"]), (1, 10, 2))
        self.assertEqual(reservation["csc"], CSC)
        self.assertEqual(Tenant.objects.get(pk=self.tenant.pk).nfce_next_number, 11)
        # Same block while numbers remain.
        self.assertEqual(self.terminal_api("POST", "nfce/reserve").json()["id"], reservation["id"])
        self.api("PATCH", "/api/v1/ops/fiscal/config", {"mode": "focusnfe"})
        self.assertEqual(self.terminal_api("POST", "nfce/reserve").status_code, 409)

    def test_register_contingency_is_signed_transmitted_and_authorized(self):
        self.configure_sefaz()
        reservation = self.terminal_api("POST", "nfce/reserve").json()
        sale = self.sale()
        draft, xml = self.offline_xml(sale, reservation)
        response = self.terminal_api("POST", "nfce/contingency", {"sale_id": str(sale.id), "xml": xml})
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["fiscal"]["state"], "contingency")
        document = FiscalDocument.objects.get(sale=sale)
        self.assertEqual((document.access_key, document.number, document.emission_type), (draft.key, 1, 9))
        signed = from_etree(parse(bytes(FiscalPayload.objects.get(document=document).nfe_xml)))
        # The cloud signs exactly what the register printed: same DigestValue as the QR Code v2.
        self.assertEqual(signed.findtext("Signature/SignedInfo/Reference/DigestValue"), draft.digest_value)
        self.assertTrue(signature.verify(signed, "infNFe"))
        # Repeating the upload is idempotent.
        self.assertEqual(self.terminal_api("POST", "nfce/contingency", {"sale_id": str(sale.id), "xml": xml}).status_code, 200)
        self.run_jobs()
        document.refresh_from_db()
        self.assertEqual(document.status, "authorized")
        self.assertEqual(NfceReservation.objects.get().used, [1])

    def test_register_contingency_validations(self):
        self.configure_sefaz()
        reservation = self.terminal_api("POST", "nfce/reserve").json()
        sale = self.sale()
        _draft, xml = self.offline_xml(sale, reservation, number=999)
        self.assertEqual(self.terminal_api("POST", "nfce/contingency", {"sale_id": str(sale.id), "xml": xml}).status_code, 409)
        _draft, xml = self.offline_xml(sale, reservation)
        other = self.sale(quantity=4000)
        self.assertEqual(self.terminal_api("POST", "nfce/contingency", {"sale_id": str(other.id), "xml": xml}).status_code, 422)
        self.assertEqual(self.terminal_api("POST", "nfce/contingency",
                                           {"sale_id": str(sale.id), "xml": "<!DOCTYPE x><x/>"}).status_code, 400)

    def test_released_reservation_inutilizes_unused_numbers(self):
        self.configure_sefaz()
        reservation = self.terminal_api("POST", "nfce/reserve").json()
        sale = self.sale()
        _draft, xml = self.offline_xml(sale, reservation)
        self.terminal_api("POST", "nfce/contingency", {"sale_id": str(sale.id), "xml": xml})
        NfceReservation.objects.update(created_at=timezone.now() - timedelta(days=30))
        service.sweep()
        self.run_jobs()
        gap = FiscalInutilization.objects.get()
        self.assertEqual((gap.start, gap.end, gap.status), (2, 10, "done"))


class FocusModeTests(NfceTestBase):
    def test_terminal_issue_through_focus_and_archive(self):
        self.api("PATCH", "/api/v1/ops/fiscal/config", {"mode": "focusnfe", "focus_token": "token-teste"})
        sale = self.sale()
        fiscal = self.terminal_api("POST", "nfce", {"sale_id": str(sale.id)}).json()["fiscal"]
        self.assertEqual(fiscal["status"], "authorized")
        self.assertEqual(fiscal["protocol"], "135260000000555")
        reference, payload = self.focus.submitted[0]
        self.assertEqual(reference, "pdv2-" + str(sale.id))
        self.assertEqual(payload["valor_total"], "16.98")
        self.run_jobs()
        document = FiscalDocument.objects.get(sale=sale)
        self.assertTrue(FiscalPayload.objects.get(document=document).xml_sha256)
        self.api("POST", f"/api/v1/ops/fiscal/documents/{document.pk}/cancel", {"reason": "Cliente desistiu da compra no caixa"})
        self.run_jobs()
        document.refresh_from_db()
        self.assertEqual(document.status, "cancelled")
        self.assertEqual(FiscalEvent.objects.get(document=document).status, "registered")

    def test_panel_submit_no_longer_needs_a_ready_payload(self):
        self.api("PATCH", "/api/v1/ops/fiscal/config", {"mode": "focusnfe", "focus_token": "token-teste"})
        sale = self.sale()
        response = self.api("POST", "/api/v1/ops/fiscal/submit", {"sale_id": str(sale.id)})
        self.assertEqual(response.status_code, 202, response.content)
        self.run_jobs()
        self.assertEqual(FiscalDocument.objects.get(sale=sale).status, "authorized")
        self.assertEqual(self.api("POST", "/api/v1/ops/fiscal/submit", {"sale_id": str(sale.id)}, role="cashier").status_code, 403)


class PrintRuleTests(NfceTestBase):
    def test_print_rules_default_edit_and_bootstrap(self):
        rows = {row["kind"]: row for row in self.api("GET", "/api/v1/payment-methods").json()["results"]}
        self.assertEqual((rows["cash"]["print_rule"], rows["pix"]["print_rule"], rows["card_debit"]["print_rule"],
                          rows["card_credit"]["print_rule"]), ("ask", "ask", "always", "always"))
        cash = PaymentMethod.objects.get(tenant=self.tenant, kind="cash")
        self.assertEqual(self.api("PATCH", f"/api/v1/payment-methods/{cash.id}", {"print_rule": "talvez"}).status_code, 400)
        self.assertEqual(self.api("PATCH", f"/api/v1/payment-methods/{cash.id}", {"print_rule": "never"}).json()["print_rule"], "never")
        self.configure_sefaz()
        private, public = generate_keypair()
        with patch.dict(os.environ, {"PDV_LICENSE_PRIVATE_KEY": private, "PDV_LICENSE_PUBLIC_KEY": public,
                                    "PDV_LICENSE_PRIVATE_KEY_FILE": "", "PDV_LICENSE_PUBLIC_KEY_FILE": ""}):
            bootstrap = self.client.get("/api/v1/terminal/bootstrap", HTTP_AUTHORIZATION=f"Bearer {self.token}").json()
        self.assertIn("nfce", bootstrap, bootstrap)
        nfce = bootstrap["nfce"]
        self.assertEqual((nfce["enabled"], nfce["environment"], nfce["mode"], nfce["contingency"]),
                         (True, "homologation", "sefaz", True))
        self.assertEqual(nfce["print_rules"]["cash"], "never")
        self.assertNotIn(CSC, json.dumps(bootstrap))
        self.assertEqual({row["kind"]: row["print_rule"] for row in bootstrap["payment_methods"]}["card_debit"], "always")
