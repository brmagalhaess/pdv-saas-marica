"""Correções da revisão antes do deploy da NFC-e: transporte (I4), contingência no caixa (I2/I3/I5),
notas legadas da Focus (I6), habilitação (I7), varredura das faixas e migração de dados."""
import importlib
import http.client
from datetime import timedelta
from unittest.mock import patch

import requests
from django.apps import apps as django_apps
from django.test import SimpleTestCase
from django.utils import timezone
from urllib3 import exceptions as u3

from operations.models import FiscalPayload, IntegrationAccount, ProviderJob
from operations.services import enqueue
from shared.secrets import encrypt_secret

from .api_common import APIError
from .models import AuditEvent, FiscalConfig, FiscalDocument, FiscalEvent, FiscalInutilization, NfceReservation, Tenant
from .nfce import service
from .nfce.transport import SoapTransport, TransportError, never_sent
from . import tests_nfce
from .tests_nfce import CSC, NfceTestBase


class _Session:
    def __init__(self, exc):
        self.exc = exc

    def post(self, *args, **kwargs):
        raise self.exc


def _wrapped(reason):
    """How requests reports a urllib3 failure: ConnectionError(MaxRetryError(reason=...))."""
    return requests.ConnectionError(u3.MaxRetryError(None, "https://sefaz.test/ws", reason))


class TransportClassificationTests(SimpleTestCase):
    def outcome(self, exc):
        transport = SoapTransport(None, session=_Session(exc))
        with self.assertRaises(TransportError) as caught:
            transport.call("https://sefaz.test/ws", "status", _body())
        return caught.exception.unreachable, caught.exception.outcome_unknown

    def test_not_sent_is_unreachable(self):
        for exc in (requests.ConnectTimeout("connect timeout"),
                    _wrapped(u3.NewConnectionError(None, "Connection refused")),
                    _wrapped(u3.NameResolutionError("sefaz.test", None, OSError("dns"))),
                    _wrapped(u3.ConnectTimeoutError(None, "connect timeout")),
                    requests.exceptions.SSLError(u3.MaxRetryError(None, "/", u3.SSLError("handshake failure")))):
            with self.subTest(type(exc).__name__):
                self.assertTrue(never_sent(exc))
                self.assertEqual(self.outcome(exc), (True, False))

    def test_maybe_sent_is_outcome_unknown(self):
        for exc in (requests.ReadTimeout("read timeout"),
                    _wrapped(u3.ReadTimeoutError(None, "/", "read timeout")),
                    requests.ConnectionError(u3.ProtocolError("Connection aborted.",
                                                              http.client.RemoteDisconnected("closed"))),
                    requests.ConnectionError(u3.ProtocolError("Connection aborted.", ConnectionResetError(104, "reset"))),
                    requests.exceptions.ChunkedEncodingError("broken chunk"),
                    requests.exceptions.ContentDecodingError("bad gzip"),
                    requests.ConnectionError("unknown")):
            with self.subTest(type(exc).__name__):
                self.assertFalse(never_sent(exc))
                self.assertEqual(self.outcome(exc), (False, True))


def _body():
    from shared.nfce import messages
    return messages.status_request("35", "2")


class OutcomeUnknownEmissionTests(NfceTestBase):
    def test_connection_dropped_after_post_never_reuses_the_number(self):
        self.configure_sefaz()
        dropped = TransportError("caiu", outcome_unknown=True)
        self.sefaz.answers["authorize"] = [dropped, lambda body: _authorize(body)]
        sale = self.sale()
        fiscal = self.terminal_api("POST", "nfce", {"sale_id": str(sale.id)}).json()["fiscal"]
        # The first number may have reached the SEFAZ: the contingency note gets a NEW number.
        self.assertEqual((fiscal["status"], fiscal["number"]), ("contingency", 2))
        self.assertIsNotNone(FiscalDocument.objects.get(sale=sale, number=1).replaced_by_id)


def _authorize(body):
    from shared.nfce.testing import ret_authorization
    return ret_authorization(body.find("NFe/infNFe").attrs["Id"][3:])


