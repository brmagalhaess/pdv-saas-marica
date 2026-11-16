"""Service host: configuration, supervision, version swap and legacy data migration.

The pywin32 wrapper itself only runs on Windows; everything it delegates to is
exercised here with real child processes.
"""

import hashlib
import importlib.util
import logging
import os
import shutil
import socket
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from logging.handlers import RotatingFileHandler
from pathlib import Path
from unittest.mock import patch

from terminal import paths, service_win, updater
from terminal.store import Store

try:
    from terminal.tests.test_updater import PackageServer, make_bundle
except ImportError:  # discovered as a top-level module
    from test_updater import PackageServer, make_bundle

ROOT = Path(__file__).resolve().parents[2]
WINDOWS_ENV = {"ProgramData": r"C:\ProgramData", "ProgramFiles": r"C:\Program Files",
               "LOCALAPPDATA": r"C:\Users\Caixa\AppData\Local"}

FAKE_CHILD = """
import os, sys, time
data = os.environ["PDV_TERMINAL_DATA"]
version = os.environ["PDV_VERSION"]
with open(os.path.join(data, "starts.log"), "a") as log:
    log.write(version + "\\n")
if {broken!r}:
    sys.exit(3)
open(os.path.join(data, "alive-" + version), "w").close()
while not os.path.exists(os.environ["PDV_STOP_FILE"]):
    time.sleep(0.05)
os.remove(os.path.join(data, "alive-" + version))
with open(os.path.join(data, "starts.log"), "a") as log:
    log.write("clean-stop " + version + "\\n")
"""


