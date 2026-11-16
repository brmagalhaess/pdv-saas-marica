"""Mode of this computer (server or station) and the process that goes with it."""

import json
import os
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import terminal
from terminal import paths, runtime, updater


class ModeFileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.data = Path(self.temp.name)

    def test_no_file_means_server(self):
        self.assertEqual(paths.mode_path(self.data), self.data / "mode.json")
        self.assertEqual(paths.read_mode(self.data, {}), "server")

    def test_write_and_read(self):
        paths.write_mode(self.data, "station")
        self.assertEqual(paths.read_mode(self.data, {}), "station")
        content = json.loads((self.data / "mode.json").read_text())
        self.assertEqual((content["v"], content["mode"]), (1, "station"))
        self.assertIsInstance(content["changed_at"], int)
        paths.write_mode(self.data, "server")
        self.assertEqual(paths.read_mode(self.data, {}), "server")
        self.assertEqual(sorted(item.name for item in self.data.iterdir()), ["mode.json"], "no temporary left")

    def test_environment_wins(self):
        paths.write_mode(self.data, "station")
        self.assertEqual(paths.read_mode(self.data, {"PDV_MODE": "server"}), "server")
        self.assertEqual(paths.read_mode(self.data, {"PDV_MODE": " Station "}), "station")
        self.assertEqual(paths.read_mode(self.data, {"PDV_MODE": "outro"}), "station", "nonsense is ignored")
        self.assertEqual(paths.read_mode(None, {"PDV_TERMINAL_DATA": str(self.data)}), "station")

    def test_unreadable_file_means_server(self):
        for content in ("", "{", "[]", '{"mode": "tablet"}', '{"v": 1}'):
            (self.data / "mode.json").write_text(content)
            self.assertEqual(paths.read_mode(self.data, {}), "server", content)
        (self.data / "mode.json").write_bytes(b"\xff\xfe")
        self.assertEqual(paths.read_mode(self.data, {}), "server")

    def test_write_is_atomic_and_refuses_unknown_modes(self):
        paths.write_mode(self.data, "station")
        with self.assertRaises(ValueError):
            paths.write_mode(self.data, "tablet")
        with patch.object(paths.os, "replace", side_effect=OSError("disco cheio")):
            with self.assertRaises(OSError):
                paths.write_mode(self.data, "server")
        self.assertEqual(paths.read_mode(self.data, {}), "station", "a failed write keeps the old mode")
        paths.write_mode(self.data / "nova" / "pasta", "station")
        self.assertEqual(paths.read_mode(self.data / "nova" / "pasta", {}), "station")


class CreateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.data = Path(self.temp.name)
        self.env = {"PDV_TERMINAL_DATA": self.temp.name}
        self.addCleanup(runtime._current.update, data_dir=None)

    def fake_server(self):
        worker = MagicMock()
        app = MagicMock()
        app.extensions = {"terminal_worker": worker}
        watchdog = threading.Event()
        lan_server = MagicMock()
        patches = (patch("terminal.app.create_app", return_value=app),
                   patch("terminal.sync.watch_worker", return_value=watchdog),
                   patch("terminal.lan.start", return_value=lan_server))
        return app, worker, watchdog, lan_server, patches

    def test_server_mode_builds_the_terminal_app_with_worker_and_lan(self):
        app, worker, watchdog, lan_server, patches = self.fake_server()
        with patches[0] as create_app, patches[1] as watch, patches[2] as lan_start:
            current = runtime.create(self.env)
            self.assertEqual((current.mode, current.app, current.data_dir), ("server", app, self.data))
            create_app.assert_called_once_with({"START_WORKER": True, "DATA_DIR": self.temp.name})
            self.assertIs(watch.call_args[0][0](), worker)
            lan_start.assert_called_once_with(app)
            self.assertIn("pdv-lan", current.threads)
            current.stop()
        self.assertTrue(watchdog.is_set())
        lan_server.stop.assert_called_once_with()
        worker.stop.assert_called_once_with()
        current.stop()  # twice is harmless
        worker.stop.assert_called_once_with()

    def test_station_mode_builds_the_bridge(self):
        paths.write_mode(self.data, "station")
        current = runtime.create(self.env)
        self.addCleanup(current.stop)
        self.assertEqual(current.mode, "station")
        health = current.app.test_client().get("/health")
        self.assertEqual(health.json, {"status": "ok", "service": "terminal-local", "mode": "station",
                                       "server": "unlinked"})
        self.assertFalse((self.data / "terminal.sqlite3").exists(), "a station never creates a database")
        self.assertIn("pdv-station-monitor", [thread.name for thread in threading.enumerate()])
        current.stop()
        self.assertNotIn("pdv-station-monitor", [thread.name for thread in threading.enumerate()])

    def test_environment_can_force_the_station(self):
        current = runtime.create({**self.env, "PDV_MODE": "station"})
        self.addCleanup(current.stop)
        self.assertEqual(current.mode, "station")
        self.assertFalse((self.data / "mode.json").exists())

    def test_finished_promotion_becomes_server_on_start(self):
        paths.write_mode(self.data, "station")
        asked = []
        fake = types.ModuleType("terminal.promotion")
        fake.pending = lambda data_dir: asked.append(Path(data_dir)) or True
        app, _worker, _watchdog, _lan_server, patches = self.fake_server()
        with patch.dict(sys.modules, {"terminal.promotion": fake}), \
                patch.object(terminal, "promotion", fake, create=True), patches[0], patches[1], patches[2]:
            current = runtime.create(self.env)
        self.addCleanup(current.stop)
        self.assertEqual((current.mode, current.app), ("server", app))
        self.assertEqual(asked, [self.data])
        self.assertEqual(paths.read_mode(self.data, {}), "server")

    def test_no_pending_promotion_stays_station(self):
        paths.write_mode(self.data, "station")
        fake = types.ModuleType("terminal.promotion")
        fake.pending = MagicMock(side_effect=[False, RuntimeError("cópia ilegível")])
        with patch.dict(sys.modules, {"terminal.promotion": fake}), \
                patch.object(terminal, "promotion", fake, create=True):
            for _attempt in range(2):
                current = runtime.create(self.env)
                self.addCleanup(current.stop)
                current.stop()
                self.assertEqual(current.mode, "station")
        self.assertEqual(paths.read_mode(self.data, {}), "station")

    def test_default_data_dir_matches_the_terminal_app(self):
        from terminal.app import default_data_dir

        self.assertEqual(runtime.resolve_data_dir({"PDV_TERMINAL_DATA": "/x/y"}), Path("/x/y"))
        self.assertEqual(runtime.resolve_data_dir({}), default_data_dir(environ={}))


class RestartTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.data = Path(self.temp.name)

    def test_supervised_writes_the_file_the_service_watches(self):
        self.assertEqual(runtime.RESTART_REQUEST, updater.RESTART_REQUEST)
        with patch.object(runtime, "_reexec") as reexec:
            self.assertTrue(runtime.request_restart("promocao", {"PDV_SUPERVISED": "1",
                                                                 "PDV_TERMINAL_DATA": self.temp.name}))
            reexec.assert_not_called()
        request = self.data / "updates" / "restart.request"
        self.assertEqual(json.loads(request.read_text())["reason"], "promocao")
        self.assertEqual([item.name for item in (self.data / "updates").iterdir()], ["restart.request"])

    def test_supervisor_restarts_the_child_on_that_file(self):
        from terminal import service_win

        config = {"install_root": self.temp.name, "data_dir": self.temp.name, "port": 1, "health_timeout": 1}
        supervisor = service_win.Supervisor(config, probe=lambda: True)
        runtime.request_restart("modo", {"PDV_SUPERVISED": "1", "PDV_TERMINAL_DATA": self.temp.name})
        self.assertTrue(supervisor.restart_file.exists())

    def test_development_reexecutes_after_a_moment(self):
        done = threading.Event()
        with patch.object(runtime, "_reexec", side_effect=done.set) as reexec:
            self.assertTrue(runtime.request_restart("modo", {"PDV_TERMINAL_DATA": self.temp.name}))
            reexec.assert_not_called()
            self.assertTrue(done.wait(3))
        self.assertFalse((self.data / "updates").exists())

    def test_never_raises(self):
        blocked = self.data / "arquivo"
        blocked.write_text("x")
        self.assertFalse(runtime.request_restart("x", {"PDV_SUPERVISED": "1", "PDV_TERMINAL_DATA": str(blocked)}))
        with patch.object(runtime, "_reexec", side_effect=OSError("sem permissão")):
            runtime._restart_now()


class EntryPointTests(unittest.TestCase):
    def test_main_serves_the_runtime_app_on_loopback(self):
        from terminal import __main__ as entry

        current = MagicMock(mode="station")
        with patch.object(entry.runtime, "create", return_value=current), patch.object(entry, "serve") as serve, \
                patch.dict(os.environ, {"PDV_TERMINAL_PORT": "8010"}), patch("builtins.print"):
            os.environ.pop("PDV_TERMINAL_HOST", None)
            entry.main()
        serve.assert_called_once_with(current.app, host="127.0.0.1", port=8010, threads=6)


if __name__ == "__main__":
    unittest.main()
