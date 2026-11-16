"""Windows service "PDV2Service": a stable host that runs the version in current.txt.

The host lives in ``<install>/bin`` and is only replaced by the full installer.
It starts ``<install>/app-<version>/PDV2Server.exe`` as a child, restarts it when
it dies or when an update asks for it, and confirms ``/health`` after a version
swap (rolling the pointer back on failure). Everything except the thin pywin32
wrapper at the end runs on any system and is covered by tests.
"""

import json
import logging
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from contextlib import closing
from logging.handlers import RotatingFileHandler
from pathlib import Path

from . import paths, updater

SERVICE_NAME = "PDV2Service"
DISPLAY_NAME = "PDV2 - Caixa local"
DESCRIPTION = ("Mantém o caixa PDV2 funcionando neste computador, com ou sem internet. "
               "Se for parado, o PDV2 deixa de abrir.")
SERVER_EXE = "PDV2Server.exe"
STOP_GRACE_SECONDS = 15
# Interactive users may start and query the service (the desktop shortcut needs it).
SERVICE_SDDL = ("D:(A;;CCLCSWRPWPDTLOCRRC;;;SY)(A;;CCDCLCSWRPWPDTLOCRSDRCWDWO;;;BA)"
                "(A;;CCLCSWRPLOCRRC;;;IU)(A;;CCLCSWLOCRRC;;;SU)")

try:  # pywin32 exists only on Windows
    import servicemanager
    import win32service
    import win32serviceutil
except ImportError:
    servicemanager = win32service = win32serviceutil = None


# ------------------------------------------------------------ configuration

def service_config(env=None, install_root=None, platform=None):
    """Everything the service needs, derived only from the environment."""
    env = os.environ if env is None else env
    root = install_root or paths.install_root(env, platform=platform) or paths.default_install_root(env, platform)
    data = paths.data_dir(env, platform, installed=True)
    join = paths._flavor(platform).join
    return {
        "name": SERVICE_NAME,
        "display_name": DISPLAY_NAME,
        "description": DESCRIPTION,
        "install_root": str(root),
        "data_dir": data,
        "log_dir": join(data, "logs"),
        "service_log": join(data, "logs", "service.log"),
        "server_log": join(data, "logs", "terminal.log"),
        "host": "127.0.0.1",
        "port": paths.terminal_port(env),
        "start": "auto",
        # Restart after 5 s, 5 s and then every 30 s; the counter resets after a day.
        "failure_reset_seconds": 86400,
        "failure_actions": "restart/5000/restart/5000/restart/30000",
        "health_timeout": updater.HEALTH_TIMEOUT,
    }


def install_commands(config, executable):
    """sc.exe calls that register the service. The quoted path avoids path hijacking."""
    name = config["name"]
    return [
        ["sc", "create", name, "binPath=", f'"{executable}"', "start=", config["start"],
         "DisplayName=", config["display_name"]],
        ["sc", "description", name, config["description"]],
        ["sc", "failure", name, "reset=", str(config["failure_reset_seconds"]),
         "actions=", config["failure_actions"]],
        ["sc", "failureflag", name, "1"],
        ["sc", "sdset", name, SERVICE_SDDL],
        ["sc", "start", name],
    ]


def uninstall_commands(config):
    # "net stop" waits for the service to finish; "sc stop" would return at once.
    return [["net", "stop", config["name"]], ["sc", "delete", config["name"]]]


def child_command(install_root, version, python=None):
    """(argv, cwd) for the terminal server of one installed version."""
    folder = paths.app_dir(install_root, version)
    frozen = folder / SERVER_EXE
    if frozen.exists():
        return [str(frozen)], str(folder)
    return [python or sys.executable, "-m", "terminal.service_win", "--serve"], str(folder)


def child_env(config, version, stop_file, base=None):
    env = dict(os.environ if base is None else base)
    env.update({
        "PDV_TERMINAL_DATA": str(config["data_dir"]),
        "PDV_INSTALL_ROOT": str(config["install_root"]),
        "PDV_TERMINAL_HOST": config["host"],
        "PDV_TERMINAL_PORT": str(config["port"]),
        "PDV_VERSION": version,
        "PDV_SUPERVISED": "1",
        "PDV_STOP_FILE": str(stop_file),
        "PDV_SUPERVISOR_PID": str(os.getpid()),
    })
    env.pop("PDV_TERMINAL_DB", None)
    return env


