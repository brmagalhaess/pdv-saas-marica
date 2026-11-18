"""Pacote mensal do contador: leitura dos XMLs, ZIP, resumo, job e e-mail, permissões, isolamento e SPED."""
import csv
import io
import json
import re
import uuid
import zipfile
from datetime import datetime, timedelta, timezone as dt_timezone
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core import mail
from django.test import TestCase
from django.utils import timezone

from operations.models import FiscalPayload, ProviderJob
from operations.services import run_one
from shared.fiscal import efd
from shared.tests import nfce_samples as S

from .accountant import jobs, sources
from .accountant import package as package_module
from .accountant.models import AccountantDelivery, AccountantPackage, AccountantSettings
from .models import FiscalDocument, Membership, Plan, Sale, Shop, Subscription, Tenant

OCT = 2026, 10


class AccountantBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        plan = Plan.objects.get(slug="essential")
        cls.tenant = Tenant.objects.create(
            name="Mercado Fiscal Ltda", document=S.CNPJ, state_registration=S.IE, address_state="SP",
            city_ibge_code="3550308", address_zip="01310100", address_line="Av. Paulista", address_number="1000",
            address_neighborhood="Bela Vista", address_city="São Paulo", crt=3, tax_regime="regime_normal",
            accountant_email="contador@escritorio.test")
        cls.other = Tenant.objects.create(name="Outro Mercado", document="45723174000110", crt=3,
                                          accountant_email="outro@escritorio.test")
        for tenant in (cls.tenant, cls.other):
            Subscription.objects.create(tenant=tenant, plan=plan, status="trial",
                                        trial_ends_at=timezone.now() + timedelta(days=14))
        cls.users = {}
        for role in ("owner", "admin", "finance", "manager", "cashier"):
            user = User.objects.create_user(username=f"{role}@contador.test", password="Synthetic-42!")
            Membership.objects.create(tenant=cls.tenant, user=user, name=role, role=role)
            cls.users[role] = user
        cls.other_user = User.objects.create_user(username="dono@outro.test", password="Synthetic-42!")
        Membership.objects.create(tenant=cls.other, user=cls.other_user, name="Outro", role="owner")
        cls.shop = Shop.objects.create(tenant=cls.tenant, name="Matriz")
        cls.other_shop = Shop.objects.create(tenant=cls.other, name="Matriz B")
        AccountantSettings.objects.create(tenant=cls.tenant, name="Ana Contadora", cpf="52998224725",
                                          crc="SP123456O8", city_ibge="3550308", e116_cod_rec="046-2",
                                          e116_due_day=20)

    def api(self, verb, path, body=None, role="owner", tenant=None, user=None):
        self.client.force_login(user or self.users[role])
        return self.client.generic(verb, "/api/v1/accountant/" + path,
                                   data=json.dumps(body) if body is not None else None,
                                   content_type="application/json",
                                   HTTP_X_TENANT_ID=str(tenant or self.tenant.id))

    def doc(self, xml=None, status="authorized", tenant=None, created=None, error=""):
        tenant = tenant or self.tenant
        shop = self.shop if tenant == self.tenant else self.other_shop
        sale = Sale.objects.create(id=uuid.uuid4(), tenant=tenant, shop=shop, request_id=str(uuid.uuid4()),
                                   content_hash="x", gross_cents=1, total_cents=1, created_at=timezone.now())
        row = FiscalDocument.objects.create(tenant=tenant, shop=shop, sale=sale, status=status, error_message=error)
        if created is None and xml and b"<dhEmi>" in xml:
            # Como na vida real: o registro nasce na nuvem logo depois da emissão.
            created = datetime.fromisoformat(xml.split(b"<dhEmi>")[1].split(b"<")[0].decode()) + timedelta(minutes=1)
        if created:
            FiscalDocument.objects.filter(pk=row.pk).update(created_at=created)
            row.refresh_from_db()
        FiscalPayload.objects.create(document=row, xml_content=xml)
        return row

    def month_fixture(self):
        """Out/2026: nº 1, 2, 4 autorizadas, 3 cancelada, 6–7 inutilizadas, 9 em contingência, 5 rejeitada."""
        self.xmls = {}
        for number, day in ((1, 1), (2, 15), (4, 20)):
            self.xmls[number] = S.nfce(number, issued=f"2026-10-{day:02d}T10:00:00-03:00", change="2.00" if number != 4 else "0",
                                       payments=None if number != 4 else [("17", "53.90")])
            self.doc(self.xmls[number])
        self.xmls[3] = S.nfce(3, issued="2026-10-16T10:00:00-03:00")
        cancelled = self.doc(self.xmls[3], status="cancelled")
        self.cancel_xml = S.cancel_event(S.key_of(self.xmls[3]), when="2026-10-16T10:20:00-03:00")
        self.events = {cancelled.pk: [self.cancel_xml]}
        self.inut_xml = S.inutilization(6, 7, when="2026-10-21T09:00:00-03:00")
        self.doc(S.nfce(9, issued="2026-10-25T12:00:00-03:00", emission_type=9, protocol=False), status="contingency")
        self.doc(None, status="rejected", error="Rejeição 778: NCM inexistente",
                 created=datetime(2026, 10, 22, 15, 0, tzinfo=dt_timezone.utc))

    def patched(self):
        return [patch.object(sources, "load_events", lambda tenant, ids: {k: v for k, v in self.events.items() if k in ids}),
                patch.object(sources, "load_inutilization_xmls",
                             lambda tenant: [self.inut_xml] if tenant == self.tenant else [])]

    def run_jobs(self):
        while run_one():
            pass

    def start_patches(self):
        for item in self.patched():
            item.start()
            self.addCleanup(item.stop)


