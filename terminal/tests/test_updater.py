"""Automatic update: download, verification, staging, pointer swap and rollback."""

import hashlib
import importlib.util
import io
import json
import os
import stat
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from terminal import paths, updater
from terminal.store import Store

ROOT = Path(__file__).resolve().parents[2]


def make_bundle(version, files=None, fmt="source", marker=True, extra=()):
    """Zip bytes of a well-formed bundle; `extra` adds raw (ZipInfo|name, data) entries."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        if marker:
            archive.writestr(f"app-{version}/bundle.json", json.dumps({"version": version, "format": fmt}))
        for name, data in (files or {"terminal/hello.txt": "olá " * 2000}).items():
            archive.writestr(f"app-{version}/{name}", data)
        for entry, data in extra:
            archive.writestr(entry, data)
    return buffer.getvalue()


def symlink_entry(name):
    info = zipfile.ZipInfo(name)
    info.external_attr = (stat.S_IFLNK | 0o777) << 16
    return info


class PackageServer:
    """Local HTTP server with Range support and an optional one-shot connection drop."""

    def __init__(self, body):
        self.body = body
        self.drop_after = None
        self.ignore_range = False
        self.ranges = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                requested = self.headers.get("Range")
                owner.ranges.append(requested)
                start = 0
                if requested and not owner.ignore_range:
                    start = int(requested.split("=")[1].rstrip("-"))
                    if start >= len(owner.body):
                        self.send_response(416)
                        self.send_header("Content-Range", f"bytes */{len(owner.body)}")
                        self.end_headers()
                        return
                    self.send_response(206)
                    self.send_header("Content-Range", f"bytes {start}-{len(owner.body) - 1}/{len(owner.body)}")
                else:
                    self.send_response(200)
                data = owner.body[start:]
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                if owner.drop_after is not None:
                    data, owner.drop_after = data[:owner.drop_after], None
                    self.close_connection = True
                self.wfile.write(data)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/pdv2.zip"
        threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class UpdaterCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        self.install, self.data = base / "install", base / "data"
        (self.install / "app-1.0.0").mkdir(parents=True)
        (self.install / "app-1.0.0" / "marker.txt").write_text("old")
        paths.write_pointer(self.install, "1.0.0")
        self.store = Store(self.data / "terminal.sqlite3")
        self.updates = self.data / "updates"
        environment = patch.dict(os.environ, {"PDV_ENV": "test", "PDV_UPDATE_AUTO_APPLY": "off"})
        environment.start()
        self.addCleanup(environment.stop)
        for name in ("PDV_VERSION", "PDV_INSTALL_ROOT", "PDV_TERMINAL_DATA", "PDV_SUPERVISED"):
            os.environ.pop(name, None)
        self.clock = [time.time()]
        self.server = None

    def where(self):
        return {"install_root": self.install, "data_dir": self.data}

    def serve(self, body):
        self.server = PackageServer(body)
        self.addCleanup(self.server.close)
        return self.server

    def publish(self, body, version="1.0.1", **changes):
        if self.server is None:
            self.serve(body)
        else:
            self.server.body = body
        manifest = {"schema": 1, "purpose": "pdv2-release", "component": "terminal", "channel": "stable",
                    "version": version, "sha256": hashlib.sha256(body).hexdigest(), "size_bytes": len(body),
                    "url": self.server.url, "kind": "bundle"}
        manifest.update(changes)
        manifest = {key: value for key, value in manifest.items() if value is not None}
        self.store.set_release_result(manifest)
        return manifest

    def step(self, online=True):
        updater.auto_step(self.store, online, block=True, now=lambda: self.clock[0], **self.where())

    def status(self):
        return updater.status(self.store, **self.where())

    def state(self):
        return updater.load_state(self.updates)

    def open_cash(self):
        with self.store.transaction() as conn:
            conn.execute("INSERT INTO cash_sessions(id,operator_id,opening_cents,opened_at) VALUES ('c1','op',0,1)")

    def queue_print(self, status="queued", updated_at=None):
        now = int(time.time() * 1000)
        with self.store.transaction() as conn:
            conn.execute("""INSERT INTO print_jobs(id,kind,payload,driver,target,status,attempts,created_at,updated_at)
                            VALUES ('j1','receipt',x'00','file','x',?,0,?,?)""",
                         (status, now, now if updated_at is None else updated_at))


class DevelopmentInstallTests(UpdaterCase):
    def test_source_checkout_never_downloads_or_applies(self):
        self.publish(make_bundle("1.0.1"))
        empty = Path(self.temp.name) / "no-install"
        empty.mkdir()
        where = {"install_root": empty, "data_dir": self.data}
        updater.auto_step(self.store, True, block=True, **where)
        status = updater.status(self.store, **where)
        self.assertFalse(status["can_apply"])
        self.assertEqual(status["blocked_reason"], "instalação de desenvolvimento")
        self.assertEqual(status["state"], "idle")
        self.assertEqual(self.server.ranges, [])
        self.assertFalse(self.updates.exists())
        result = updater.request_apply(self.store, **where)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "instalação de desenvolvimento")

    def test_default_resolution_without_install_root_is_development(self):
        with patch.dict(os.environ, {"PDV_TERMINAL_DATA": str(self.data)}):
            status = updater.status(self.store)
        self.assertFalse(status["installed"])
        self.assertEqual(status["blocked_reason"], updater.DEV_REASON)
        self.assertEqual(set(status) >= {"current_version", "available", "staged_version", "state", "error",
                                         "can_apply", "blocked_reason"}, True)


class DownloadTests(UpdaterCase):
    def test_download_verifies_and_stages(self):
        manifest = self.publish(make_bundle("1.0.1"))
        self.assertEqual(self.status()["blocked_reason"], "o pacote ainda não foi baixado")
        self.step()
        status = self.status()
        self.assertEqual((status["state"], status["staged_version"]), ("staged", "1.0.1"))
        self.assertTrue(status["can_apply"])
        self.assertEqual(status["available"], manifest)
        self.assertEqual(status["current_version"], "1.0.0")
        package = self.updates / "1.0.1" / "package.zip"
        self.assertEqual(hashlib.sha256(package.read_bytes()).hexdigest(), manifest["sha256"])
        self.step()  # already staged: no second download
        self.assertEqual(len(self.server.ranges), 1)

    def test_offline_does_not_download(self):
        self.publish(make_bundle("1.0.1"))
        self.step(online=False)
        self.assertEqual(self.server.ranges, [])
        self.assertEqual(self.state()["state"], "idle")

    def test_wrong_hash_is_discarded_and_retried_only_later(self):
        body = make_bundle("1.0.1")
        self.publish(body, sha256="0" * 64)
        self.step()
        state = self.state()
        self.assertEqual(state["state"], "failed")
        self.assertIn("SHA-256", state["error"])
        self.assertFalse((self.updates / "1.0.1").exists())
        self.assertFalse(self.status()["can_apply"])
        self.step()
        self.assertEqual(len(self.server.ranges), 1, "retry must wait for the backoff")
        self.clock[0] += updater.RETRY_BASE_SECONDS + 1
        self.publish(body)  # fixed manifest
        self.step()
        self.assertEqual(self.state()["state"], "staged")

    def test_package_larger_than_manifest_is_rejected(self):
        body = make_bundle("1.0.1")
        self.publish(body, size_bytes=len(body) - 10)
        self.step()
        self.assertEqual(self.state()["state"], "failed")
        self.assertIn("Tamanho", self.state()["error"])
        self.assertFalse((self.updates / "1.0.1").exists())

    def test_package_smaller_than_manifest_is_rejected(self):
        body = make_bundle("1.0.1")
        self.publish(body, size_bytes=len(body) + 10)
        self.step()  # looks like a dropped connection first
        self.assertEqual(self.state()["state"], "downloading")
        self.clock[0] += updater.RESUME_RETRY_SECONDS + 1
        self.step()  # the server has nothing after the last byte
        self.assertEqual(self.state()["state"], "failed")
        self.assertIn("Tamanho", self.state()["error"])
        self.assertFalse((self.updates / "1.0.1").exists())

    def test_connection_drop_keeps_partial_file_and_resumes(self):
        body = make_bundle("1.0.1", {"terminal/big.bin": os.urandom(300000)})
        self.publish(body)
        self.server.drop_after = 100000
        self.step()
        state = self.state()
        self.assertEqual(state["state"], "downloading")
        self.assertIn("retomado", state["error"])
        part = self.updates / "1.0.1" / "package.zip.part"
        kept = part.stat().st_size
        self.assertTrue(0 < kept <= 100000)
        self.assertEqual(self.status()["downloaded_bytes"], kept)
        self.assertEqual(self.status()["blocked_reason"], "o pacote ainda está sendo baixado")
        self.step()
        self.assertEqual(len(self.server.ranges), 1, "resume waits a little before trying again")
        self.clock[0] += updater.RESUME_RETRY_SECONDS + 1
        self.step()
        self.assertEqual(self.server.ranges, [None, f"bytes={kept}-"])
        self.assertEqual(self.state()["state"], "staged")
        self.assertEqual((self.updates / "1.0.1" / "package.zip").read_bytes(), body)
        self.assertFalse(part.exists())

    def test_server_without_range_support_restarts_from_zero(self):
        body = make_bundle("1.0.1", {"terminal/big.bin": os.urandom(200000)})
        self.publish(body)
        self.server.drop_after = 50000
        self.step()
        self.server.ignore_range = True
        self.clock[0] += updater.RESUME_RETRY_SECONDS + 1
        self.step()
        self.assertEqual(self.state()["state"], "staged")
        self.assertEqual((self.updates / "1.0.1" / "package.zip").read_bytes(), body)

    def test_unreachable_server_keeps_trying_without_failing_the_worker(self):
        self.publish(make_bundle("1.0.1"))
        self.server.close()
        self.step()
        self.assertEqual(self.state()["state"], "downloading")
        self.assertIn("conexão", self.state()["error"])

    def test_malicious_bundle_with_valid_hash_is_never_staged(self):
        body = make_bundle("1.0.1", extra=[("app-1.0.1/../../evil.txt", "x")])
        self.publish(body)
        self.step()
        self.assertEqual(self.state()["state"], "failed")
        self.assertIn("caminho inválido", self.state()["error"])
        self.assertIsNone(self.status()["staged_version"])
        self.assertFalse((Path(self.temp.name) / "evil.txt").exists())

    def test_installer_kind_missing_kind_and_min_version_are_not_downloaded(self):
        body = make_bundle("1.0.1")
        for changes, text in (({"kind": "installer"}, "instalador completo"),
                              ({"kind": None}, "não traz pacote"),
                              ({"min_version": "1.0.1"}, "mínimo 1.0.1")):
            self.publish(body, **changes)
            self.step()
            status = self.status()
            self.assertFalse(status["can_apply"])
            self.assertIn(text, status["blocked_reason"])
        self.assertEqual(self.server.ranges, [])
        self.publish(body, min_version="1.0.0")
        self.step()
        self.assertTrue(self.status()["can_apply"])

    def test_same_or_older_version_is_not_an_update(self):
        for version in ("1.0.0", "0.9.9"):
            self.publish(make_bundle(version), version=version)
            self.step()
            self.assertIsNone(self.status()["available"])
            self.assertEqual(self.status()["blocked_reason"], "nenhuma atualização disponível")
        self.assertEqual(self.server.ranges, [])

    def test_withdrawn_release_clears_the_staged_package(self):
        self.publish(make_bundle("1.0.1"))
        self.step()
        self.store.set_release_result()
        self.step()
        self.assertEqual(self.state()["state"], "idle")
        self.assertFalse((self.updates / "1.0.1").exists())

    def test_republished_version_with_other_contents_is_downloaded_again(self):
        self.publish(make_bundle("1.0.1"))
        self.step()
        second = make_bundle("1.0.1", {"terminal/hello.txt": "outro"})
        self.publish(second)
        self.step()  # drops the stale package
        self.step()
        self.assertEqual((self.updates / "1.0.1" / "package.zip").read_bytes(), second)
        self.assertEqual(self.state()["state"], "staged")

    def test_corrupted_state_file_is_treated_as_idle(self):
        self.updates.mkdir(parents=True)
        (self.updates / "state.json").write_text("{not json")
        self.assertEqual(self.state(), {"state": "idle"})
        (self.updates / "state.json").write_text('{"state": "weird"}')
        self.assertEqual(self.state(), {"state": "idle"})

    def test_background_download_does_not_block_the_worker(self):
        self.publish(make_bundle("1.0.1"))
        updater.auto_step(self.store, True, **self.where())
        deadline = time.time() + 10
        while self.state()["state"] != "staged" and time.time() < deadline:
            time.sleep(0.05)
        self.assertEqual(self.state()["state"], "staged")


class UrlPolicyTests(unittest.TestCase):
    def test_https_is_mandatory_outside_development(self):
        self.assertTrue(updater.check_url("https://downloads.example.com/a.zip", {}))
        for env in ({}, {"PDV_ENV": "production"}):
            with self.assertRaises(updater.UpdateError):
                updater.check_url("http://127.0.0.1:8000/a.zip", env)
        self.assertTrue(updater.check_url("http://127.0.0.1:8000/a.zip", {"PDV_ENV": "test"}))
        self.assertTrue(updater.check_url("http://localhost/a.zip", {"PDV_ENV": "development"}))
        for url in ("http://downloads.example.com/a.zip", "ftp://127.0.0.1/a.zip", "file:///etc/passwd",
                    "https://user:pw@example.com/a.zip", "https:///a.zip"):
            with self.assertRaises(updater.UpdateError, msg=url):
                updater.check_url(url, {"PDV_ENV": "test"})

    def test_redirect_to_plain_http_is_refused(self):
        handler = updater._SafeRedirect({"PDV_ENV": "production"})
        request = urllib.request.Request("https://downloads.example.com/a.zip")
        with self.assertRaises(urllib.error.URLError):
            handler.redirect_request(request, None, 302, "Found", {}, "http://evil.example.com/a.zip")
        self.assertIsNotNone(handler.redirect_request(request, None, 302, "Found", {},
                                                      "https://cdn.example.com/a.zip"))


class BundleSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.install = self.base / "install"
        self.install.mkdir()

    def package(self, body):
        path = self.base / "package.zip"
        path.write_bytes(body)
        return path

    def assert_rejected(self, body, text, **kwargs):
        with self.assertRaises(updater.UpdateError) as caught:
            updater.extract_bundle(self.package(body), "1.0.1", self.install, **kwargs)
        self.assertIn(text, str(caught.exception))
        self.assertEqual([entry.name for entry in self.install.iterdir()], [], "nothing may be left behind")
        self.assertEqual(sorted(entry.name for entry in self.base.iterdir()), ["install", "package.zip"])

    def test_good_bundle_is_extracted(self):
        body = make_bundle("1.0.1", {"terminal/a.txt": "a", "shared/deep/b.txt": "b"})
        target = updater.extract_bundle(self.package(body), "1.0.1", self.install, "source")
        self.assertEqual(target, self.install / "app-1.0.1")
        self.assertEqual((target / "shared" / "deep" / "b.txt").read_text(), "b")
        self.assertEqual(json.loads((target / "bundle.json").read_text())["version"], "1.0.1")
        self.assertEqual(sorted(entry.name for entry in self.install.iterdir()), ["app-1.0.1"])

    def test_leftover_folder_of_a_previous_attempt_is_replaced(self):
        (self.install / "app-1.0.1").mkdir()
        (self.install / "app-1.0.1" / "stale.txt").write_text("x")
        updater.extract_bundle(self.package(make_bundle("1.0.1")), "1.0.1", self.install)
        self.assertFalse((self.install / "app-1.0.1" / "stale.txt").exists())

    def test_path_traversal_is_rejected(self):
        for name in ("app-1.0.1/../evil.txt", "app-1.0.1/a/../../evil.txt", "../evil.txt",
                     "app-1.0.1/./x.txt", "app-1.0.1//x.txt"):
            self.assert_rejected(make_bundle("1.0.1", extra=[(name, "x")]), "caminho inválido")

    def test_absolute_and_windows_paths_are_rejected(self):
        for name in ("/etc/cron.d/evil", "C:/Windows/evil.dll", "app-1.0.1\\..\\evil.txt",
                     "app-1.0.1/file.txt:stream", "app-1.0.1/trailing. "):
            self.assert_rejected(make_bundle("1.0.1", extra=[(name, "x")]), "caminho inválido")

    def test_files_outside_the_version_folder_are_rejected(self):
        for name in ("evil.txt", "app-1.0.2/x.txt", "app-1.0.10/x.txt", "bin/PDV2Service.exe", "current.txt"):
            self.assert_rejected(make_bundle("1.0.1", extra=[(name, "x")]), "fora de app-1.0.1/")

    def test_symbolic_links_are_rejected(self):
        body = make_bundle("1.0.1", extra=[(symlink_entry("app-1.0.1/link"), "/etc/passwd")])
        self.assert_rejected(body, "link ou arquivo especial")

    def test_duplicate_entries_are_rejected(self):
        body = make_bundle("1.0.1", {"A.txt": "1"}, extra=[("app-1.0.1/a.txt", "2")])
        self.assert_rejected(body, "repetido")

    def test_marker_must_match_version_and_format(self):
        self.assert_rejected(make_bundle("1.0.1", marker=False), "identificação")
        wrong = make_bundle("1.0.1", marker=False,
                            extra=[("app-1.0.1/bundle.json", json.dumps({"version": "9.9.9", "format": "source"}))])
        self.assert_rejected(wrong, "não corresponde")
        self.assert_rejected(make_bundle("1.0.1", fmt="source"), "incompatível", expected_format="frozen-win64")

    def test_not_a_zip_is_rejected(self):
        self.assert_rejected(b"MZ this is an executable, not a zip", "corrompido")

    def test_cleanup_keeps_current_and_previous(self):
        for name in ("app-0.8.0", "app-0.9.0", "app-1.0.0", "app-1.0.1", ".staging-1.0.2-abcd", "bin", "app-data"):
            (self.install / name).mkdir()
        (self.install / "current.txt").write_text("1.0.1\n")
        removed = updater.cleanup_versions(self.install, keep=("1.0.1", "1.0.0", None))
        self.assertEqual(sorted(removed), [".staging-1.0.2-abcd", "app-0.8.0", "app-0.9.0"])
        self.assertEqual(sorted(entry.name for entry in self.install.iterdir()),
                         ["app-1.0.0", "app-1.0.1", "app-data", "bin", "current.txt"])


class ApplyTests(UpdaterCase):
    def stage(self, version="1.0.1"):
        self.publish(make_bundle(version, {"marker.txt": "new"}), version=version)
        self.step()
        self.assertEqual(self.state()["state"], "staged")

    def apply(self):
        result = updater.request_apply(self.store, **self.where())
        self.assertTrue(result["ok"], result)
        return result

    def test_open_cashier_blocks_the_update(self):
        self.stage()
        self.open_cash()
        status = self.status()
        self.assertFalse(status["can_apply"])
        self.assertIn("caixa aberto", status["blocked_reason"])
        result = updater.request_apply(self.store, **self.where())
        self.assertFalse(result["ok"])
        self.assertIn("caixa aberto", result["error"])
        self.assertEqual(paths.read_pointer(self.install), "1.0.0")
        self.assertFalse((self.install / "app-1.0.1").exists())
        self.assertEqual(self.state()["state"], "staged")

    def test_busy_print_queue_blocks_the_update(self):
        self.stage()
        self.queue_print("queued")
        self.assertIn("impressões", self.status()["blocked_reason"])
        self.assertFalse(updater.request_apply(self.store, **self.where())["ok"])

    def test_job_stuck_in_sending_does_not_block_forever(self):
        self.stage()
        self.queue_print("sending", updated_at=int(time.time() * 1000) - 10 * 60 * 1000)
        self.assertTrue(self.status()["can_apply"])

    def test_apply_backs_up_extracts_and_swaps_the_pointer(self):
        self.stage()
        result = self.apply()
        self.assertEqual((result["version"], result["previous_version"], result["restart"]),
                         ("1.0.1", "1.0.0", "manual"))
        self.assertEqual(paths.read_pointer(self.install), "1.0.1")
        self.assertEqual((self.install / "app-1.0.1" / "marker.txt").read_text(), "new")
        self.assertTrue((self.install / "app-1.0.0").is_dir(), "previous version stays for rollback")
        self.assertTrue((self.data / "backups" / result["backup"]).is_file())
        self.assertTrue((self.updates / updater.RESTART_REQUEST).is_file())
        state = self.state()
        self.assertEqual((state["state"], state["previous_version"], state["version"]),
                         ("applying", "1.0.0", "1.0.1"))
        again = updater.request_apply(self.store, **self.where())
        self.assertFalse(again["ok"])
        self.assertIn("em andamento", again["error"])

    def test_supervised_process_reports_service_restart(self):
        self.stage()
        with patch.dict(os.environ, {"PDV_SUPERVISED": "1"}):
            self.assertEqual(self.apply()["restart"], "service")

    def test_failed_backup_cancels_the_update(self):
        self.stage()
        with patch.object(Store, "backup_now", side_effect=RuntimeError("disco cheio")):
            result = updater.request_apply(self.store, **self.where())
        self.assertFalse(result["ok"])
        self.assertIn("Backup", result["error"])
        self.assertEqual(paths.read_pointer(self.install), "1.0.0")
        self.assertFalse((self.install / "app-1.0.1").exists())

    def test_package_tampered_after_staging_is_refused(self):
        self.stage()
        package = self.updates / "1.0.1" / "package.zip"
        package.write_bytes(package.read_bytes()[:-1] + b"X")
        result = updater.request_apply(self.store, **self.where())
        self.assertFalse(result["ok"])
        self.assertIn("SHA-256", result["error"])
        self.assertEqual(paths.read_pointer(self.install), "1.0.0")
        self.assertFalse(package.exists())

    def test_health_confirmation_keeps_the_new_version_and_cleans_old_ones(self):
        (self.install / "app-0.9.0").mkdir()
        self.stage()
        self.apply()
        outcome = updater.confirm_or_rollback(self.install, self.data, lambda: True)
        self.assertEqual(outcome, "confirmed")
        self.assertEqual(paths.read_pointer(self.install), "1.0.1")
        self.assertEqual(sorted(entry.name for entry in self.install.iterdir()),
                         ["app-1.0.0", "app-1.0.1", "current.txt"])
        self.assertFalse((self.updates / "1.0.1").exists())
        state = self.state()
        self.assertEqual((state["state"], state["confirmed_version"]), ("idle", "1.0.1"))
        self.assertIsNone(updater.confirm_or_rollback(self.install, self.data, lambda: False))
        with patch.dict(os.environ, {"PDV_VERSION": "1.0.1"}):
            status = self.status()
        self.assertEqual((status["current_version"], status["available"]), ("1.0.1", None))

    def test_support_can_roll_back_a_confirmed_version(self):
        self.stage()
        self.apply()
        updater.confirm_or_rollback(self.install, self.data, lambda: True)
        self.assertEqual(updater.rollback(self.install, self.data, "Volta pedida pelo suporte."), "1.0.0")
        self.assertEqual(paths.read_pointer(self.install), "1.0.0")
        self.assertIn("suporte", self.state()["error"])
        self.step()
        self.assertEqual(self.state()["state"], "failed", "the rolled back version is not fetched again")

    def test_unhealthy_start_rolls_back_automatically(self):
        self.stage()
        self.apply()
        ticks = iter(range(0, 1000, 10))
        slept = []
        outcome = updater.confirm_or_rollback(self.install, self.data, lambda: False, timeout=60,
                                              sleep=slept.append, clock=lambda: next(ticks))
        self.assertEqual(outcome, "rolled_back")
        self.assertEqual(len(slept), 5, "keeps probing until the 60 s deadline")
        self.assertEqual(paths.read_pointer(self.install), "1.0.0")
        self.assertFalse((self.install / "app-1.0.1").exists())
        self.assertTrue((self.install / "app-1.0.0" / "marker.txt").is_file())
        state = self.state()
        self.assertEqual((state["state"], state["failed_version"]), ("failed", "1.0.1"))
        self.assertIn("voltou para a versão 1.0.0", state["error"])
        status = self.status()
        self.assertEqual((status["state"], status["can_apply"]), ("failed", False))
        self.assertIn("não iniciou", status["blocked_reason"])
        hits = len(self.server.ranges)
        self.clock[0] += 10 * updater.RETRY_MAX_SECONDS
        self.step()
        self.assertEqual(len(self.server.ranges), hits, "a version that failed is not downloaded again")
        self.stage("1.0.2")  # a newer release is accepted
        self.assertTrue(self.status()["can_apply"])

    def test_process_that_dies_fails_fast(self):
        slept = []
        self.assertFalse(updater.await_health(lambda: False, 60, alive=lambda: False, sleep=slept.append))
        self.assertEqual(slept, [])

    def test_rollback_without_previous_version_reports_instead_of_breaking(self):
        self.stage()
        self.apply()
        import shutil
        shutil.rmtree(self.install / "app-1.0.0")
        self.assertIsNone(updater.rollback(self.install, self.data))
        self.assertEqual(paths.read_pointer(self.install), "1.0.1")
        self.assertIn("reinstale", self.state()["error"])

    def test_unattended_apply_only_in_the_quiet_window_and_with_cashier_closed(self):
        self.stage()
        with patch.dict(os.environ, {"PDV_UPDATE_AUTO_APPLY": "off"}):
            self.step()
        self.assertEqual(paths.read_pointer(self.install), "1.0.0")
        self.open_cash()
        with patch.dict(os.environ, {"PDV_UPDATE_AUTO_APPLY": "0-24"}):
            self.step()
            self.assertEqual(paths.read_pointer(self.install), "1.0.0", "never with an open cashier")
            with self.store.transaction() as conn:
                conn.execute("UPDATE cash_sessions SET closed_at=2")
            self.step()
        self.assertEqual(paths.read_pointer(self.install), "1.0.1")
        self.assertEqual(self.state()["state"], "applying")

    def test_quiet_window_parsing(self):
        two_am = time.mktime((2026, 11, 16, 2, 30, 0, 0, 0, -1))
        noon = time.mktime((2026, 11, 16, 12, 0, 0, 0, 0, -1))
        self.assertTrue(updater._auto_apply_now({}, two_am))
        self.assertFalse(updater._auto_apply_now({}, noon))
        self.assertTrue(updater._auto_apply_now({"PDV_UPDATE_AUTO_APPLY": "22-4"}, two_am))
        self.assertFalse(updater._auto_apply_now({"PDV_UPDATE_AUTO_APPLY": "22-4"}, noon))
        for value in ("off", "0", "", "abc", "1-2-3"):
            self.assertFalse(updater._auto_apply_now({"PDV_UPDATE_AUTO_APPLY": value}, two_am), value)


class PackagingTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location("package_terminal", ROOT / "scripts" / "package_terminal.py")
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.out = Path(self.temp.name)

    def test_default_run_keeps_the_source_zip_and_adds_a_bundle(self):
        with patch("builtins.print"):
            results = self.module.main(["--out", str(self.out), "--version", "3.2.1"])
        (legacy, legacy_meta), (bundle, bundle_meta) = results
        self.assertEqual(legacy.name, "PDV2-Terminal-3.2.1.zip")
        self.assertNotIn("kind", legacy_meta)
        with zipfile.ZipFile(legacy) as archive:
            self.assertIn("terminal/app.py", archive.namelist())
        self.assertEqual(bundle_meta["kind"], "bundle")
        self.assertEqual(bundle_meta["sha256"], hashlib.sha256(bundle.read_bytes()).hexdigest())
        self.assertEqual(bundle_meta["size_bytes"], bundle.stat().st_size)
        self.assertEqual(json.loads((self.out / "PDV2-Terminal-3.2.1-bundle.metadata.json").read_text()), bundle_meta)
        self.assertEqual(updater.inspect_bundle(bundle, "3.2.1", "source")["format"], "source")
        with zipfile.ZipFile(bundle) as archive:
            names = archive.namelist()
        self.assertIn("app-3.2.1/terminal/updater.py", names)
        self.assertIn("app-3.2.1/shared/licensing.py", names)
        self.assertFalse([name for name in names if "/tests/" in name or name.endswith((".sqlite3", ".pem", ".pyc"))])

    def test_frozen_bundle_and_installer_metadata(self):
        frozen = self.out / "stage" / "app-3.2.1"
        (frozen / "_internal").mkdir(parents=True)
        (frozen / "PDV2Server.exe").write_bytes(b"MZ")
        (frozen / "_internal" / "base.zip").write_bytes(b"PK")
        (frozen / "bundle.json").write_text("{}")
        installer = self.out / "PDV2-Instalar-3.2.1.exe"
        installer.write_bytes(b"MZ installer")
        with patch("builtins.print"):
            (_, setup_meta), (bundle, meta) = self.module.main(
                ["--out", str(self.out), "--version", "3.2.1", "--frozen-dir", str(frozen),
                 "--installer", str(installer)])
        self.assertEqual((setup_meta["kind"], meta["kind"]), ("installer", "bundle"))
        self.assertEqual(bundle.name, "PDV2-Terminal-3.2.1-win64-bundle.zip")
        self.assertEqual(updater.inspect_bundle(bundle, "3.2.1", "frozen-win64")["format"], "frozen-win64")
        with self.assertRaises(SystemExit):
            self.module.build_frozen_bundle("3.2.1", self.out, self.out)


if __name__ == "__main__":
    unittest.main()