def setup_logging(log_file, name):
    """Rotating file log: 5 files of 1 MB. Safe to call more than once."""
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    log_file = Path(log_file)
    if any(getattr(handler, "baseFilename", None) == str(log_file) for handler in logger.handlers):
        return logger
    log_file.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(log_file, maxBytes=1024 * 1024, backupCount=5, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logger.addHandler(handler)
    return logger


# --------------------------------------------------------------- supervisor

class Supervisor:
    """Keeps one terminal server process alive for the version in current.txt."""

    def __init__(self, config, *, command=child_command, probe=None, logger=None,
                 poll=1.0, health_timeout=None, base_env=None, watchdog_seconds=180):
        self.config = config
        self.install_root = Path(config["install_root"])
        self.data_dir = Path(config["data_dir"])
        self.command = command
        self.probe = probe or (lambda: updater.probe_health(
            f"http://127.0.0.1:{config['port']}/health"))
        self.log = logger or logging.getLogger("pdv2.service")
        self.poll = poll
        self.health_timeout = config["health_timeout"] if health_timeout is None else health_timeout
        self.base_env = base_env
        self.watchdog_seconds = watchdog_seconds
        self.stop_event = threading.Event()
        self.child = None
        self.stop_file = None
        self.launches = 0
        self._job = None

    def stop(self):
        self.stop_event.set()

    @property
    def restart_file(self):
        return self.data_dir / "updates" / updater.RESTART_REQUEST

    def _start_child(self, version):
        run_dir = self.data_dir / "run"
        run_dir.mkdir(parents=True, exist_ok=True)
        for stale in run_dir.glob("stop-*"):
            stale.unlink(missing_ok=True)
        self.launches += 1
        self.stop_file = run_dir / f"stop-{os.getpid()}-{self.launches}"
        argv, cwd = self.command(self.install_root, version)
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        self.child = subprocess.Popen(argv, cwd=cwd, env=child_env(self.config, version, self.stop_file,
                                                                  self.base_env),
                                      stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                      stderr=subprocess.DEVNULL, creationflags=flags)
        self._bind_to_job(self.child)
        self.log.info("Versão %s iniciada (pid %s)", version, self.child.pid)

    def _bind_to_job(self, child):
        """Windows only: the child dies with the host, so the port is never orphaned."""
        if os.name != "nt":
            return
        try:
            import win32api
            import win32con
            import win32job
            if self._job is None:
                self._job = win32job.CreateJobObject(None, "")
                info = win32job.QueryInformationJobObject(self._job, win32job.JobObjectExtendedLimitInformation)
                info["BasicLimitInformation"]["LimitFlags"] |= win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
                win32job.SetInformationJobObject(self._job, win32job.JobObjectExtendedLimitInformation, info)
            handle = win32api.OpenProcess(win32con.PROCESS_SET_QUOTA | win32con.PROCESS_TERMINATE,
                                          False, child.pid)
            win32job.AssignProcessToJobObject(self._job, handle)
        except Exception as exc:
            self.log.warning("Não foi possível vincular o processo ao serviço: %s", exc)

    def _stop_child(self):
        child, self.child = self.child, None
        if child is None:
            return
        if child.poll() is None:
            try:
                self.stop_file.touch()
                child.wait(timeout=STOP_GRACE_SECONDS)
            except (OSError, subprocess.TimeoutExpired):
                child.terminate()
                try:
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=5)
        if self.stop_file is not None:
            self.stop_file.unlink(missing_ok=True)

    def _alive(self):
        return self.child is not None and self.child.poll() is None

    def _wait(self, seconds):
        return self.stop_event.wait(seconds)

    def _watch(self):
        """Wait while the child lives. True when it stopped answering /health for too long."""
        last_ok = last_probe = time.monotonic()
        interval = min(30.0, self.watchdog_seconds / 3)
        while self._alive() and not self.stop_event.is_set() and not self.restart_file.exists():
            self._wait(self.poll)
            now = time.monotonic()
            if now - last_probe < interval:
                continue
            last_probe = now
            if self.probe():
                last_ok = now
            elif now - last_ok > self.watchdog_seconds:
                return True
        return False

    def run(self):
        """Blocks until stop(). Never raises for a failing child."""
        failures = 0
        self.log.info("Serviço iniciado; instalação em %s", self.install_root)
        while not self.stop_event.is_set():
            version = paths.read_pointer(self.install_root)
            if version is None or not paths.app_dir(self.install_root, version).is_dir():
                self.log.error("current.txt ausente ou apontando para uma versão não instalada")
                if updater.pending_apply(self.install_root, self.data_dir):
                    updater.rollback(self.install_root, self.data_dir)
                    continue
                self._wait(30)
                continue
            self.restart_file.unlink(missing_ok=True)
            started = time.monotonic()
            try:
                self._start_child(version)
            except OSError as exc:
                self.log.error("Não foi possível iniciar a versão %s: %s", version, exc)
                self.child = None
            if updater.pending_apply(self.install_root, self.data_dir):
                healthy = self.child is not None and updater.await_health(
                    self.probe, self.health_timeout, interval=self.poll,
                    alive=lambda: self._alive() and not self.stop_event.is_set(), sleep=self._wait)
                if self.stop_event.is_set():
                    break  # confirmation is repeated on the next start
                if healthy:
                    updater.confirm_apply(self.install_root, self.data_dir)
                    self.log.info("Atualização para a versão %s confirmada", version)
                else:
                    self._stop_child()
                    previous = updater.rollback(self.install_root, self.data_dir)
                    self.log.error("Versão %s não respondeu; voltando para %s", version, previous)
                    continue
            hung = self._watch()
            if self.stop_event.is_set():
                break
            if self.restart_file.exists():
                self.log.info("Reinício pedido pela atualização")
                self._stop_child()
                failures = 0
                continue
            if hung:
                self.log.error("O terminal está no ar mas não responde; reiniciando")
                self._stop_child()
                continue
            code = self.child.returncode if self.child is not None else None
            self.child = None
            failures = 0 if time.monotonic() - started > 60 else min(failures + 1, 5)
            delay = min(30, 2 ** failures)
            self.log.error("O terminal parou (código %s); novo início em %s s", code, delay)
            self._wait(delay)
        self._stop_child()
        self.log.info("Serviço parado")


