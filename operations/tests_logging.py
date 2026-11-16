"""Internal errors must leave a trace: stdout logging, request ids and worker tracebacks."""
import io
import logging
import os
import re
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock, patch

from django.conf import settings
from django.http import JsonResponse
from django.test import TestCase, override_settings
from django.urls import path

from core.models import Tenant
from integrations.base import ProviderError
from . import services, worker
from .middleware import RequestIdFilter
from .services import enqueue, run_one


def boom(request):
    raise RuntimeError("boom-marker")


def fine(request):
    logging.getLogger("pdv2.test").info("inside-view")
    return JsonResponse({"ok": True})


urlpatterns = [path("boom/", boom), path("fine/", fine)]


def console_handler():
    for handler in logging.getLogger().handlers:
        if any(isinstance(item, RequestIdFilter) for item in handler.filters):
            return handler
    raise AssertionError("LOGGING não instalou o handler de console na raiz")


class captured_console:
    """Swap the stream of the configured console handler to read real output."""
    def __enter__(self):
        self.handler = console_handler()
        self.buffer = io.StringIO()
        self.previous = self.handler.setStream(self.buffer)
        return self.buffer

    def __exit__(self, *exc):
        self.handler.setStream(self.previous)


@override_settings(ROOT_URLCONF=__name__)
class RequestLoggingTests(TestCase):
    def test_unhandled_error_is_logged_with_traceback_and_request_id(self):
        self.client.raise_request_exception = False
        with captured_console() as output:
            response = self.client.get("/boom/")
        self.assertEqual(response.status_code, 500)
        request_id = response["X-Request-ID"]
        self.assertRegex(request_id, r"^[0-9a-f]{16}$")
        text = output.getvalue()
        self.assertRegex(text, rf"ERROR django\.request \[{request_id}\] Internal Server Error: /boom/")
        self.assertIn("Traceback (most recent call last)", text)
        self.assertIn("RuntimeError: boom-marker", text)

    def test_each_request_gets_its_own_id_and_none_outside_requests(self):
        with captured_console() as output:
            first = self.client.get("/fine/")
            second = self.client.get("/fine/")
            logging.getLogger("pdv2.test").info("outside-request")
        self.assertNotEqual(first["X-Request-ID"], second["X-Request-ID"])
        text = output.getvalue()
        self.assertIn(f"INFO pdv2.test [{first['X-Request-ID']}] inside-view", text)
        self.assertIn(f"INFO pdv2.test [{second['X-Request-ID']}] inside-view", text)
        self.assertIn("INFO pdv2.test [-] outside-request", text)

    def test_client_errors_are_left_to_the_access_log(self):
        with captured_console() as output:
            self.assertEqual(self.client.get("/missing/").status_code, 404)
        self.assertEqual(output.getvalue(), "")


class LoggingConfigurationTests(TestCase):
    def test_console_handler_writes_to_stdout_for_root_and_django(self):
        handler = settings.LOGGING["handlers"]["console"]
        self.assertEqual(handler["stream"], "ext://sys.stdout")
        self.assertEqual(settings.LOGGING["root"]["handlers"], ["console"])
        self.assertEqual(settings.LOGGING["loggers"]["django"]["handlers"], ["console"])
        self.assertFalse(settings.LOGGING["disable_existing_loggers"])
        for name in ("levelname", "name", "message", "request_id"):
            self.assertIn(f"%({name})s", settings.LOGGING["formatters"]["standard"]["format"])

    def test_production_process_logs_exceptions_to_stdout(self):
        root = Path(settings.ROOT_DIR)
        env = {key: value for key, value in os.environ.items()
               if not key.startswith(("PDV_", "DJANGO_", "DATABASE_URL"))}
        env.update({
            "PDV_ENV": "production", "PDV_DEBUG": "0", "PDV_SECRET_KEY": "k" * 64,
            "PDV_ALLOWED_HOSTS": "pdv.example.test", "PDV_LICENSE_PRIVATE_KEY": "test-only",
            "DATABASE_URL": "postgresql://pdv2:unused@127.0.0.1:1/pdv2",
            "DJANGO_SETTINGS_MODULE": "pdvcloud.settings",
            "PYTHONPATH": os.pathsep.join([str(root / "cloud"), str(root)]),
        })
        script = ("import django, logging; django.setup()\n"
                  "log = logging.getLogger('operations.worker')\n"
                  "log.info('started-marker')\n"
                  "try:\n    {}['payment']\n"
                  "except KeyError:\n    log.exception('job-failed-marker')\n")
        result = subprocess.run([sys.executable, "-c", script], env=env, capture_output=True,
                                text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertRegex(result.stdout, r"INFO operations\.worker \[-\] started-marker")
        self.assertRegex(result.stdout, r"ERROR operations\.worker \[-\] job-failed-marker")
        self.assertIn("KeyError: 'payment'", result.stdout)
        self.assertNotIn("marker", result.stderr)


class WorkerLoggingTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Mercado A")

    def run_failing(self, error):
        job = enqueue("billing.reconcile", self.tenant, "job-" + type(error).__name__, {"payment_id": "pay_1"})
        with patch.dict("operations.services.HANDLERS", {"billing.reconcile": Mock(side_effect=error)}):
            worker.install_job_logging()
            worker.install_job_logging()
            with self.assertLogs("operations.worker", level="INFO") as logs:
                self.assertTrue(run_one())
        job.refresh_from_db()
        return job, logs

    def test_unexpected_job_error_is_logged_with_traceback(self):
        job, logs = self.run_failing(KeyError("billingType"))
        self.assertEqual(job.status, "needs_review")
        self.assertNotIn("billingType", job.last_error)
        self.assertEqual(len(logs.records), 1)
        record = logs.records[0]
        self.assertEqual(record.levelname, "ERROR")
        self.assertIs(record.exc_info[0], KeyError)
        self.assertIn(str(job.pk), record.getMessage())
        self.assertIn("billing.reconcile", record.getMessage())

    def test_provider_refusal_is_logged_without_changing_the_retry(self):
        job, logs = self.run_failing(ProviderError("Provedor recusou a operação (HTTP 503).", status=503))
        self.assertEqual(job.status, "queued")
        self.assertEqual([record.levelname for record in logs.records], ["WARNING"])
        self.assertIn("HTTP 503", logs.records[0].getMessage())

    def test_handlers_are_restored_and_wrapped_only_once(self):
        original = dict(services.HANDLERS)
        with patch.dict("operations.services.HANDLERS"):
            worker.install_job_logging()
            wrapped = dict(services.HANDLERS)
            worker.install_job_logging()
            self.assertEqual(services.HANDLERS, wrapped)
            self.assertEqual(set(wrapped), set(original))
            for kind, handler in wrapped.items():
                self.assertIs(handler.__wrapped__, original[kind])
        self.assertEqual(services.HANDLERS, original)


class WorkerLoopTests(TestCase):
    def test_iteration_failure_is_logged_and_the_loop_survives(self):
        calls = []

        def failing_run_one():
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("database-gone")
            worker.stop()
            return True

        self.addCleanup(setattr, worker, "running", True)
        with patch.dict("operations.services.HANDLERS"), \
                patch.object(services, "run_one", failing_run_one), \
                patch.object(worker.signal, "signal"), patch.object(worker.time, "sleep"), \
                patch.object(worker.Path, "write_text"), \
                self.assertLogs("operations.worker", level="ERROR") as logs:
            worker.main()
        self.assertEqual(len(calls), 2)
        self.assertIs(logs.records[0].exc_info[0], RuntimeError)
        self.assertTrue(re.search("iteration failed", logs.records[0].getMessage()))
