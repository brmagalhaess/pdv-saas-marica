"""What this process is: the shop server (caixa principal) or a station (caixa adicional).

`create()` reads <data_dir>/mode.json and builds the matching WSGI app with its
background work. The callers (`python -m terminal` and the Windows service child)
only serve `runtime.app` on 127.0.0.1.
"""

import json
import logging
import os
import sys
import threading
import time
from pathlib import Path

from . import paths

RESTART_REQUEST = "restart.request"  # the same file the updater leaves for the Supervisor
log = logging.getLogger("pdv2.runtime")
_current = {"data_dir": None}


class Runtime:
    def __init__(self, mode, app, data_dir, threads=(), stoppers=()):
        self.mode = mode
        self.app = app
        self.data_dir = Path(data_dir)
        self.threads = list(threads)
        self._stoppers = list(stoppers)

    def stop(self):
        """Stops the background work. Never raises."""
        stoppers, self._stoppers = self._stoppers, []
        for stopper in stoppers:
            try:
                stopper()
            except Exception as exc:
                log.error("Falha ao parar o trabalho em segundo plano: %s", type(exc).__name__)


def resolve_data_dir(env=None):
    """Same folder `terminal.app` uses when nothing is configured."""
    env = os.environ if env is None else env
    if env.get("PDV_TERMINAL_DATA"):
        return Path(env["PDV_TERMINAL_DATA"])
    if paths.is_windows():
        return Path(paths.installed_data_dir(env))
    return Path(paths.user_data_dir(env))


def _promotion_pending(data_dir):
    """A promotion that finished right before the mode was written: this is the server now."""
    try:
        from . import promotion
    except ImportError:
        return False
    try:
        return bool(promotion.pending(data_dir))
    except Exception as exc:
        log.error("Não foi possível conferir a promoção pendente: %s", type(exc).__name__)
        return False


def _create_server(data_dir, env):
    from . import lan
    from .app import create_app
    from .sync import watch_worker

    config = {"START_WORKER": True}
    if env.get("PDV_TERMINAL_DATA"):
        config["DATA_DIR"] = str(data_dir)
    app = create_app(config)
    # Sync, license renewal and printing live in one thread: revive it if it dies.
    watchdog = watch_worker(lambda: app.extensions.get("terminal_worker"))
    server = lan.start(app)

    def stop_worker():
        worker = app.extensions.get("terminal_worker")
        if worker is not None:
            worker.stop()

    return Runtime("server", app, data_dir, ["pdv-terminal-watchdog", "pdv-lan"],
                   [watchdog.set, server.stop, stop_worker])


def _create_station(data_dir):
    from . import station

    app = station.create_app({"DATA_DIR": str(data_dir), "START_BACKGROUND": True})
    return Runtime("station", app, data_dir, ["pdv-station-monitor", "pdv-station-print"],
                   [lambda: station.stop_background(app)])


def create(env=None):
    env = os.environ if env is None else env
    data_dir = resolve_data_dir(env)
    _current["data_dir"] = data_dir
    mode = paths.read_mode(data_dir, env)
    if mode == "station" and _promotion_pending(data_dir):
        try:
            paths.write_mode(data_dir, "server")
        except OSError as exc:
            log.error("Não foi possível gravar o modo servidor: %s", type(exc).__name__)
        mode = "server"
    return _create_station(data_dir) if mode == "station" else _create_server(data_dir, env)


def _reexec():
    """Development only: replace this process by a fresh one with the same command line."""
    arguments = list(getattr(sys, "orig_argv", None) or [sys.executable, *sys.argv])
    if getattr(sys, "frozen", False):
        arguments = [sys.executable, *sys.argv[1:]]
    os.execv(sys.executable, arguments)


def request_restart(reason, env=None):
    """Asks for this process to start again (mode change, promotion). Never raises.

    Under the service the Supervisor watches <data>/updates/restart.request and restarts
    the child. In development the process re-executes itself after half a second.
    """
    try:
        env = os.environ if env is None else env
        if env.get("PDV_SUPERVISED") == "1":
            updates = Path(env.get("PDV_TERMINAL_DATA") or _current["data_dir"] or resolve_data_dir(env)) / "updates"
            updates.mkdir(parents=True, exist_ok=True)
            temporary = updates / (RESTART_REQUEST + ".tmp")
            temporary.write_text(json.dumps({"reason": str(reason)[:40], "at": int(time.time())}), encoding="utf-8")
            os.replace(temporary, updates / RESTART_REQUEST)
            return True
        timer = threading.Timer(0.5, _restart_now)
        timer.daemon = True
        timer.start()
        return True
    except Exception as exc:
        log.error("Não foi possível pedir o reinício: %s", type(exc).__name__)
        return False


def _restart_now():
    try:
        _reexec()
    except Exception as exc:
        log.error("Reinício automático falhou; feche e abra o PDV2 de novo: %s", type(exc).__name__)