# ------------------------------------------------------------ server (child)

def run_server(env=None):
    """Entry point of PDV2Server.exe: Waitress plus a clean stop on request."""
    env = os.environ if env is None else env
    from waitress.server import create_server

    from .app import create_app
    from .sync import watch_worker

    installed = paths.read_pointer(paths.install_root(env)) is not None
    data = Path(paths.data_dir(env, installed=installed))
    if installed and not env.get("PDV_TERMINAL_DATA"):
        # Started by hand on an installed machine: still use the machine data folder.
        os.environ["PDV_TERMINAL_DATA"] = str(data)
    log = setup_logging(data / "logs" / "terminal.log", "pdv2.terminal")
    setup_logging(data / "logs" / "terminal.log", "waitress")
    host = env.get("PDV_TERMINAL_HOST", "127.0.0.1")
    port = paths.terminal_port(env)
    try:
        app = create_app({"START_WORKER": True})
        watch_worker(lambda: app.extensions.get("terminal_worker"))
        server = create_server(app, host=host, port=port, threads=6)
    except Exception:
        # No traceback dialog: a windowed program in the service session would hang on it.
        log.exception("O terminal não conseguiu iniciar")
        return 1
    stop_file = env.get("PDV_STOP_FILE")
    parent = os.getppid()
    stopping = threading.Event()

    def watch():
        while True:
            time.sleep(0.5)
            orphaned = os.name != "nt" and env.get("PDV_SUPERVISED") == "1" and os.getppid() != parent
            if (stop_file and os.path.exists(stop_file)) or orphaned:
                break
        log.info("Parada solicitada")
        stopping.set()
        try:
            worker = app.extensions.get("terminal_worker")
            if worker is not None:
                worker.stop()
            server.close()
        finally:
            # Idle keep-alive connections would hold the loop open; data is already durable.
            time.sleep(2)
            os._exit(0)

    if stop_file or env.get("PDV_SUPERVISED") == "1":
        threading.Thread(target=watch, name="pdv-stop-watch", daemon=True).start()
    log.info("Terminal PDV2 %s em http://%s:%s", paths.source_version(env), host, port)
    try:
        server.run()
    except Exception:
        if not stopping.is_set():  # closing the listener interrupts the loop on purpose
            log.exception("O terminal parou por erro")
            return 1
    return 0