class ReadinessTests(NfceTestBase):
    def test_get_config_does_not_create_it_nor_turn_nfce_on(self):
        response = self.api("GET", "/api/v1/ops/fiscal/config")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["ready"])
        self.assertFalse(FiscalConfig.objects.exists())
        self.assertFalse(service.terminal_settings(self.tenant)["enabled"])
        # A saved configuration without credential still does not turn it on.
        self.api("PATCH", "/api/v1/ops/fiscal/config", {"qr_version": 3})
        self.assertTrue(FiscalConfig.objects.exists())
        self.assertFalse(service.terminal_settings(self.tenant)["enabled"])
        self.configure_sefaz()
        self.assertTrue(service.terminal_settings(self.tenant)["enabled"])

    def test_focus_mode_needs_the_token(self):
        self.api("PATCH", "/api/v1/ops/fiscal/config", {"mode": "focusnfe"})
        self.assertFalse(service.terminal_settings(self.tenant)["enabled"])
        sale = self.sale()
        self.assertEqual(self.terminal_api("POST", "nfce", {"sale_id": str(sale.id)}).json()["status"], "disabled")
        self.api("PATCH", "/api/v1/ops/fiscal/config", {"focus_token": "token-teste"})
        self.assertTrue(service.terminal_settings(self.tenant)["enabled"])

    def test_production_without_platform_release_spends_no_number(self):
        self.configure_sefaz()
        Tenant.objects.filter(pk=self.tenant.pk).update(fiscal_environment="production")
        self.tenant.refresh_from_db()
        self.assertFalse(service.terminal_settings(self.tenant)["enabled"])
        sale = self.sale()
        with self.assertRaises(APIError) as caught:
            service.create_document(sale)
        self.assertEqual(caught.exception.code, "fiscal_live_disabled")
        response = self.api("POST", "/api/v1/ops/fiscal/submit", {"sale_id": str(sale.id)})
        self.assertEqual((response.status_code, response.json()["code"]), (409, "fiscal_live_disabled"))
        self.assertEqual(Tenant.objects.get(pk=self.tenant.pk).nfce_next_number, 1)
        self.assertFalse(FiscalDocument.objects.exists())

    def test_incomplete_company_spends_no_number(self):
        self.configure_sefaz()
        Tenant.objects.filter(pk=self.tenant.pk).update(city_ibge_code="")
        self.tenant.refresh_from_db()
        with self.assertRaises(APIError):
            service.create_document(self.sale())
        self.assertEqual(Tenant.objects.get(pk=self.tenant.pk).nfce_next_number, 1)