def wait_for(condition, timeout=20, message="condition"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(0.05)
    raise AssertionError("timeout waiting for " + message)


class ConfigTests(unittest.TestCase):
    def test_windows_service_uses_program_data_and_auto_restart(self):
        config = service_win.service_config(WINDOWS_ENV, platform="win32")
        self.assertEqual(config["name"], "PDV2Service")
        self.assertEqual(config["install_root"], r"C:\Program Files\PDV2")
        self.assertEqual(config["data_dir"], r"C:\ProgramData\PDV2")
        self.assertEqual(config["service_log"], r"C:\ProgramData\PDV2\logs\service.log")
        self.assertEqual(config["server_log"], r"C:\ProgramData\PDV2\logs\terminal.log")
        self.assertEqual((config["host"], config["port"], config["start"]), ("127.0.0.1", 8010, "auto"))
        self.assertEqual(config["health_timeout"], 60)
        self.assertIn("restart", config["failure_actions"])

    def test_explicit_data_folder_and_port_take_precedence(self):
        env = {**WINDOWS_ENV, "PDV_TERMINAL_DATA": r"D:\PDV", "PDV_TERMINAL_PORT": "8020",
               "PDV_INSTALL_ROOT": r"D:\Programas\PDV2"}
        config = service_win.service_config(env, platform="win32")
        self.assertEqual((config["data_dir"], config["port"], config["install_root"]),
                         (r"D:\PDV", 8020, r"D:\Programas\PDV2"))
        self.assertEqual(service_win.service_config({"PDV_TERMINAL_PORT": "abc"}, platform="win32")["port"], 8010)

    def test_install_commands_quote_the_path_and_configure_recovery(self):
        config = service_win.service_config(WINDOWS_ENV, platform="win32")
        commands = service_win.install_commands(config, r"C:\Program Files\PDV2\bin\PDV2Service.exe")
        create = commands[0]
        self.assertEqual(create[:3], ["sc", "create", "PDV2Service"])
        self.assertEqual(create[create.index("binPath=") + 1], r'"C:\Program Files\PDV2\bin\PDV2Service.exe"')
        self.assertEqual(create[create.index("start=") + 1], "auto")
        failure = next(command for command in commands if command[1] == "failure")
        self.assertEqual(failure[failure.index("actions=") + 1], "restart/5000/restart/5000/restart/30000")
        self.assertEqual(failure[failure.index("reset=") + 1], "86400")
        self.assertEqual(commands[-1], ["sc", "start", "PDV2Service"])
        self.assertEqual(service_win.uninstall_commands(config),
                         [["net", "stop", "PDV2Service"], ["sc", "delete", "PDV2Service"]])

    def test_child_command_prefers_the_frozen_server(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "app-1.2.3").mkdir()
            argv, cwd = service_win.child_command(root, "1.2.3", python="python3")
            self.assertEqual(argv, ["python3", "-m", "terminal.service_win", "--serve"])
            self.assertEqual(cwd, str(root / "app-1.2.3"))
            (root / "app-1.2.3" / "PDV2Server.exe").write_bytes(b"MZ")
            argv, cwd = service_win.child_command(root, "1.2.3")
            self.assertEqual(argv, [str(root / "app-1.2.3" / "PDV2Server.exe")])

    def test_child_environment_pins_data_version_and_stop_file(self):
        config = service_win.service_config(WINDOWS_ENV, platform="win32")
        env = service_win.child_env(config, "1.2.3", r"C:\ProgramData\PDV2\run\stop-1",
                                    base={"PATH": "x", "PDV_TERMINAL_DB": r"C:\outro.sqlite3"})
        self.assertEqual(env["PDV_TERMINAL_DATA"], r"C:\ProgramData\PDV2")
        self.assertEqual((env["PDV_VERSION"], env["PDV_SUPERVISED"], env["PATH"]), ("1.2.3", "1", "x"))
        self.assertEqual(env["PDV_STOP_FILE"], r"C:\ProgramData\PDV2\run\stop-1")
        self.assertNotIn("PDV_TERMINAL_DB", env)

    def test_log_is_a_rotating_file_and_is_not_duplicated(self):
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "logs" / "service.log"
            logger = service_win.setup_logging(target, "pdv2.test-log")
            self.assertIs(service_win.setup_logging(target, "pdv2.test-log"), logger)
            handlers = [handler for handler in logger.handlers if isinstance(handler, RotatingFileHandler)]
            self.assertEqual(len(handlers), 1)
            self.assertEqual((handlers[0].maxBytes, handlers[0].backupCount), (1024 * 1024, 5))
            logger.info("serviço iniciado")
            handlers[0].flush()
            self.assertIn("serviço iniciado", target.read_text(encoding="utf-8"))
            handlers[0].close()
            logger.removeHandler(handlers[0])

    def test_main_without_pywin32_explains_itself(self):
        if service_win.win32serviceutil is not None:
            self.skipTest("pywin32 instalado")
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {"PDV_TERMINAL_DATA": temp}), \
                patch("builtins.print") as printed:
            self.assertEqual(service_win.main([]), 2)
            self.assertIn("só existe no Windows", printed.call_args[0][0])
            for handler in list(logging.getLogger("pdv2.service").handlers):
                handler.close()
                logging.getLogger("pdv2.service").removeHandler(handler)


class SupervisorCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        self.install, self.data = base / "install", base / "data"
        self.data.mkdir()
        self.config = {**service_win.service_config({"PDV_TERMINAL_DATA": str(self.data),
                                                     "PDV_INSTALL_ROOT": str(self.install)}),
                       "port": self.free_port()}
        self.supervisor = None

    @staticmethod
    def free_port():
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            return probe.getsockname()[1]

    def start(self, **kwargs):
        quiet = logging.getLogger("pdv2.test-supervisor")
        quiet.propagate = False
        if not quiet.handlers:
            quiet.addHandler(logging.NullHandler())
        self.supervisor = service_win.Supervisor(self.config, poll=0.05, logger=quiet, **kwargs)
        self.thread = threading.Thread(target=self.supervisor.run, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop)

    def stop(self):
        self.supervisor.stop()
        self.thread.join(timeout=40)
        self.assertFalse(self.thread.is_alive())