# ---------------------------------------------------- legacy data migration

def _copy_database(source, target):
    """Consistent copy of a SQLite file that may still have a WAL beside it."""
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = target.with_name(target.name + ".migrating")
    staging.unlink(missing_ok=True)
    try:
        with closing(sqlite3.connect(source, timeout=10)) as origin, closing(sqlite3.connect(staging)) as copy:
            origin.backup(copy)
            if copy.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RuntimeError("cópia do banco não passou na verificação")
        os.replace(staging, target)
    finally:
        staging.unlink(missing_ok=True)


def legacy_candidates(local_appdata):
    """Where the development installer and the old default kept the data."""
    base = Path(local_appdata) / paths.APP_NAME
    return [base / "data", base]


# The development installer ran "<LOCALAPPDATA>\PDV2\.venv\Scripts\python.exe -m terminal"
# from the Startup folder. installer/pdv2.iss carries the same marker.
LEGACY_PROCESS_MARKER = "\\AppData\\Local\\PDV2\\.venv\\"
LEGACY_RUNNING_EXIT = 3
LEGACY_RUNNING_REASON = ("o PDV2 antigo ainda está aberto neste computador; "
                         "feche-o e execute o instalador de novo")


def is_legacy_process(name, executable="", command_line=""):
    """True for the Python process of the old per-user install (and only for it)."""
    if not str(name or "").lower().startswith("python"):
        return False
    marker = LEGACY_PROCESS_MARKER.lower()
    return any(marker in str(text or "").lower().replace("/", "\\") for text in (executable, command_line))


def legacy_stop_command(system_root=None):
    """PowerShell that ends the old terminal, applying is_legacy_process to every process."""
    pattern = f"*{LEGACY_PROCESS_MARKER}*"
    script = ("Get-CimInstance Win32_Process | Where-Object { $_.ProcessId -ne $PID -and $_.Name -like 'python*' "
              f"-and (($_.ExecutablePath -like '{pattern}') -or ($_.CommandLine -like '{pattern}')) }} | "
              "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }")
    root = system_root or os.environ.get("SystemRoot") or r"C:\Windows"
    return [root + r"\System32\WindowsPowerShell\v1.0\powershell.exe",
            "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script]


def stop_legacy_processes(platform=None, run=subprocess.run):
    if not paths.is_windows(platform):
        return False
    try:
        run(legacy_stop_command(), capture_output=True, timeout=60,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError):
        return False
    return True


def port_in_use(port, host="127.0.0.1", timeout=1.0):
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except OSError:
        return False


def legacy_migration_blocker(busy, attempts=10, interval=0.5, sleep=time.sleep):
    """Why the old data cannot be copied now, or None when it is safe.

    While something still answers on the terminal port the old terminal is alive:
    it keeps writing to the old database and the new service could not listen.
    """
    for attempt in range(max(1, attempts)):
        if not busy():
            return None
        if attempt + 1 < attempts:
            sleep(interval)
    return LEGACY_RUNNING_REASON


def migrate_legacy_data(source, target):
    """Copy an old per-user data folder to the machine folder. Never overwrites."""
    source, target = Path(source), Path(target)
    if not (source / "terminal.sqlite3").is_file():
        return {"migrated": False, "reason": "nenhum banco antigo encontrado"}
    if (target / "terminal.sqlite3").exists():
        return {"migrated": False, "reason": "já existe um banco no destino"}
    target.mkdir(parents=True, exist_ok=True)
    copied = []
    for database in [source / "terminal.sqlite3", *sorted(source.glob("profiles/*/terminal.sqlite3"))]:
        relative = database.relative_to(source)
        _copy_database(database, target / relative)
        copied.append(str(relative))
    for name in ("session.key", "active-profile.json"):
        if (source / name).is_file():
            shutil.copy2(source / name, target / name)
            copied.append(name)
    for backups in [source / "backups", *sorted(source.glob("profiles/*/backups"))]:
        if backups.is_dir():
            shutil.copytree(backups, target / backups.relative_to(source), dirs_exist_ok=True)
    (target / "migrated-from.txt").write_text(str(source) + "\n", encoding="utf-8")
    return {"migrated": True, "source": str(source), "files": copied}


# -------------------------------------------------------------- entry point

if win32serviceutil is not None:
    class PDV2Service(win32serviceutil.ServiceFramework):
        _svc_name_ = SERVICE_NAME
        _svc_display_name_ = DISPLAY_NAME
        _svc_description_ = DESCRIPTION

        def __init__(self, args):
            super().__init__(args)
            self.config = service_config()
            self.supervisor = Supervisor(self.config,
                                         logger=setup_logging(self.config["service_log"], "pdv2.service"))

        def SvcStop(self):
            self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING, waitHint=(STOP_GRACE_SECONDS + 15) * 1000)
            self.supervisor.stop()

        def SvcDoRun(self):
            try:
                self.supervisor.run()
            except Exception:
                self.supervisor.log.exception("Falha interna do serviço")
                # Exit without reporting "stopped" so Windows applies the restart policy.
                os._exit(1)