class ReservationTests(NfceTestBase):
    offline_xml = tests_nfce.TerminalContingencyTests.offline_xml

    def test_repeated_asks_return_the_same_block_and_audit_once(self):
        self.configure_sefaz()
        first = self.terminal_api("POST", "nfce/reserve", {}).json()
        second = self.terminal_api("POST", "nfce/reserve", {}).json()
        self.assertEqual((first["created"], second["created"]), (True, False))
        self.assertEqual(second["numbers"], list(range(1, 11)))
        self.assertEqual(NfceReservation.objects.count(), 1)
        self.assertEqual(AuditEvent.objects.filter(action="fiscal.numbers_reserved").count(), 1)

    def test_numbers_used_offline_open_an_additional_block(self):
        self.configure_sefaz()
        self.terminal_api("POST", "nfce/reserve", {})
        answer = self.terminal_api("POST", "nfce/reserve", {"used": list(range(1, 9))}).json()
        self.assertTrue(answer["created"])
        # The old block keeps its two numbers; the new one adds ten.
        self.assertEqual(answer["numbers"], [9, 10] + list(range(11, 21)))
        self.assertEqual(answer["ranges"], [[1, 10], [11, 20]])
        self.assertEqual(NfceReservation.objects.filter(released_at__isnull=True).count(), 2)
        again = self.terminal_api("POST", "nfce/reserve", {"used": list(range(1, 9))}).json()
        self.assertFalse(again["created"])

    def test_block_close_to_expiry_is_renewed_but_still_accepts_uploads(self):
        self.configure_sefaz()
        old = self.terminal_api("POST", "nfce/reserve", {}).json()
        NfceReservation.objects.update(created_at=timezone.now() - timedelta(days=service.RESERVATION_MAX_DAYS - 2))
        fresh = self.terminal_api("POST", "nfce/reserve", {}).json()
        self.assertTrue(fresh["created"])
        self.assertEqual(fresh["numbers"], list(range(11, 21)))
        # A note issued offline with the old block before the renewal still registers.
        sale = self.sale()
        _draft, xml = self.offline_xml(sale, old)
        response = self.terminal_api("POST", "nfce/contingency", {"sale_id": str(sale.id), "xml": xml})
        self.assertEqual(response.status_code, 200, response.content)

    def test_released_block_is_refused_with_a_code_the_register_handles(self):
        self.configure_sefaz()
        reservation = self.terminal_api("POST", "nfce/reserve", {}).json()
        sale = self.sale()
        _draft, xml = self.offline_xml(sale, reservation)
        NfceReservation.objects.update(released_at=timezone.now())
        response = self.terminal_api("POST", "nfce/contingency", {"sale_id": str(sale.id), "xml": xml})
        self.assertEqual((response.status_code, response.json()["code"]), (409, "reservation_released"))
        self.assertFalse(FiscalDocument.objects.filter(sale=sale).exists())

    def test_signing_failure_leaves_no_orphan_note(self):
        self.configure_sefaz()
        reservation = self.terminal_api("POST", "nfce/reserve", {}).json()
        sale = self.sale()
        _draft, xml = self.offline_xml(sale, reservation)
        with patch("core.nfce.backends.SefazBackend.sign_received", side_effect=service.NotConfigured("sem certificado")):
            response = self.terminal_api("POST", "nfce/contingency", {"sale_id": str(sale.id), "xml": xml})
        self.assertEqual(response.status_code, 409)
        self.assertFalse(FiscalDocument.objects.filter(sale=sale).exists())
        self.assertEqual(NfceReservation.objects.get().used, [])
        # The register uploads again later and it goes through.
        response = self.terminal_api("POST", "nfce/contingency", {"sale_id": str(sale.id), "xml": xml})
        self.assertEqual(response.status_code, 200, response.content)

    def test_repeated_upload_repairs_a_half_registered_note(self):
        self.configure_sefaz()
        reservation = self.terminal_api("POST", "nfce/reserve", {}).json()
        sale = self.sale()
        _draft, xml = self.offline_xml(sale, reservation)
        self.assertEqual(self.terminal_api("POST", "nfce/contingency", {"sale_id": str(sale.id), "xml": xml}).status_code, 200)
        document = FiscalDocument.objects.get(sale=sale)
        # Simulate the old failure mode: committed without signature and without its job.
        FiscalPayload.objects.filter(document=document).update(nfe_xml=None)
        ProviderJob.objects.filter(kind="fiscal.transmit").delete()
        self.assertEqual(self.terminal_api("POST", "nfce/contingency", {"sale_id": str(sale.id), "xml": xml}).status_code, 200)
        self.assertIsNotNone(FiscalPayload.objects.get(document=document).nfe_xml)
        self.assertTrue(ProviderJob.objects.filter(kind="fiscal.transmit", payload__document_id=str(document.pk)).exists())
        self.run_jobs()
        document.refresh_from_db()
        self.assertEqual(document.status, "authorized")

    def test_note_without_qr_code_is_refused(self):
        self.configure_sefaz()
        reservation = self.terminal_api("POST", "nfce/reserve", {}).json()
        sale = self.sale()
        _draft, xml = self.offline_xml(sale, reservation)
        start, end = xml.index("<infNFeSupl>"), xml.index("</infNFeSupl>") + len("</infNFeSupl>")
        response = self.terminal_api("POST", "nfce/contingency", {"sale_id": str(sale.id), "xml": xml[:start] + xml[end:]})
        self.assertEqual(response.status_code, 422)
        self.assertFalse(FiscalDocument.objects.filter(sale=sale).exists())