class FakeChildTests(SupervisorCase):
    def add_version(self, version, broken=False):
        folder = self.install / f"app-{version}"
        folder.mkdir(parents=True)
        (folder / "child.py").write_text(FAKE_CHILD.format(broken=broken))

    def command(self, root, version):
        folder = paths.app_dir(root, version)
        return [sys.executable, str(folder / "child.py")], str(folder)

    def probe(self):
        return (self.data / f"alive-{paths.read_pointer(self.install)}").exists()

    def starts(self):
        log = self.data / "starts.log"
        return log.read_text().split("\n")[:-1] if log.exists() else []

    def begin(self, **kwargs):
        self.start(command=self.command, probe=self.probe, **kwargs)

    def test_runs_the_pointed_version_and_stops_cleanly(self):
        self.add_version("1.0.0")
        paths.write_pointer(self.install, "1.0.0")
        self.begin()
        wait_for(lambda: (self.data / "alive-1.0.0").exists(), message="child start")
        self.stop()
        self.assertEqual(self.starts(), ["1.0.0", "clean-stop 1.0.0"])
        self.assertEqual(list((self.data / "run").iterdir()), [])

    def test_restarts_the_child_when_it_dies(self):
        self.add_version("1.0.0")
        paths.write_pointer(self.install, "1.0.0")
        self.begin()
        wait_for(lambda: (self.data / "alive-1.0.0").exists(), message="child start")
        self.supervisor.child.kill()
        wait_for(lambda: self.starts().count("1.0.0") == 2, message="restart after crash")

    def test_child_that_stops_answering_is_restarted(self):
        self.add_version("1.0.0")
        paths.write_pointer(self.install, "1.0.0")
        self.start(command=self.command, probe=lambda: False, watchdog_seconds=0.3)
        wait_for(lambda: self.starts()[:3] == ["1.0.0", "clean-stop 1.0.0", "1.0.0"], message="watchdog restart")

    def apply(self, version):
        """What updater.request_apply leaves behind for the host."""
        previous = paths.read_pointer(self.install)
        updater.save_state(self.data / "updates", state="applying", version=version, previous_version=previous)
        paths.write_pointer(self.install, version)
        (self.data / "updates" / updater.RESTART_REQUEST).write_text("{}")

    def test_update_restart_confirms_a_healthy_version(self):
        self.add_version("1.0.0")
        self.add_version("0.9.0")
        self.add_version("1.0.1")
        paths.write_pointer(self.install, "1.0.0")
        self.begin()
        wait_for(lambda: (self.data / "alive-1.0.0").exists(), message="child start")
        self.apply("1.0.1")
        wait_for(lambda: updater.load_state(self.data / "updates").get("confirmed_version") == "1.0.1",
                 message="confirmation")
        self.assertEqual(self.starts(), ["1.0.0", "clean-stop 1.0.0", "1.0.1"])
        self.assertTrue((self.data / "alive-1.0.1").exists())
        self.assertFalse((self.data / "updates" / updater.RESTART_REQUEST).exists())
        self.assertEqual(sorted(entry.name for entry in self.install.iterdir()),
                         ["app-1.0.0", "app-1.0.1", "current.txt"], "only current and previous remain")

    def test_version_that_does_not_start_is_rolled_back(self):
        self.add_version("1.0.0")
        self.add_version("1.0.1", broken=True)
        paths.write_pointer(self.install, "1.0.0")
        self.begin()
        wait_for(lambda: (self.data / "alive-1.0.0").exists(), message="child start")
        self.apply("1.0.1")
        wait_for(lambda: updater.load_state(self.data / "updates")["state"] == "failed", message="rollback")
        wait_for(lambda: (self.data / "alive-1.0.0").exists(), message="previous version back")
        self.assertEqual(paths.read_pointer(self.install), "1.0.0")
        self.assertEqual(self.starts(), ["1.0.0", "clean-stop 1.0.0", "1.0.1", "1.0.0"])
        state = updater.load_state(self.data / "updates")
        self.assertEqual(state["failed_version"], "1.0.1")
        self.assertFalse((self.install / "app-1.0.1").exists())

    def test_version_that_never_answers_health_is_rolled_back_after_the_deadline(self):
        self.add_version("1.0.0")
        self.add_version("1.0.1")
        paths.write_pointer(self.install, "1.0.0")
        updater.save_state(self.data / "updates", state="applying", version="1.0.1", previous_version="1.0.0")
        paths.write_pointer(self.install, "1.0.1")
        # Interrupted confirmation (power cut): it must run again on this start.
        self.start(command=self.command, probe=lambda: False, health_timeout=0.5)
        wait_for(lambda: updater.load_state(self.data / "updates")["state"] == "failed", message="rollback")
        wait_for(lambda: (self.data / "alive-1.0.0").exists(), message="previous version back")
        self.assertEqual(self.starts()[:3], ["1.0.1", "clean-stop 1.0.1", "1.0.0"])

    def test_missing_pointer_does_not_crash_the_host(self):
        self.install.mkdir()
        self.begin()
        time.sleep(0.3)
        self.assertTrue(self.thread.is_alive())
        self.assertEqual(self.starts(), [])