class SourcesTests(AccountantBase):
    def test_homologation_never_enters_the_package(self):
        production = self.doc(S.nfce(1, issued="2026-10-05T10:00:00-03:00"))
        self.doc(S.nfce(2, issued="2026-10-05T11:00:00-03:00", tp_amb=2))  # legacy row, XML says homologation
        marked = self.doc(S.nfce(3, issued="2026-10-05T12:00:00-03:00"))
        FiscalDocument.objects.filter(pk=marked.pk).update(environment="homologation")
        FiscalDocument.objects.filter(pk=production.pk).update(environment="production")
        docs = sources.iter_month(self.tenant, 2026, 10)
        self.assertEqual([d["id"] for d in docs], [str(production.pk)])
        with patch.object(sources, "load_inutilization_xmls", lambda tenant: [S.inutilization(4, 4, tp_amb=2)]):
            self.assertEqual(sources.inutilizations_month(self.tenant, 2026, 10), [])

    def test_reads_authorized_cancelled_inutilized_and_contingency_from_xml(self):
        self.month_fixture()
        self.start_patches()
        docs = sources.iter_month(self.tenant, *OCT)
        by_number = {d["number"]: d for d in docs if d["number"]}
        self.assertEqual(by_number[1]["status"], "authorized")
        self.assertEqual(by_number[1]["key"], S.key_of(self.xmls[1]))
        self.assertEqual(by_number[1]["nfe"]["totals"]["vNF"], 5390)
        self.assertEqual(by_number[3]["status"], "cancelled")
        self.assertEqual(by_number[3]["cancel"]["type"], "110111")
        self.assertEqual(by_number[9]["status"], "contingency")
        rejected = [d for d in docs if d["status"] == "rejected"]
        self.assertEqual(len(rejected), 1)
        self.assertFalse(rejected[0]["corrected"])
        inut = sources.inutilizations_month(self.tenant, *OCT)
        self.assertEqual([(i["series"], i["start"], i["end"]) for i in inut], [(1, 6, 7)])
        self.assertEqual(sources.inutilizations_month(self.tenant, 2026, 11), [])

    def test_month_boundary_uses_local_emission_date(self):
        # 23:30 em São Paulo de 31/10 é 02:30 UTC de 01/11: pertence a outubro.
        self.doc(S.nfce(10, issued="2026-10-31T23:30:00-03:00"),
                 created=datetime(2026, 11, 1, 2, 31, tzinfo=dt_timezone.utc))
        self.doc(S.nfce(11, issued="2026-11-01T00:10:00-03:00"),
                 created=datetime(2026, 11, 1, 3, 11, tzinfo=dt_timezone.utc))
        # Manaus (-04:00): 23:50 local de 30/09 continua em setembro, embora já seja 01/10 em Brasília.
        self.doc(S.nfce(12, issued="2026-09-30T23:50:00-04:00"),
                 created=datetime(2026, 10, 1, 3, 51, tzinfo=dt_timezone.utc))
        # Sem XML: cai na data local (America/Sao_Paulo) de criação.
        self.doc(None, status="rejected", created=datetime(2026, 11, 1, 2, 30, tzinfo=dt_timezone.utc))
        october = sources.iter_month(self.tenant, *OCT)
        self.assertEqual(sorted(d["number"] for d in october if d["nfe"]), [10])
        self.assertEqual(len([d for d in october if d["status"] == "rejected"]), 1)
        self.assertEqual([d["number"] for d in sources.iter_month(self.tenant, 2026, 11)], [11])
        self.assertEqual([d["number"] for d in sources.iter_month(self.tenant, 2026, 9)], [12])

    def test_model_status_fallbacks_and_unreadable_xml(self):
        self.doc(None, status="authorized", created=datetime(2026, 10, 5, 12, tzinfo=dt_timezone.utc))
        self.doc(b"<nfeProc><quebrado>", status="authorized", created=datetime(2026, 10, 6, 12, tzinfo=dt_timezone.utc))
        self.doc(S.nfce(20, issued="2026-10-07T10:00:00-03:00"), status="cancelled")
        docs = sources.iter_month(self.tenant, *OCT)
        self.assertEqual(len(docs), 3)
        self.assertTrue(any("ilegível" in w for d in docs for w in d["warnings"]))
        cancelled = [d for d in docs if d["status"] == "cancelled"][0]
        self.assertIn("sem o XML do evento", cancelled["warnings"][0])

    def test_isolated_by_tenant(self):
        self.doc(S.nfce(1, issued="2026-10-03T10:00:00-03:00"), tenant=self.other)
        self.assertEqual(sources.iter_month(self.tenant, *OCT), [])
        self.assertEqual(len(sources.iter_month(self.other, *OCT)), 1)