class SweepTests(NfceTestBase):
    def test_one_failing_block_does_not_stop_the_others_nor_lose_its_release(self):
        self.configure_sefaz()
        self.terminal_api("POST", "nfce/reserve", {})
        self.terminal_api("POST", "nfce/reserve", {"used": list(range(1, 11))})
        NfceReservation.objects.update(created_at=timezone.now() - timedelta(days=30))
        first = NfceReservation.objects.order_by("start").first()
        real = service.request_inutilization

        def flaky(tenant, environment, series, start, end, reason):
            if start == first.start:
                raise RuntimeError("falha no meio")
            return real(tenant, environment, series, start, end, reason)

        with patch.object(service, "request_inutilization", flaky):
            self.assertEqual(service.sweep(), 1)
        first.refresh_from_db()
        self.assertIsNone(first.released_at)  # stays open: tried again on the next sweep
        self.assertEqual(FiscalInutilization.objects.count(), 1)
        self.assertEqual(service.sweep(), 1)
        self.assertEqual(FiscalInutilization.objects.count(), 2)
        self.assertFalse(NfceReservation.objects.filter(released_at__isnull=True).exists())

    def test_company_no_longer_fiscal_releases_without_inutilizing(self):
        self.configure_sefaz()
        self.terminal_api("POST", "nfce/reserve", {})
        NfceReservation.objects.update(created_at=timezone.now() - timedelta(days=30))
        Tenant.objects.filter(pk=self.tenant.pk).update(fiscal_environment="non_fiscal")
        self.assertEqual(service.sweep(), 1)
        self.assertFalse(FiscalInutilization.objects.exists())
        self.assertIsNotNone(NfceReservation.objects.get().released_at)