def _run_commands(commands, log, tolerate=()):
    for command in commands:
        result = subprocess.run(command, capture_output=True, text=True, errors="replace",
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        log.info("%s -> %s %s", " ".join(command[:3]), result.returncode, (result.stdout or "").strip()[-200:])
        if result.returncode and command[1] not in tolerate:
            return result.returncode
    return 0


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["--serve"]:
        return run_server()
    config = service_config()
    log = setup_logging(config["service_log"], "pdv2.service")
    if argv[:1] == ["--migrate-legacy"]:
        stop_legacy_processes()
        blocker = legacy_migration_blocker(lambda: port_in_use(config["port"]))
        if blocker:
            log.error("Migração de dados antigos cancelada: %s", blocker)
            return LEGACY_RUNNING_EXIT
        for source in argv[1:]:
            try:
                result = migrate_legacy_data(source, config["data_dir"])
            except Exception as exc:
                result = {"migrated": False, "source": source, "reason": f"{type(exc).__name__}: {exc}"}
            log.info("Migração de dados antigos: %s", json.dumps(result, ensure_ascii=False))
            if result["migrated"]:
                break
        return 0
    if argv[:1] == ["--install"]:
        executable = argv[1] if len(argv) > 1 else sys.executable
        _run_commands(uninstall_commands(config)[:1], log, tolerate=("stop",))
        commands = install_commands(config, executable)
        if _run_commands(commands[:1], log):
            # Already registered (reinstall or upgrade): repair it in place.
            if _run_commands([["sc", "config", *commands[0][2:]]], log):
                return 1
        return _run_commands(commands[1:], log, tolerate=("failureflag", "sdset"))
    if argv[:1] == ["--uninstall"]:
        return _run_commands(uninstall_commands(config), log, tolerate=("stop",))
    if argv[:1] == ["--rollback"]:
        previous = updater.rollback(config["install_root"], config["data_dir"], "Volta pedida pelo suporte.")
        print(previous or "sem versão anterior")
        return 0 if previous else 1
    if argv[:1] == ["--run"]:
        supervisor = Supervisor(config, logger=log)
        try:
            supervisor.run()
        except KeyboardInterrupt:
            supervisor.stop()
            supervisor._stop_child()
        return 0
    if win32serviceutil is None:
        print("O serviço PDV2Service só existe no Windows. Use --run para testar o supervisor.")
        return 2
    if not argv:
        servicemanager.Initialize()
        servicemanager.PrepareToHostSingle(PDV2Service)
        servicemanager.StartServiceCtrlDispatcher()
        return 0
    win32serviceutil.HandleCommandLine(PDV2Service, argv=[sys.argv[0], *argv])
    return 0


if __name__ == "__main__":
    sys.exit(main())