class PackageTests(AccountantBase):
    def generate(self, role="owner", **extra):
        response = self.api("POST", "packages", {"year": 2026, "month": 10, **extra}, role=role)
        self.assertEqual(response.status_code, 202, response.content)
        self.run_jobs()
        return AccountantPackage.objects.get(pk=response.json()["id"])

    def test_zip_structure_totals_and_summary(self):
        self.month_fixture()
        self.start_patches()
        package = self.generate()
        self.assertEqual(package.status, "ready", package.error)
        archive = zipfile.ZipFile(io.BytesIO(bytes(package.content)))
        names = archive.namelist()
        base = f"pacote-contador-{S.CNPJ}-2026-10/"
        key3 = S.key_of(self.xmls[3])
        for name in ("autorizadas/", "canceladas/", "inutilizadas/", "resumo.csv", "resumo.html", "LEIA-ME.txt",
                     f"canceladas/{key3}-nfce.xml", f"canceladas/{key3}-cancelamento.xml",
                     f"inutilizadas/{S.CNPJ}-serie1-6-7-inutilizacao.xml",
                     f"sped/SPED-EFD-ICMS-IPI-{S.CNPJ}-202610.txt"):
            self.assertIn(base + name, names)
        authorized = [n for n in names if n.startswith(base + "autorizadas/") and n.endswith(".xml")]
        self.assertEqual(len(authorized), 3)
        self.assertEqual(archive.read(f"{base}canceladas/{key3}-cancelamento.xml"), self.cancel_xml)
        # O total do resumo é a soma dos vNF dos XMLs que estão na pasta.
        total = sum(int(re.search(rb"<vNF>(\d+)\.(\d\d)</vNF>", archive.read(n)).expand(rb"\1\2")) for n in authorized)
        summary = package.summary
        self.assertEqual(summary["taxes"]["vNF"], total)
        self.assertTrue(summary["checks"]["analytic_matches_documents"])
        self.assertTrue(summary["checks"]["payments_match_documents"])
        status = {row["status"]: row for row in summary["by_status"]}
        self.assertEqual((status["authorized"]["count"], status["cancelled"]["count"],
                          status["inutilized"]["count"], status["contingency"]["count"]), (3, 1, 2, 1))
        warnings = summary["warnings"]
        self.assertEqual(len(warnings["contingency"]), 1)
        self.assertEqual(len(warnings["rejected"]), 1)
        self.assertEqual(warnings["gaps"], ["Série 1: nº 5 sem nota nem inutilização",
                                            "Série 1: nº 8 sem nota nem inutilização"])
        text = archive.read(base + "resumo.csv").decode("utf-8-sig")
        rows = list(csv.reader(io.StringIO(text), delimiter=";"))
        self.assertIn(["Autorizadas", "3", "161,70"], rows)
        self.assertIn(["17", "PIX dinâmico", "1", "53,90"], rows)
        self.assertIn(["5102", "000", "18,00", "83,70", "83,70", "15,06", "0,00", "0,00", "0,00", "3"], rows)
        self.assertIn(["ICMS", "15,06"], rows)
        self.assertIn(["PIS", plain_pis(self.xmls)], rows)
        html = archive.read(base + "resumo.html").decode()
        self.assertIn("Lacunas de numeração não inutilizadas", html)
        self.assertIn("PVA", html)
        readme = archive.read(base + "LEIA-ME.txt").decode()
        self.assertIn("VALIDAR NO PVA", readme)
        self.assertIn("nº 5 sem nota", readme)

    def test_sped_from_package_matches_rules(self):
        self.month_fixture()
        self.start_patches()
        package = self.generate()
        self.client.force_login(self.users["finance"])
        response = self.client.get(f"/api/v1/accountant/packages/{package.pk}/sped",
                                   HTTP_X_TENANT_ID=str(self.tenant.pk))
        self.assertEqual(response.status_code, 200)
        lines = response.content.decode("latin-1").split("\r\n")[:-1]
        self.assertEqual(efd.validate(lines, cnpj=S.CNPJ), [])
        regs = [line.split("|")[1] for line in lines]
        self.assertEqual(regs.count("C100"), 4)  # 3 autorizadas + 1 cancelada; inutilizada não entra em 2026
        self.assertEqual(regs.count("C170"), 0)
        cancelled = [line for line in lines if "|02|" in line and line.startswith("|C100|")]
        self.assertEqual(cancelled[0].split("|")[9], S.key_of(self.xmls[3]))
        self.assertEqual(lines[0].split("|")[2], "020")
        self.assertEqual(lines[-1], f"|9999|{len(lines)}|")
        e110 = [line for line in lines if line.startswith("|E110|")][0].split("|")
        self.assertEqual(e110[2], "15,06")
        self.assertIn("|E116|000|15,06|20112026|046-2|", "\n".join(lines))
        self.assertEqual(package.summary["sped"]["problems"], [])

    def test_simples_needs_confirmation_and_warns(self):
        Tenant.objects.filter(pk=self.tenant.pk).update(crt=1, tax_regime="simples_nacional")
        self.doc(S.nfce(1, issued="2026-10-03T10:00:00-03:00", crt=1,
                        items=[{"code": "X", "description": "Item", "ncm": "22021000", "cfop": "5102",
                                "qty": "1", "price": "10", "csosn": "102"}]))
        response = self.api("POST", "packages", {"year": 2026, "month": 10, "include_sped": True})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "sped_simples_confirm")
        self.assertIn("dispensada", response.json()["error"])
        package = self.generate()
        self.assertFalse(package.summary["sped"]["generated"])
        self.assertIsNone(package.sped_content)
        forced = self.generate(include_sped=True, force_sped=True)
        self.assertTrue(forced.summary["sped"]["generated"])
        lines = bytes(forced.sped_content).decode("latin-1").split("\r\n")
        self.assertIn("|C190|102|5102|0,00|10,00|0,00|0,00|0,00|0,00|0,00|0,00||", lines)

    def test_future_month_and_bad_input_refused(self):
        self.assertEqual(self.api("POST", "packages", {"year": 2099, "month": 1}).status_code, 400)
        self.assertEqual(self.api("POST", "packages", {"year": 2026, "month": 13}).status_code, 400)
        self.assertEqual(self.api("POST", "packages", {"year": "2026", "month": 1}).status_code, 400)


