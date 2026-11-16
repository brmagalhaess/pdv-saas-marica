"""Desktop shortcut: browser discovery, application window, single instance and messages."""

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from terminal import launcher, paths

WINDOWS_ENV = {"ProgramFiles(x86)": r"C:\Program Files (x86)", "ProgramFiles": r"C:\Program Files",
               "LOCALAPPDATA": r"C:\Users\Caixa\AppData\Local", "ProgramData": r"C:\ProgramData"}
EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"


class BrowserTests(unittest.TestCase):
    def test_windows_prefers_edge_then_chrome(self):
        find = lambda installed: launcher.find_browser("win32", WINDOWS_ENV, exists=lambda path: path in installed)
        self.assertEqual(find({EDGE, CHROME}), ("edge", EDGE))
        self.assertEqual(find({CHROME}), ("chrome", CHROME))
        per_user = r"C:\Users\Caixa\AppData\Local\Google\Chrome\Application\chrome.exe"
        self.assertEqual(find({per_user}), ("chrome", per_user))
        self.assertIsNone(find(set()))

    def test_macos_and_linux_discovery(self):
        mac_chrome = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
        self.assertEqual(launcher.find_browser("darwin", {}, exists=lambda path: path == mac_chrome),
                         ("chrome", mac_chrome))
        which = lambda name: "/usr/bin/chromium" if name == "chromium" else None
        self.assertEqual(launcher.find_browser("linux", {}, which=which), ("chrome", "/usr/bin/chromium"))
        self.assertIsNone(launcher.find_browser("linux", {}, which=lambda name: None))

    def test_command_opens_an_app_window_on_a_dedicated_profile(self):
        folder = launcher.profile_dir(WINDOWS_ENV, "win32")
        self.assertEqual(folder, r"C:\Users\Caixa\AppData\Local\PDV2\browser-profile")
        command = launcher.browser_command(EDGE, "http://127.0.0.1:8010/", folder)
        self.assertEqual(command[0], EDGE)
        self.assertIn("--app=http://127.0.0.1:8010/", command)
        self.assertIn("--user-data-dir=" + folder, command)
        self.assertIn("--no-first-run", command)

    def test_url_follows_the_configured_port(self):
        self.assertEqual(launcher.cashier_url({}), "http://127.0.0.1:8010/")
        self.assertEqual(launcher.cashier_url({"PDV_TERMINAL_PORT": "8020"}), "http://127.0.0.1:8020/")

    def test_open_cashier_falls_back_to_the_default_browser(self):
        with tempfile.TemporaryDirectory() as temp:
            env = {"HOME": temp}
            process = MagicMock()
            popen = MagicMock(return_value=process)
            found = lambda platform, env: ("edge", "/opt/edge")
            self.assertIs(launcher.open_cashier("http://x/", env, "linux", popen=popen, find=found), process)
            self.assertEqual(popen.call_args[0][0][:2], ["/opt/edge", "--app=http://x/"])
            self.assertTrue((Path(temp) / ".local/share/pdv2/browser-profile").is_dir())
            fallback = MagicMock(return_value=True)
            broken = MagicMock(side_effect=OSError("bloqueado"))
            self.assertIs(launcher.open_cashier("http://x/", env, "linux", popen=broken, find=found,
                                                fallback=fallback), True)
            fallback.assert_called_once_with("http://x/")
            nothing = lambda platform, env: None
            self.assertIs(launcher.open_cashier("http://x/", env, "linux", find=nothing, fallback=fallback), True)
            self.assertIsNone(launcher.open_cashier("http://x/", env, "linux", find=nothing,
                                                    fallback=lambda url: False))


class SingleInstanceTests(unittest.TestCase):
    def test_second_launcher_is_refused_until_the_first_ends(self):
        with tempfile.TemporaryDirectory() as temp:
            lock_path = Path(temp) / "sub" / "launcher.lock"
            first = launcher.acquire_single_instance(lock_path)
            self.assertIsNotNone(first)
            self.assertIsNone(launcher.acquire_single_instance(lock_path))
            code = ("import sys; from terminal import launcher; "
                    "sys.exit(0 if launcher.acquire_single_instance(sys.argv[1]) is None else 1)")
            other = subprocess.run([sys.executable, "-c", code, str(lock_path)],
                                   cwd=str(Path(__file__).resolve().parents[2]))
            self.assertEqual(other.returncode, 0, "another process must not get the lock")
            first.close()
            again = launcher.acquire_single_instance(lock_path)
            self.assertIsNotNone(again)
            again.close()


class ServiceTests(unittest.TestCase):
    def test_healthy_service_is_not_started_again(self):
        start = MagicMock()
        self.assertTrue(launcher.ensure_running(lambda: True, start))
        start.assert_not_called()

    def test_service_is_started_once_and_awaited(self):
        answers = iter([False, False, False, True])
        start, slept = MagicMock(), []
        self.assertTrue(launcher.ensure_running(lambda: next(answers), start, sleep=slept.append))
        start.assert_called_once_with()
        self.assertEqual(len(slept), 2)

    def test_gives_up_after_the_deadline_even_if_start_raises(self):
        ticks = iter(range(0, 1000, 10))
        start = MagicMock(side_effect=OSError("sc não encontrado"))
        self.assertFalse(launcher.ensure_running(lambda: False, start, timeout=40, sleep=lambda s: None,
                                                 clock=lambda: next(ticks)))

    def test_windows_starts_the_service_by_name(self):
        run = MagicMock(return_value=MagicMock(returncode=0))
        self.assertTrue(launcher.start_service("win32", run=run))
        self.assertEqual(run.call_args[0][0], ["sc", "start", "PDV2Service"])
        run.return_value.returncode = 1056  # already running
        self.assertTrue(launcher.start_service("win32", run=run))
        run.return_value.returncode = 1060  # not installed
        self.assertFalse(launcher.start_service("win32", run=run))

    def test_other_systems_start_the_terminal_process(self):
        popen = MagicMock()
        self.assertTrue(launcher.start_service("darwin", popen=popen))
        self.assertEqual(popen.call_args[0][0], [sys.executable, "-m", "terminal"])
        self.assertTrue((Path(popen.call_args[1]["cwd"]) / "terminal" / "launcher.py").is_file())