class LegacyFocusTests(NfceTestBase):
    """Notes created by the release before NFC-e (core 0041, Focus NFe only)."""

    def setUp(self):
        super().setUp()
        self.account = IntegrationAccount.objects.create(tenant=self.tenant, provider="focusnfe", environment="homologation",
                                                         token_encrypted=encrypt_secret("token-antigo"), enabled=True)
        self.environments = []
        self.focus_factory = patch.object(service, "FOCUS_FACTORY",
                                          lambda token, environment: self.environments.append(environment) or self.focus)
        self.focus_factory.start()
        self.addCleanup(self.focus_factory.stop)

    def legacy(self, status="authorized", response=None, payload=None, xml_url="/arquivos/x-nfe.xml"):
        sale = self.sale()
        reference = "pdv2-" + str(sale.id)
        doc = FiscalDocument.objects.create(tenant=self.tenant, shop=self.shop, sale=sale, provider="focusnfe",
                                            external_id=reference, status=status, xml_url=xml_url,
                                            authorization="135260000000555" if status == "authorized" else "")
        FiscalPayload.objects.create(document=doc, request_payload=payload or {"natureza_operacao": "VENDA"},
                                     provider_response=response or {})
        return doc

    def migrate_data(self):
        module = importlib.import_module("core.migrations.0045_nfce_status")
        module.forwards(django_apps, None)

    def test_data_migration_backfills_key_number_environment_and_config(self):
        key = "35261111222333000181650010000004211123456780"
        authorized = self.legacy(response={"status": "autorizado", "chave_nfe": "NFe" + key, "numero": "421", "serie": "1",
                                           "data_emissao": "2026-11-10T10:00:00-03:00",
                                           "data_autorizacao": "2026-11-10T10:00:05-03:00"})
        # Without "numero"/"serie" in the answer, they come from the access key; no "NFe" prefix also works.
        bare = self.legacy(response={"chave_nfe": key[:-1] + "9"})
        FiscalDocument.objects.filter(pk=bare.pk).update(status="denied", error_message="Rejeição 301")
        processing = self.legacy(status="processing")
        Tenant.objects.filter(pk=self.tenant.pk).update(fiscal_environment="non_fiscal")
        self.migrate_data()
        authorized.refresh_from_db()
        self.assertEqual((authorized.environment, authorized.number, authorized.series, authorized.access_key),
                         ("homologation", 421, 1, key))
        self.assertIsNotNone(authorized.issued_at)
        self.assertIsNotNone(authorized.authorized_at)
        bare.refresh_from_db()
        self.assertEqual((bare.status, bare.access_key), ("rejected", key[:-1] + "9"))
        self.assertIsNone(bare.number)  # 421/1 already belongs to the first note: never duplicated
        self.assertTrue(bare.error_message.startswith("Uso denegado"))
        processing.refresh_from_db()
        self.assertEqual((processing.status, processing.environment, processing.number), ("pending", "homologation", None))
        self.assertEqual(FiscalConfig.objects.get(tenant=self.tenant).mode, "focusnfe")
        # Running it again changes nothing.
        self.migrate_data()
        self.assertEqual(FiscalConfig.objects.count(), 1)

    def test_old_cancel_job_without_event_id_still_cancels(self):
        doc = self.legacy()
        self.focus.state[doc.external_id] = {"status": "autorizado", "chave_nfe": "NFe" + "3" * 44}
        enqueue("fiscal.cancel", self.tenant, "fiscal.cancel:" + str(doc.pk),
                {"document_id": str(doc.pk), "reason": "Cliente desistiu da compra no caixa"})
        Tenant.objects.filter(pk=self.tenant.pk).update(fiscal_environment="non_fiscal")
        self.run_jobs()
        doc.refresh_from_db()
        self.assertEqual(doc.status, "cancelled")
        self.assertEqual(FiscalEvent.objects.get(document=doc).status, "registered")
        self.assertEqual(set(self.environments), {"homologation"})

    def test_old_cancel_job_without_reason_fails_with_a_clear_message(self):
        doc = self.legacy()
        job = enqueue("fiscal.cancel", self.tenant, "fiscal.cancel:" + str(doc.pk), {"document_id": str(doc.pk)})
        self.run_jobs()
        job.refresh_from_db()
        self.assertEqual(job.status, "needs_review")
        self.assertIn("justificativa", job.last_error)

    def test_archive_uses_the_key_of_the_focus_answer(self):
        key = "3" * 44
        doc = self.legacy(response={"chave_nfe": "NFe" + key})
        job = enqueue("fiscal.archive", self.tenant, "fiscal.archive:" + str(doc.pk), {"document_id": str(doc.pk)})
        self.run_jobs()
        job.refresh_from_db()
        self.assertEqual(job.status, "done", job.last_error)
        self.assertIn(key.encode(), bytes(FiscalPayload.objects.get(document=doc).xml_content))

    def test_pending_legacy_note_is_polled_in_its_own_environment(self):
        doc = self.legacy(status="pending", payload={"natureza_operacao": "VENDA", "items": []})
        Tenant.objects.filter(pk=self.tenant.pk).update(fiscal_environment="non_fiscal")
        self.focus.state[doc.external_id] = {"status": "autorizado", "status_sefaz": "100",
                                             "chave_nfe": "NFe" + "3" * 44, "numero_protocolo": "135260000000555",
                                             "numero": "77", "serie": "1"}
        enqueue("fiscal.poll", self.tenant, "fiscal.poll:" + str(doc.pk), {"document_id": str(doc.pk)})
        self.run_jobs()
        doc.refresh_from_db()
        self.assertEqual((doc.status, doc.access_key), ("authorized", "3" * 44))
        self.assertEqual(self.focus.submitted, [])
        self.assertIn("homologation", self.environments)

    def test_unknown_legacy_note_is_submitted_with_its_stored_payload(self):
        payload = {"natureza_operacao": "VENDA", "numero": 5, "serie": 1}
        doc = self.legacy(status="pending", payload=payload)
        enqueue("fiscal.submit", self.tenant, "fiscal.submit:" + str(doc.pk), {"document_id": str(doc.pk)})
        self.run_jobs()
        self.assertEqual(self.focus.submitted, [(doc.external_id, payload)])
        doc.refresh_from_db()
        self.assertEqual(doc.status, "authorized")

    def test_legacy_rejected_note_cannot_be_rebuilt(self):
        doc = self.legacy(status="rejected")
        response = self.api("POST", f"/api/v1/ops/fiscal/documents/{doc.pk}/retry")
        self.assertEqual((response.status_code, response.json()["code"]), (409, "fiscal_legacy_document"))


class CscOutOfBackupsTests(SimpleTestCase):
    def test_strip_secrets_keeps_numbers(self):
        import json
        import sqlite3
        from terminal import nfce
        conn = sqlite3.connect(":memory:")
        nfce.migrate(conn)
        conn.execute("INSERT INTO nfce_reservation(id,data) VALUES (1,?)",
                     (json.dumps({"csc": CSC, "csc_id": "000001", "numbers": [3, 4], "used": [1, 2]}),))
        nfce.strip_secrets(conn)
        data = json.loads(conn.execute("SELECT data FROM nfce_reservation").fetchone()[0])
        self.assertNotIn("csc", data)
        self.assertEqual((data["numbers"], data["used"]), ([3, 4], [1, 2]))