def plain_pis(xmls):
    total = 0
    for number in (1, 2, 4):
        last = re.findall(rb"<vPIS>(\d+)\.(\d\d)</vPIS>", xmls[number])[-1]  # ICMSTot vem depois dos itens
        total += int(last[0] + last[1])
    return f"{total // 100},{total % 100:02d}"


class DeliveryTests(AccountantBase):
    def test_generate_and_send_link_then_public_download(self):
        self.month_fixture()
        self.start_patches()
        response = self.api("POST", "packages", {"year": 2026, "month": 10, "send": True}, role="finance")
        self.assertEqual(response.status_code, 202, response.content)
        self.run_jobs()
        self.assertEqual(len(mail.outbox), 1)
        message = mail.outbox[0]
        self.assertEqual(message.to, ["contador@escritorio.test"])
        self.assertEqual(message.attachments, [])
        self.assertIn("10/2026", message.subject)
        self.assertIn("PVA", message.body)
        link = re.search(r"https?://\S+/api/v1/accountant/download\?t=(\S+)", message.body)
        token = link.group(1)
        delivery = AccountantDelivery.objects.get()
        self.assertEqual(delivery.status, "sent")
        self.assertNotIn(token, delivery.token_hash)
        self.assertAlmostEqual((delivery.link_expires_at - timezone.now()).days, 29, delta=1)
        self.client.logout()
        download = self.client.get("/api/v1/accountant/download", {"t": token})
        self.assertEqual(download.status_code, 200)
        self.assertEqual(download["Content-Type"], "application/zip")
        self.assertTrue(zipfile.ZipFile(io.BytesIO(download.content)).namelist())
        self.assertEqual(self.client.get("/api/v1/accountant/download/" + token + "x").status_code, 404)
        AccountantDelivery.objects.update(link_expires_at=timezone.now() - timedelta(seconds=1))
        self.assertEqual(self.client.get("/api/v1/accountant/download/" + token).status_code, 404)
        # Painel mostra o registro do envio.
        listing = self.api("GET", "packages").json()["results"][0]
        self.assertEqual(listing["deliveries"][0]["status"], "sent")
        self.assertEqual(listing["deliveries"][0]["downloads"], 1)
        # Old link form (/download/<token>) is still accepted for a valid token.
        AccountantDelivery.objects.update(link_expires_at=timezone.now() + timedelta(days=1))
        self.assertEqual(self.client.get("/api/v1/accountant/download/" + token).status_code, 200)

    def test_token_is_recorded_before_the_email_leaves(self):
        self.month_fixture()
        self.start_patches()
        seen = {}

        def capture(message, fail_silently=False):
            token = re.search(r"download\?t=(\S+)", message.body).group(1)
            # At the moment the e-mail is handed to SMTP the link already works.
            seen["delivery"] = bool(package_module.find_by_token(token))
            return 1

        with patch("django.core.mail.EmailMessage.send", capture):
            self.api("POST", "packages", {"year": 2026, "month": 10, "send": True}, role="finance")
            self.run_jobs()
        self.assertTrue(seen.get("delivery"))

    def test_public_download_is_rate_limited(self):
        self.client.logout()
        codes = [self.client.get("/api/v1/accountant/download", {"t": "x" * 40}).status_code for _ in range(21)]
        self.assertEqual(codes[:20], [404] * 20)
        self.assertEqual(codes[20], 429)

    def test_smtp_has_a_timeout(self):
        from django.conf import settings
        self.assertEqual(settings.EMAIL_TIMEOUT, 15)

    def test_email_failure_is_recorded_and_resend_works(self):
        self.month_fixture()
        self.start_patches()
        with patch("django.core.mail.EmailMessage.send", side_effect=ConnectionRefusedError("smtp down")):
            self.api("POST", "packages", {"year": 2026, "month": 10, "send": True})
            self.run_jobs()
        package = AccountantPackage.objects.get()
        self.assertEqual(package.status, "ready")
        delivery = AccountantDelivery.objects.get()
        self.assertEqual(delivery.status, "failed")
        self.assertIn("ConnectionRefusedError", delivery.error)
        self.assertEqual(ProviderJob.objects.get(kind="accountant.package").status, "needs_review")
        listing = self.api("GET", "packages").json()["results"][0]
        self.assertEqual(listing["deliveries"][0]["status"], "failed")
        response = self.api("POST", f"packages/{package.pk}/send", {})
        self.assertEqual(response.status_code, 202, response.content)
        self.run_jobs()
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(AccountantDelivery.objects.filter(status="sent").count(), 1)

    def test_send_requires_accountant_email(self):
        Tenant.objects.filter(pk=self.tenant.pk).update(accountant_email="")
        response = self.api("POST", "packages", {"year": 2026, "month": 10, "send": True})
        self.assertEqual(response.status_code, 409)

    def test_monthly_schedule_runs_on_day_one_once(self):
        self.month_fixture()
        self.start_patches()
        self.doc(S.nfce(1, issued="2026-10-03T10:00:00-03:00"), tenant=self.other)
        day_one = datetime(2026, 11, 1, 6, 0, tzinfo=dt_timezone.utc)
        self.assertEqual(jobs.schedule_monthly(day_one), 2)
        self.assertEqual(jobs.schedule_monthly(day_one + timedelta(hours=1)), 0)
        self.assertEqual(jobs.schedule_monthly(datetime(2026, 11, 10, 12, tzinfo=dt_timezone.utc)), 0)
        self.run_jobs()
        self.assertEqual(sorted(m.to[0] for m in mail.outbox), ["contador@escritorio.test", "outro@escritorio.test"])
        mine = AccountantPackage.objects.get(tenant=self.tenant)
        self.assertEqual((mine.year, mine.month, mine.trigger, mine.include_sped), (2026, 10, "schedule", True))
        self.assertEqual(ProviderJob.objects.filter(kind="accountant.monthly", status="done").count(), 2)
        # Janeiro gera dezembro do ano anterior; quem desligou o envio automático fica de fora.
        AccountantSettings.objects.filter(tenant=self.tenant).update(auto_send=False)
        self.assertEqual(jobs.schedule_monthly(datetime(2027, 1, 1, 12, tzinfo=dt_timezone.utc)), 1)
        self.assertTrue(ProviderJob.objects.filter(idempotency_key__endswith=f"{self.other.pk}:2026-12").exists())

    def test_expired_zip_is_purged(self):
        self.doc(S.nfce(1, issued="2026-10-03T10:00:00-03:00"))
        self.api("POST", "packages", {"year": 2026, "month": 10})
        self.run_jobs()
        package = AccountantPackage.objects.get()
        jobs.housekeeping(package.expires_at + timedelta(minutes=1))
        package.refresh_from_db()
        self.assertEqual(package.status, "expired")
        self.assertIsNone(package.content)
        response = self.api("GET", f"packages/{package.pk}/download")
        self.assertEqual(response.status_code, 409)