class MessageTests(unittest.TestCase):
    def test_messages_are_clear_and_in_portuguese(self):
        text = launcher.not_running_message(WINDOWS_ENV, "win32")
        self.assertIn("não conseguiu iniciar", text)
        self.assertIn("127.0.0.1:8010", text)
        self.assertIn(r"C:\ProgramData\PDV2\logs", text)
        self.assertIn("http://127.0.0.1:8010/", launcher.no_browser_message("http://127.0.0.1:8010/"))

    def test_message_without_a_desktop_goes_to_stderr(self):
        with patch("builtins.print") as printed:
            launcher.show_message("Olá", platform="linux")
        self.assertEqual(printed.call_args[0][0], "Olá")


class DelegationTests(unittest.TestCase):
    def test_stable_launcher_hands_over_to_the_current_version(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "bin").mkdir()
            (root / "app-2.1.0").mkdir()
            (root / "app-2.1.0" / "PDV2.exe").write_bytes(b"MZ")
            stable = str(root / "bin" / "PDV2.exe")
            self.assertIsNone(launcher.delegate_target({}, stable, frozen=True), "no pointer yet")
            paths.write_pointer(root, "2.1.0")
            self.assertEqual(launcher.delegate_target({}, stable, frozen=True),
                             str(root / "app-2.1.0" / "PDV2.exe"))
            self.assertIsNone(launcher.delegate_target({}, stable, frozen=False))
            self.assertIsNone(launcher.delegate_target({"PDV_LAUNCHER_DELEGATED": "1"}, stable, frozen=True))
            versioned = str(root / "app-2.1.0" / "PDV2.exe")
            self.assertIsNone(launcher.delegate_target({}, versioned, frozen=True), "no loop")
            paths.write_pointer(root, "2.2.0")
            self.assertIsNone(launcher.delegate_target({}, stable, frozen=True), "missing version: run in place")


class MainTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.env = {"HOME": self.temp.name, "LOCALAPPDATA": self.temp.name}

    def test_opens_the_cashier_and_waits_for_the_window(self):
        process = MagicMock(pid=4321)
        with patch.object(launcher.updater, "probe_health", return_value=True), \
                patch.object(launcher, "open_cashier", return_value=process) as opened, \
                patch.object(launcher, "show_message") as message:
            self.assertEqual(launcher.main([], self.env), 0)
        opened.assert_called_once_with("http://127.0.0.1:8010/", self.env)
        process.wait.assert_called_once_with()
        message.assert_not_called()
        lock = launcher.acquire_single_instance(Path(paths.user_data_dir(self.env)) / "launcher.lock")
        self.assertIsNotNone(lock, "the lock is released at the end")
        lock.close()

    def test_second_instance_exits_without_opening_anything(self):
        held = launcher.acquire_single_instance(Path(paths.user_data_dir(self.env)) / "launcher.lock")
        self.addCleanup(held.close)
        with patch.object(launcher.updater, "probe_health") as probe, \
                patch.object(launcher, "open_cashier") as opened:
            self.assertEqual(launcher.main([], self.env), 0)
        probe.assert_not_called()
        opened.assert_not_called()

    def test_service_that_never_answers_shows_a_message(self):
        with patch.object(launcher, "ensure_running", return_value=False), \
                patch.object(launcher, "open_cashier") as opened, \
                patch.object(launcher, "show_message") as message:
            self.assertEqual(launcher.main([], self.env), 1)
        opened.assert_not_called()
        self.assertIn("não conseguiu iniciar", message.call_args[0][0])

    def test_no_browser_at_all_shows_the_address(self):
        with patch.object(launcher, "ensure_running", return_value=True), \
                patch.object(launcher, "open_cashier", return_value=None), \
                patch.object(launcher, "show_message") as message:
            self.assertEqual(launcher.main([], self.env), 1)
        self.assertIn("http://127.0.0.1:8010/", message.call_args[0][0])


if __name__ == "__main__":
    unittest.main()


class DamagedDatabaseProbeTests(unittest.TestCase):
    """The shortcut must open the recovery screen when the terminal started in damaged-database mode."""

    def _serve(self, status, body):
        import http.server
        import json
        import threading

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)
        return f"http://127.0.0.1:{server.server_port}/health"

    def test_damaged_mode_counts_as_responding_but_not_as_healthy(self):
        from terminal import updater
        url = self._serve(503, {"status": "degraded", "service": "terminal-local", "database": "damaged"})
        self.assertTrue(updater.probe_responding(url))
        self.assertFalse(updater.probe_health(url))  # the updater still rolls back on this

    def test_other_services_and_dead_ports_do_not_count(self):
        from terminal import updater
        self.assertFalse(updater.probe_responding(self._serve(200, {"status": "ok", "service": "outro"})))
        self.assertFalse(updater.probe_responding("http://127.0.0.1:9/health", timeout=0.5))