class RealTerminalTests(SupervisorCase):
    """End to end on this machine: real terminal server, real download, swap and rollback."""

    def install_source(self, version):
        folder = self.install / f"app-{version}"
        ignore = shutil.ignore_patterns("__pycache__", "tests", "*.sqlite3", "*.pyc")
        for package in ("terminal", "shared"):
            shutil.copytree(ROOT / package, folder / package, ignore=ignore)
        (folder / "bundle.json").write_text('{"version": "%s", "format": "source"}' % version)

    def log_text(self):
        log = self.data / "logs" / "terminal.log"
        return log.read_text(encoding="utf-8") if log.exists() else ""

    def test_update_swap_and_rollback_with_the_real_server(self):
        spec = importlib.util.spec_from_file_location("package_terminal", ROOT / "scripts" / "package_terminal.py")
        packager = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(packager)
        self.install_source("1.0.0")
        paths.write_pointer(self.install, "1.0.0")
        env = {key: value for key, value in os.environ.items() if not key.startswith("PDV_")}
        env["PDV_ENV"] = "test"
        health = f"http://127.0.0.1:{self.config['port']}/health"
        self.start(base_env=env)
        wait_for(lambda: updater.probe_health(health), 40, "first start of the real terminal")
        self.assertIn("Terminal PDV2 1.0.0", self.log_text())

        out = Path(self.temp.name) / "dist"
        out.mkdir()
        good, _ = packager.build_source_bundle("1.0.1", out)
        server = PackageServer(good.read_bytes())
        self.addCleanup(server.close)
        store = Store(self.data / "terminal.sqlite3")
        where = {"install_root": self.install, "data_dir": self.data}

        def publish(version, body):
            server.body = body
            store.set_release_result({"component": "terminal", "channel": "stable", "version": version,
                                      "sha256": hashlib.sha256(body).hexdigest(), "size_bytes": len(body),
                                      "url": server.url, "kind": "bundle"})

        state = lambda: updater.load_state(self.data / "updates")
        with patch.dict(os.environ, {"PDV_ENV": "test", "PDV_VERSION": "1.0.0", "PDV_UPDATE_AUTO_APPLY": "off"}):
            publish("1.0.1", good.read_bytes())
            updater.auto_step(store, True, block=True, **where)
            result = updater.request_apply(store, **where)
        self.assertTrue(result["ok"], result)
        wait_for(lambda: state().get("confirmed_version") == "1.0.1", 80, "health confirmation of 1.0.1")
        self.assertTrue(updater.probe_health(health))
        self.assertIn("Terminal PDV2 1.0.1", self.log_text())
        self.assertEqual(paths.read_pointer(self.install), "1.0.1")
        with sqlite3.connect(self.data / "backups" / result["backup"]) as backup:
            self.assertEqual(backup.execute("PRAGMA integrity_check").fetchone()[0], "ok")

        broken = make_bundle("1.0.2", {"terminal/__init__.py": "raise RuntimeError('versão quebrada')\n",
                                       "terminal/service_win.py": ""})
        with patch.dict(os.environ, {"PDV_ENV": "test", "PDV_VERSION": "1.0.1", "PDV_UPDATE_AUTO_APPLY": "off"}):
            publish("1.0.2", broken)
            updater.auto_step(store, True, block=True, **where)
            self.assertTrue(updater.request_apply(store, **where)["ok"])
        wait_for(lambda: state()["state"] == "failed", 80, "rollback of 1.0.2")
        self.assertEqual(paths.read_pointer(self.install), "1.0.1")
        wait_for(lambda: updater.probe_health(health), 40, "previous version serving again")
        self.assertEqual(state()["failed_version"], "1.0.2")
        with patch.dict(os.environ, {"PDV_VERSION": "1.0.1"}):
            self.assertIn("não iniciou", updater.status(store, **where)["blocked_reason"])
        self.stop()
        self.assertFalse(updater.probe_health(health), "clean stop releases the port")


class LegacyMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.source = Path(self.temp.name) / "Local" / "PDV2" / "data"
        self.target = Path(self.temp.name) / "ProgramData" / "PDV2"

    def test_candidates_cover_the_legacy_installer_and_the_old_default(self):
        self.assertEqual(service_win.legacy_candidates(Path(self.temp.name) / "Local"),
                         [self.source, self.source.parent])

    def test_copies_database_with_pending_wal_session_key_profiles_and_backups(self):
        store = Store(self.source / "terminal.sqlite3")
        store.set_release_result(error="marca antiga")
        Store(self.source / "profiles" / "loja2" / "terminal.sqlite3")
        (self.source / "session.key").write_bytes(b"k" * 32)
        (self.source / "active-profile.json").write_text('{"profile": "loja2"}')
        (self.source / "backups").mkdir()
        (self.source / "backups" / "terminal-1-abc.sqlite3").write_bytes(b"copy")
        result = service_win.migrate_legacy_data(self.source, self.target)
        self.assertTrue(result["migrated"])
        self.assertEqual(Store(self.target / "terminal.sqlite3").meta()["release_error"], "marca antiga")
        self.assertEqual(Store(self.target / "terminal.sqlite3").meta()["device_id"], store.meta()["device_id"])
        self.assertTrue((self.target / "profiles" / "loja2" / "terminal.sqlite3").is_file())
        self.assertEqual((self.target / "session.key").read_bytes(), b"k" * 32)
        self.assertTrue((self.target / "active-profile.json").is_file())
        self.assertEqual((self.target / "backups" / "terminal-1-abc.sqlite3").read_bytes(), b"copy")
        self.assertTrue((self.source / "terminal.sqlite3").is_file(), "the old data stays where it was")
        self.assertEqual((self.target / "migrated-from.txt").read_text().strip(), str(self.source))

    def test_never_overwrites_an_existing_database(self):
        Store(self.source / "terminal.sqlite3")
        current = Store(self.target / "terminal.sqlite3")
        device = current.meta()["device_id"]
        result = service_win.migrate_legacy_data(self.source, self.target)
        self.assertEqual(result, {"migrated": False, "reason": "já existe um banco no destino"})
        self.assertEqual(Store(self.target / "terminal.sqlite3").meta()["device_id"], device)

    def test_nothing_to_migrate(self):
        self.assertFalse(service_win.migrate_legacy_data(self.source, self.target)["migrated"])
        self.assertFalse(self.target.exists())