class PermissionTests(AccountantBase):
    def test_roles(self):
        for role in ("owner", "admin", "finance"):
            self.assertEqual(self.api("GET", "packages", role=role).status_code, 200, role)
        for role in ("manager", "cashier"):
            self.assertEqual(self.api("GET", "packages", role=role).status_code, 403, role)
            self.assertEqual(self.api("POST", "packages", {"year": 2026, "month": 10}, role=role).status_code, 403)
        self.assertEqual(self.api("PATCH", "settings", {"crc": "SP1"}, role="finance").status_code, 403)
        self.assertEqual(self.api("PATCH", "settings", {"crc": "SP1"}, role="admin").status_code, 200)

    def test_other_company_cannot_see_or_download(self):
        self.doc(S.nfce(1, issued="2026-10-03T10:00:00-03:00"))
        self.api("POST", "packages", {"year": 2026, "month": 10})
        self.run_jobs()
        package = AccountantPackage.objects.get()
        # Usuário de outra empresa com o próprio X-Tenant-ID: não acha o pacote.
        for path in (f"packages/{package.pk}", f"packages/{package.pk}/download", f"packages/{package.pk}/sped"):
            response = self.api("GET", path, user=self.other_user, tenant=self.other.pk)
            self.assertEqual(response.status_code, 404, path)
        self.assertEqual(self.api("POST", f"packages/{package.pk}/send", {}, user=self.other_user,
                                  tenant=self.other.pk).status_code, 404)
        # E com o X-Tenant-ID da empresa alheia: acesso negado.
        self.assertEqual(self.api("GET", "packages", user=self.other_user).status_code, 403)
        self.assertEqual(self.api("GET", "packages", user=self.other_user, tenant=self.other.pk).json()["results"], [])
        self.client.logout()
        self.assertIn(self.client.get(f"/api/v1/accountant/packages/{package.pk}/download").status_code, (401, 403))

    def test_settings_validation(self):
        ok = self.api("PATCH", "settings", {"accountant_email": "Novo@Contab.test", "cpf": "529.982.247-25",
                                            "sped_profile": "B", "e116_due_day": 9, "phone": "(11) 3333-4444"})
        self.assertEqual(ok.status_code, 200, ok.content)
        self.assertEqual(ok.json()["accountant_email"], "novo@contab.test")
        self.assertEqual(ok.json()["sped_profile"], "B")
        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.accountant_email, "novo@contab.test")
        for body in ({"accountant_email": "não é email"}, {"cpf": "11111111111"}, {"sped_profile": "D"},
                     {"e116_due_day": 40}, {"city_ibge": "1234567"}, {"unknown": 1}, {"auto_send": "sim"}):
            self.assertEqual(self.api("PATCH", "settings", body).status_code, 400, body)