class LegacyTerminalStillRunningTests(unittest.TestCase):
    VENV_PYTHON = r"C:\Users\Caixa\AppData\Local\PDV2\.venv\Scripts\python.exe"

    def test_recognises_only_the_python_of_the_old_install(self):
        self.assertTrue(service_win.is_legacy_process("python.exe", self.VENV_PYTHON, ""))
        # The venv launcher re-executes the base interpreter with the original command line.
        self.assertTrue(service_win.is_legacy_process(
            "python.exe", r"C:\Python312\python.exe", f'"{self.VENV_PYTHON.upper()}" -m terminal'))
        self.assertTrue(service_win.is_legacy_process("pythonw.exe", self.VENV_PYTHON.replace("\\", "/")))
        self.assertFalse(service_win.is_legacy_process("python.exe", r"C:\Python312\python.exe", "python -m http.server"))
        self.assertFalse(service_win.is_legacy_process("PDV2Server.exe", r"C:\Program Files\PDV2\app-2.0.1\PDV2Server.exe"))
        self.assertFalse(service_win.is_legacy_process("powershell.exe", "", f"-Command ... '*{service_win.LEGACY_PROCESS_MARKER}*'"))
        self.assertFalse(service_win.is_legacy_process(None, None, None))

    def test_stop_command_filters_by_the_same_marker_and_spares_itself(self):
        command = service_win.legacy_stop_command(r"C:\Windows")
        self.assertEqual(command[0], r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe")
        script = command[-1]
        self.assertIn(f"-like '*{service_win.LEGACY_PROCESS_MARKER}*'", script)
        self.assertIn("$_.Name -like 'python*'", script)
        self.assertIn("$_.ProcessId -ne $PID", script)
        self.assertIn("Stop-Process", script)

    def test_stop_runs_only_on_windows_and_survives_a_missing_powershell(self):
        calls = []
        self.assertFalse(service_win.stop_legacy_processes("darwin", run=lambda *a, **k: calls.append(a)))
        self.assertEqual(calls, [])
        self.assertTrue(service_win.stop_legacy_processes("win32", run=lambda *a, **k: calls.append(a)))
        self.assertEqual(calls[0][0], service_win.legacy_stop_command())

        def missing(*args, **kwargs):
            raise FileNotFoundError("powershell")
        self.assertFalse(service_win.stop_legacy_processes("win32", run=missing))

    def test_blocker_waits_for_the_port_and_gives_up_in_portuguese(self):
        naps = []
        answers = iter([True, True, False])
        self.assertIsNone(service_win.legacy_migration_blocker(lambda: next(answers), sleep=naps.append))
        self.assertEqual(naps, [0.5, 0.5])
        naps.clear()
        reason = service_win.legacy_migration_blocker(lambda: True, attempts=3, sleep=naps.append)
        self.assertIn("PDV2 antigo ainda está aberto", reason)
        self.assertEqual(naps, [0.5, 0.5], "no sleep after the last attempt")
        self.assertIsNone(service_win.legacy_migration_blocker(lambda: False, sleep=naps.append))

    def test_port_in_use_sees_a_real_listener(self):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            port = listener.getsockname()[1]
            self.assertTrue(service_win.port_in_use(port))
        self.assertFalse(service_win.port_in_use(port, timeout=0.5))


class MigrateLegacyCommandTests(unittest.TestCase):
    """`PDV2Service.exe --migrate-legacy`, as the installer calls it."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.source = root / "Local" / "PDV2" / "data"
        self.target = root / "ProgramData" / "PDV2"
        Store(self.source / "terminal.sqlite3").set_release_result(error="venda pendente")
        self.listener = socket.socket()
        self.addCleanup(self.listener.close)
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(32)  # nobody accepts: the backlog must hold every probe
        config = {"data_dir": self.target, "port": self.listener.getsockname()[1],
                  "service_log": root / "logs" / "service.log"}
        for patcher in (patch.object(service_win, "service_config", return_value=config),
                        patch.object(service_win, "stop_legacy_processes"),
                        patch.object(service_win.time, "sleep")):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(self.drop_log_handlers)

    @staticmethod
    def drop_log_handlers():
        logger = logging.getLogger("pdv2.service")
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
            handler.close()

    def test_refuses_to_copy_while_the_old_terminal_answers(self):
        self.assertEqual(service_win.main(["--migrate-legacy", str(self.source)]), service_win.LEGACY_RUNNING_EXIT)
        service_win.stop_legacy_processes.assert_called_once()
        self.assertFalse((self.target / "terminal.sqlite3").exists(),
                         "no database in the target, so the next installer run migrates again")

    def test_copies_once_the_port_is_free(self):
        self.listener.close()
        self.assertEqual(service_win.main(["--migrate-legacy", str(self.source)]), 0)
        self.assertEqual(Store(self.target / "terminal.sqlite3").meta()["release_error"], "venda pendente")


class InstallerScriptTests(unittest.TestCase):
    """pdv2.iss cannot be compiled here; this pins the order that protects the old data."""

    def setUp(self):
        self.script = (ROOT / "installer" / "pdv2.iss").read_text(encoding="utf-8")

    def test_shares_marker_port_and_exit_code_with_the_service(self):
        marker = service_win.LEGACY_PROCESS_MARKER.rstrip("\\")
        self.assertIn(f'#define LegacyMarker "{marker}"', self.script)
        self.assertEqual(self.script.count("-like ''*{#LegacyMarker}\\*''"), 2)
        self.assertIn(f'#define LegacyRunningExit "{service_win.LEGACY_RUNNING_EXIT}"', self.script)
        self.assertIn(f'#define TerminalPort "{paths.terminal_port({})}"', self.script)

    def test_old_terminal_is_stopped_and_the_port_checked_before_anything_is_touched(self):
        prepare = self.script.split("function PrepareToInstall", 1)[1].split("end;\n\n", 1)[0]
        order = [prepare.index(step) for step in
                 ("StopRunningCopy();", "StopLegacyTerminal();", "WaitTerminalPortFree()", "exit;", "RemoveOtherVersions();")]
        self.assertEqual(order, sorted(order))
        self.assertIn("Feche o PDV2 antigo", prepare)

    def test_refused_migration_does_not_register_the_service(self):
        post = self.script.split("procedure CurStepChanged", 1)[1].split("procedure CurUninstallStepChanged", 1)[0]
        refused = post.index("= {#LegacyRunningExit}")
        self.assertLess(refused, post.index("exit;", refused))
        self.assertLess(post.index("exit;", refused), post.index("'--install'"))


class StationServerTests(unittest.TestCase):
    """The service child in station mode: no database, same /health contract, clean stop."""

    def test_child_serves_the_station_bridge_and_stops_on_request(self):
        import json
        import subprocess
        import urllib.request

        with tempfile.TemporaryDirectory() as temp, socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
            probe.close()
            data = Path(temp)
            paths.write_mode(data, "station")
            stop_file = data / "run" / "stop-test"
            stop_file.parent.mkdir()
            env = {**os.environ, "PDV_TERMINAL_DATA": temp, "PDV_TERMINAL_PORT": str(port),
                   "PDV_STOP_FILE": str(stop_file), "PDV_LAN_PORT": "0", "PDV_DISCOVERY_PORT": "0"}
            for name in ("PDV_MODE", "PDV_INSTALL_ROOT", "PDV_SUPERVISED", "PDV_TERMINAL_DB", "PDV_TERMINAL_HOST"):
                env.pop(name, None)
            child = subprocess.Popen([sys.executable, "-m", "terminal.service_win", "--serve"], cwd=str(ROOT),
                                     env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                url = f"http://127.0.0.1:{port}/health"
                wait_for(lambda: updater.probe_health(url), timeout=30, message="station child /health")
                with urllib.request.urlopen(url, timeout=5) as response:
                    self.assertEqual(json.loads(response.read()), {
                        "status": "ok", "service": "terminal-local", "mode": "station", "server": "unlinked"})
                self.assertTrue(updater.probe_responding(url), "the launcher sees the station as running")
                self.assertFalse((data / "terminal.sqlite3").exists(), "a station never creates a database")
                stop_file.touch()
                self.assertEqual(child.wait(timeout=20), 0)
            finally:
                if child.poll() is None:
                    child.kill()
                    child.wait(timeout=10)
            self.assertIn("caixa adicional", (data / "logs" / "terminal.log").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
