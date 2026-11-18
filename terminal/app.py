"""Browser UI and local JSON API. Browser never calls the cloud directly."""

import json
import os
import secrets
import hashlib
import hmac
import shutil
import sqlite3
import time
from contextlib import closing
from functools import wraps
from pathlib import Path
from urllib.parse import urlparse

from flask import Flask, jsonify, render_template, request, session

from . import paths, sync
from .printers import cups_queues
from .store import MANAGER_ROLES, SELL_ROLES, SERVER_ONLY_MESSAGE, SERVER_STATION_ID, Store, TerminalError


MIGRATION_MARKER = "migrated-from.json"
# SQLite failures that mean "try again in a moment", not a bug in a statement.
DATABASE_UNAVAILABLE_HINTS = ("locked", "busy", "unable to open", "disk", "readonly", "read-only",
                              "malformed", "not a database", "full")


def default_data_dir(os_name=None, environ=None, home=None):
    """Windows keeps data in %ProgramData%\\PDV2: the service runs as the system
    account and does not see a user's %LOCALAPPDATA%."""
    os_name = os.name if os_name is None else os_name
    environ = os.environ if environ is None else environ
    if os_name == "nt":
        return Path(environ.get("ProgramData") or environ.get("PROGRAMDATA") or "C:\\ProgramData") / "PDV2"
    return Path(home or Path.home()) / ".local" / "share" / "pdv2"


def legacy_data_dir(os_name=None, environ=None):
    """Where Windows builds kept the data before; PDV_TERMINAL_LEGACY_DATA lets the installer point at it."""
    os_name = os.name if os_name is None else os_name
    environ = os.environ if environ is None else environ
    if os_name != "nt":
        return None
    if environ.get("PDV_TERMINAL_LEGACY_DATA"):
        return Path(environ["PDV_TERMINAL_LEGACY_DATA"])
    return Path(environ["LOCALAPPDATA"]) / "PDV2" if environ.get("LOCALAPPDATA") else None


def _copy_database(source, destination):
    """Transaction-consistent, verified copy of one SQLite file."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(source, timeout=10)) as origin, closing(sqlite3.connect(destination)) as target:
        origin.backup(target)
        if target.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise sqlite3.DatabaseError("cópia não passou na verificação")
        target.commit()
    with destination.open("rb+") as copied:
        os.fsync(copied.fileno())


def _move_into(source, target):
    if source.is_dir():
        target.mkdir(exist_ok=True)
        for child in source.iterdir():
            _move_into(child, target / child.name)
    else:
        os.replace(source, target)


def migrate_legacy_data(legacy_dir, data_dir):
    """One-time copy of the old per-user data into the new directory.

    The origin is never changed. Returns the directory to use: `data_dir`, or
    `legacy_dir` when the copy failed (the terminal keeps working where the data
    is and tries again on the next start).
    """
    legacy_dir, data_dir = Path(legacy_dir), Path(data_dir)
    database = legacy_dir / "terminal.sqlite3"
    if (legacy_dir == data_dir or not database.exists() or (data_dir / "terminal.sqlite3").exists()
            or (data_dir / MIGRATION_MARKER).exists()):
        return data_dir
    staging = data_dir / ".migrating"
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        shutil.rmtree(staging, ignore_errors=True)
        for source in [database, *sorted(legacy_dir.glob("profiles/*/terminal.sqlite3"))]:
            _copy_database(source, staging / source.relative_to(legacy_dir))
        for name in ("active-profile.json", "session.key"):
            if (legacy_dir / name).is_file():
                shutil.copy2(legacy_dir / name, staging / name)
        for copy in sorted(legacy_dir.glob("backups/terminal-*.sqlite3")):
            (staging / "backups").mkdir(exist_ok=True)
            shutil.copy2(copy, staging / "backups" / copy.name)
        # The main database goes last: its presence is what says "already migrated".
        for item in sorted(staging.iterdir(), key=lambda entry: entry.name == "terminal.sqlite3"):
            _move_into(item, data_dir / item.name)
        (data_dir / MIGRATION_MARKER).write_text(json.dumps(
            {"source": str(legacy_dir), "migrated_at": int(time.time())}, ensure_ascii=False), encoding="utf-8")
    except (OSError, sqlite3.Error):
        shutil.rmtree(staging, ignore_errors=True)
        return legacy_dir
    shutil.rmtree(staging, ignore_errors=True)
    return data_dir


def _resolve_data_dir(config):
    explicit = config.get("DATA_DIR") or os.environ.get("PDV_TERMINAL_DATA")
    if explicit:
        return Path(explicit)
    data_dir, legacy = default_data_dir(), legacy_data_dir()
    return migrate_legacy_data(legacy, data_dir) if legacy else data_dir


def _version_tuple(value):
    try:
        return tuple(int(part) for part in str(value).split("."))
    except ValueError:
        return ()


def _active_profile_db(data_dir, fallback):
    """Resolve the selected local profile without trusting arbitrary paths."""
    pointer = data_dir / "active-profile.json"
    try:
        value = json.loads(pointer.read_text(encoding="utf-8"))
        name = value.get("profile") if isinstance(value, dict) else None
    except (OSError, ValueError, TypeError):
        return fallback
    if not isinstance(name, str) or not name or Path(name).name != name:
        return fallback
    candidate = data_dir / "profiles" / name / "terminal.sqlite3"
    return candidate if candidate.exists() else fallback


def _write_active_profile(data_dir, name):
    pointer = data_dir / "active-profile.json"
    temporary = data_dir / ".active-profile.tmp"
    temporary.write_text(json.dumps({"profile": name}, ensure_ascii=False), encoding="utf-8")
    try:
        temporary.chmod(0o600)
    except OSError:
        pass
    os.replace(temporary, pointer)


# /api/bootstrap is reachable from the login screen: keep it from being hammered.
BOOTSTRAP_CALLS = 6
BOOTSTRAP_WINDOW_SECONDS = 60
MIRROR_UNAVAILABLE = "Cópia em segundo disco indisponível nesta instalação"
LAN_UNAVAILABLE = "Rede entre caixas indisponível nesta instalação"
# Routes an additional cashier (station) may never reach: they belong to the computer that owns
# the database. Same list, by path, in terminal/lan.py.
SERVER_ONLY_ENDPOINTS = frozenset((
    "pair", "unpair", "new_profile", "bootstrap", "restore_start", "restore_state", "restore_advance",
    "restore_retry", "restore_abandon", "restore_backups", "restore_import_backup", "restore_skip_backup",
    "export", "backups", "create_backup", "mirror_drives", "mirror_config", "mirror_snapshot", "update_apply",
    "stations_list", "stations_code", "station_rename", "station_revoke", "station_cash_close",
    "mode_station", "promotion_regularize"))
STATION_DAMAGED = "O caixa principal está com o banco de dados danificado. Chame o suporte."


def create_app(config=None):
    config = dict(config or {})
    data_dir = _resolve_data_dir(config)
    data_dir.mkdir(parents=True, exist_ok=True)
    try:
        data_dir.chmod(0o700)
    except OSError:
        pass
    secret_path = data_dir / "session.key"
    if not secret_path.exists():
        try:
            fd = os.open(secret_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as secret_file:
                secret_file.write(secrets.token_bytes(32))
        except FileExistsError:
            pass
    app = Flask(__name__, template_folder="templates", static_folder="static")
    app.config.update(SECRET_KEY=secret_path.read_bytes(), SESSION_COOKIE_HTTPONLY=True,
                      SESSION_COOKIE_SAMESITE="Strict", SESSION_COOKIE_NAME="pdv2_session",
                      MAX_CONTENT_LENGTH=256 * 1024)
    app.config.update(config)
    configured_db = Path(config.get("DB_PATH") or os.environ.get("PDV_TERMINAL_DB") or data_dir / "terminal.sqlite3")
    db_path = _active_profile_db(data_dir, configured_db)
    allowed_hosts = set(config.get("ALLOWED_HOSTS") or ("localhost", "127.0.0.1", "[::1]"))
    # A damaged file must not take the screen down: start without selling and
    # tell the shop how to bring the last local backup back.
    damage = Store.quick_check(db_path)
    store = None
    if damage is None:
        try:
            store = Store(db_path)
        except sqlite3.DatabaseError as exc:
            # Only real corruption sends the shop to its backups. A failed migration, a lock
            # or a permission error must stop the process so the updater can roll back.
            if not Store.is_corruption(exc):
                raise
            damage = str(exc) or type(exc).__name__
    worker = None
    app.extensions["terminal_store"] = store
    app.extensions["terminal_damage"] = damage
    app.extensions["terminal_data_dir"] = data_dir
    bootstrap_calls = []
    if store is None:
        app.logger.error("Banco local danificado (%s): %s", db_path, damage)

    def request_station():
        """Additional cashier behind this request, or None for the browser of this computer.

        Only terminal/lan.py can put this key in the WSGI environ, after checking the station's
        token: an HTTP client can only create environ keys that start with HTTP_.
        """
        station = request.environ.get("pdv2.station")
        if (isinstance(station, dict) and isinstance(station.get("id"), str) and station["id"]
                and station["id"] != SERVER_STATION_ID):
            return station
        return None

    @app.before_request
    def local_security():
        station = request_station()
        if store is not None:
            store.set_station(station["id"] if station else None)
        host = request.host.rsplit(":", 1)[0]
        if host not in allowed_hosts:
            raise TerminalError("Host não permitido", 403)
        if station and request.endpoint in SERVER_ONLY_ENDPOINTS:
            raise TerminalError(SERVER_ONLY_MESSAGE, 403, code="server_only")
        if store is None:
            if request.endpoint in ("index", "favicon", "health", "static"):
                return None
            if station:
                # Another computer asking: no file path of this one leaves here.
                return jsonify({"error": STATION_DAMAGED, "damaged": True}), 503
            if request.endpoint == "session_state":
                if not session.get("csrf"):
                    session["csrf"] = secrets.token_urlsafe(32)
                return jsonify({"csrf": session["csrf"], "paired": False, "operator": None, "operators": [],
                                "damaged": {"database": str(db_path), "problem": damage,
                                            "backup_directory": str(db_path.parent / "backups"),
                                            "backups": Store.local_backups(db_path)}})
            return jsonify({"error": "Banco de dados local danificado. As vendas estão paradas até a restauração "
                                     "do último backup; chame o suporte.", "damaged": True}), 503
        if request.method in ("POST", "PUT", "PATCH", "DELETE"):
            origin = request.headers.get("Origin")
            if origin:
                parsed = urlparse(origin)
                if parsed.scheme not in ("http", "https") or parsed.netloc != request.host:
                    raise TerminalError("Origem não permitida", 403)
            if not session.get("csrf") or not secrets.compare_digest(
                    request.headers.get("X-CSRF-Token", ""), session["csrf"]):
                raise TerminalError("Sessão/CSRF inválido; recarregue a página", 403)

    @app.teardown_request
    def forget_station(_exc):
        # The worker threads of the server are reused: the next request starts as the main cashier.
        if store is not None:
            store.set_station(None)

    @app.errorhandler(TerminalError)
    def error(exc):
        answer = {"error": str(exc)}
        if getattr(exc, "code", None):
            answer["code"] = exc.code
        return jsonify(answer), exc.status

    @app.errorhandler(sqlite3.Error)
    def database_error(exc):
        # Only a database that is locked or cannot be read is "busy, try again".
        unavailable = ((isinstance(exc, sqlite3.OperationalError) or type(exc) is sqlite3.DatabaseError)
                       and any(hint in str(exc).lower() for hint in DATABASE_UNAVAILABLE_HINTS))
        if unavailable:
            app.logger.error("Banco local indisponível: %s: %s", type(exc).__name__, exc)
            return jsonify({"error": "Banco de dados local ocupado ou indisponível. Aguarde alguns segundos e tente "
                                     "novamente; se persistir, reinicie o terminal."}), 503
        app.logger.error("Falha no banco local: %s: %s", type(exc).__name__, exc)
        return jsonify({"error": "Erro interno ao gravar no banco de dados local. A operação não foi concluída; "
                                 "se o erro se repetir, chame o suporte."}), 500

    @app.errorhandler(404)
    def not_found(_exc):
        return jsonify({"error": "Rota não encontrada"}), 404

    def session_operator_id():
        """Operator of this browser session, as long as it logged in on this same cashier.

        A session cookie copied from one computer of the shop is worth nothing on another.
        """
        if session.get("station_id", SERVER_STATION_ID) != store.current_station():
            return None
        return session.get("operator_id")

    def authenticated(*roles):
        def decorate(func):
            @wraps(func)
            def wrapper(*args, **kwargs):
                operator_id = session_operator_id()
                if not operator_id:
                    raise TerminalError("Faça login com PIN", 401)
                operator = store.operator(operator_id)
                marker = hmac.new(app.secret_key, store.operator_pin_hash(operator_id).encode(),
                                  hashlib.sha256).hexdigest()
                if not secrets.compare_digest(session.get("auth_marker", ""), marker):
                    session.pop("operator_id", None)
                    session.pop("auth_marker", None)
                    raise TerminalError("PIN ou sessão alterados; entre novamente", 401)
                if roles and operator["role"] not in roles:
                    raise TerminalError("Sem permissão para esta operação", 403)
                return func(operator, *args, **kwargs)
            return wrapper
        return decorate

    def body():
        value = request.get_json(silent=True)
        if not isinstance(value, dict):
            raise TerminalError("Envie um objeto JSON")
        return value

    def session_manager():
        """Manager logged in on this browser session, if any."""
        operator_id = session_operator_id()
        if not operator_id:
            return None
        try:
            operator = store.operator(operator_id)
            marker = hmac.new(app.secret_key, store.operator_pin_hash(operator_id).encode(),
                              hashlib.sha256).hexdigest()
        except TerminalError:
            return None
        if not secrets.compare_digest(session.get("auth_marker", ""), marker):
            return None
        return operator if operator["role"] in MANAGER_ROLES else None

    def require_manager_when_paired(data):
        """Maintenance of a paired terminal needs a manager session or a manager PIN in the body.

        Before pairing nobody can log in, so the routes stay open; the same holds
        while no manager PIN has reached this terminal yet (wrong pairing, first load).
        """
        if not store.identity()["terminal_id"] or not store.has_manager() or session_manager():
            return
        # No requester: these attempts are counted apart from the cashier's authorizations.
        store.manager_override(data.get("manager_pin"))

    def throttle_cloud_calls():
        now = time.monotonic()
        bootstrap_calls[:] = [moment for moment in bootstrap_calls if now - moment < BOOTSTRAP_WINDOW_SECONDS]
        if len(bootstrap_calls) >= BOOTSTRAP_CALLS:
            raise TerminalError("Muitas atualizações seguidas; aguarde um minuto e tente novamente", 429)
        bootstrap_calls.append(now)

    def update_status():
        """What updater.status says, or the plain release notice when this build has no updater."""
        base = {"supported": sync.updater is not None, "current_version": sync.VERSION, "available": None,
                "staged_version": None, "state": "idle", "error": "", "can_apply": False, "blocked_reason": ""}
        if sync.updater is None:
            release = store.sync_status()["release"]
            if release and _version_tuple(release.get("version")) > _version_tuple(sync.VERSION):
                base["available"] = release
            base["blocked_reason"] = "Atualização automática indisponível nesta instalação"
            return base
        try:
            status = {**base, **sync.updater.status(store)}
        except Exception as exc:
            app.logger.error("Atualizador falhou ao informar o estado: %s: %s", type(exc).__name__, exc)
            return dict(base, state="failed", error="Não foi possível consultar o atualizador",
                        blocked_reason="Não foi possível consultar o atualizador")
        if status["can_apply"] and store.any_cash_open():
            status.update(can_apply=False, blocked_reason="Feche o caixa em todos os computadores para atualizar")
        return status

    def lan_status():
        """What lan.status says, or a plain "not here" when this build has no network between cashiers."""
        missing = {"supported": False, "running": False, "message": LAN_UNAVAILABLE}
        if sync.lan is None:
            return missing
        try:
            return sync.lan.status(app)
        except Exception as exc:
            app.logger.error("Rede entre caixas falhou ao informar o estado: %s", type(exc).__name__)
            return dict(missing, supported=True, message="Não foi possível consultar a rede entre caixas")

    def lan_poke():
        if sync.lan is None:
            return
        try:
            sync.lan.poke(app)
        except Exception as exc:  # the listener re-evaluates by itself every 30 seconds
            app.logger.error("Rede entre caixas não foi avisada: %s", type(exc).__name__)

    def replica_status():
        if sync.replica_net is None:
            return {"supported": False}
        try:
            return sync.replica_net.status(store)
        except Exception as exc:
            app.logger.error("Cópia pela rede falhou ao informar o estado: %s", type(exc).__name__)
            return {"supported": True, "state": "off", "message": "Não foi possível consultar a cópia pela rede",
                    "stations": {}}

    def station_info():
        station = request_station()
        if station:
            name = station.get("name") if isinstance(station.get("name"), str) else ""
            version = station.get("version") if isinstance(station.get("version"), str) else ""
            return {"id": station["id"], "name": name or store.station_name(station["id"]) or "Caixa",
                    "is_server": False, "version": version}
        return {"id": SERVER_STATION_ID, "name": store.station_name(SERVER_STATION_ID), "is_server": True}

    def mirror_status():
        """What mirror.status says, or a plain "not here" when this build has no second-disk copy."""
        if sync.mirror is None:
            return {"supported": False, "enabled": False, "state": "off", "message": MIRROR_UNAVAILABLE}
        try:
            return sync.mirror.status(store)
        except Exception as exc:
            app.logger.error("Cópia em segundo disco falhou ao informar o estado: %s", type(exc).__name__)
            return {"supported": True, "enabled": False, "state": "error",
                    "message": "Não foi possível consultar a cópia de segurança"}

    def require_mirror():
        if sync.mirror is None:
            raise TerminalError(MIRROR_UNAVAILABLE, 409)
        return sync.mirror

    def mirror_call(action, func, *args, **kwargs):
        """Run one operation of the copy; only its own errors (TerminalError) reach the screen as they are."""
        try:
            return func(*args, **kwargs)
        except TerminalError:
            raise
        except Exception as exc:
            app.logger.error("Cópia em segundo disco: %s falhou: %s", action, type(exc).__name__)
            raise TerminalError("Não foi possível concluir a operação da cópia de segurança. Confira o disco "
                                "e tente de novo.", 500) from exc

    def start_mirror(target):
        if sync.mirror is None or target is None:
            return
        try:
            app.extensions["terminal_mirror"] = sync.mirror.start(target)
        except Exception as exc:  # the copy never keeps the cashier from starting
            app.logger.error("Cópia em segundo disco não iniciou: %s", type(exc).__name__)

    def stop_mirror():
        handle = app.extensions.pop("terminal_mirror", None)
        if handle is not None:
            try:
                handle.stop()
            except Exception as exc:
                app.logger.error("Cópia em segundo disco não parou: %s", type(exc).__name__)

    def require_restore(*states, second_pass=False):
        status = store.restore_status()
        if not status["active"]:
            raise TerminalError("Restauração não está em andamento", 409)
        again = second_pass and status["state"] == "restoring" and status["pass"] == 2
        if states and status["state"] not in states and not again:
            raise TerminalError("A recuperação não está esperando a cópia de segurança", 409)
        return status

    @app.get("/")
    def index():
        return render_template("index.html")

    @app.get("/favicon.ico")
    def favicon():
        return app.send_static_file("favicon.svg")

    @app.get("/health")
    def health():
        if store is None:
            # Not 2xx: the updater must not take "started damaged" for "started fine".
            return jsonify({"status": "degraded", "service": "terminal-local", "database": "damaged"}), 503
        return jsonify({"status": "ok", "service": "terminal-local", "mode": "server"})

    @app.get("/api/session")
    def session_state():
        if not session.get("csrf"):
            session["csrf"] = secrets.token_urlsafe(32)
        operator = None
        logged_here = session_operator_id()
        if logged_here:
            try:
                operator = store.operator(logged_here)
                marker = hmac.new(app.secret_key, store.operator_pin_hash(operator["id"]).encode(),
                                  hashlib.sha256).hexdigest()
                if not secrets.compare_digest(session.get("auth_marker", ""), marker):
                    raise TerminalError("Sessão alterada", 401)
            except TerminalError:
                session.pop("operator_id", None)
                session.pop("auth_marker", None)
                operator = None
        identity = store.identity()
        return jsonify({"csrf": session["csrf"], "operator": operator,
                        "operators": store.operators(), "paired": bool(identity["terminal_id"]),
                        "device_id": identity["device_id"], "connection": store.connection_info(),
                        "license": store.lease_status(),
                        "hours": store.business_hours_status(),
                        "cash": store.cash_state(), "sync": store.sync_status(),
                        "update": update_status(),
                        "restore": store.restore_status(),
                        "mirror": mirror_status() if sync.mirror is not None else None,
                        "station": station_info(),
                        "promotion": store.promotion_status(),
                        "stations_limit": store.station_limit(),
                        "default_cloud_url": os.environ.get("PDV_CLOUD_URL", ""),
                        "payment_methods": store.payment_methods()})

    @app.post("/api/unpair")
    def unpair():
        data = body()
        if data.get("confirm") is not True:
            raise TerminalError("Confirme a troca de empresa", 409)
        require_manager_when_paired(data)
        result = store.unpair()
        session.clear()
        return jsonify(result)

    @app.post("/api/new-profile")
    def new_profile():
        """Create a clean local profile while preserving the current database."""
        nonlocal store, worker
        data = body()
        if data.get("confirm") is not True:
            raise TerminalError("Confirme a criação de um novo perfil", 409)
        if session.get("operator_id"):
            raise TerminalError("Saia do operador antes de criar outro perfil", 409)
        require_manager_when_paired(data)
        current = store
        previous_pointer = data_dir / "active-profile.json"
        previous_contents = previous_pointer.read_bytes() if previous_pointer.exists() else None
        old_worker = worker
        if old_worker:
            old_worker.stop()
            worker = None
            stop_mirror()
        try:
            if current.pending_events():
                raise TerminalError("Sincronize os eventos pendentes antes de criar outro perfil", 409)
            if current.any_cash_open():
                raise TerminalError("Feche o caixa antes de criar outro perfil", 409)
            backup = current.backup_now() if current.path.exists() else None
            profile_name = "perfil-" + secrets.token_hex(6)
            profile_dir = data_dir / "profiles" / profile_name
            profile_dir.mkdir(mode=0o700, parents=True, exist_ok=False)
            new_store = Store(profile_dir / "terminal.sqlite3")
            _write_active_profile(data_dir, profile_name)
            store = new_store
            app.extensions["terminal_store"] = store
            if old_worker:
                worker = sync.Worker(store)
                worker.start()
                app.extensions["terminal_worker"] = worker
                start_mirror(store)
            session.clear()
            return jsonify({"profile": profile_name, "paired": False, "preserved_backup": backup})
        except Exception:
            if previous_contents is None:
                previous_pointer.unlink(missing_ok=True)
            else:
                previous_pointer.write_bytes(previous_contents)
            store = current
            app.extensions["terminal_store"] = store
            if old_worker:
                worker = sync.Worker(store)
                worker.start()
                app.extensions["terminal_worker"] = worker
                start_mirror(store)
            raise

    @app.post("/api/pair")
    def pair():
        data = body()
        result = sync.pair(store, data.get("cloud_url"), data.get("license_key"), data.get("code"))
        return jsonify({"paired": True, "bootstrap": result})

    @app.post("/api/restore/start")
    def restore_start():
        # "Recuperar minha loja": same pairing as /api/pair, with the recovery code of the panel.
        data = body()
        result = warning = None
        already_paired = bool(store.identity()["terminal_id"])
        try:
            result = sync.pair(store, data.get("cloud_url") or os.environ.get("PDV_CLOUD_URL", ""),
                               data.get("license_key"), data.get("code"))
        except TerminalError as exc:
            if already_paired or not store.identity()["terminal_id"]:
                raise
            # The pairing is saved and the one-time code is spent; only the first load of the
            # catalog failed, and the worker repeats it. The screen must follow, not ask again.
            warning = str(exc)
        status = store.restore_status()
        answer = {"paired": True, "recovery": status["active"], "restore": status, "bootstrap": result}
        if warning:
            answer["warning"] = warning
        return jsonify(answer)

    @app.get("/api/restore/status")
    def restore_state():
        return jsonify({"restore": store.restore_status()})

    @app.post("/api/restore/step")
    def restore_advance():
        body()
        require_restore()
        # A connection failure comes back in restore.error with HTTP 200: the screen keeps trying.
        return jsonify({"restore": sync.restore_step(store, max_pages=3)})

    @app.post("/api/restore/retry")
    def restore_retry():
        body()
        return jsonify({"restore": store.restore_retry()})

    @app.post("/api/restore/abandon")
    def restore_abandon():
        # The way out when the recovery cannot finish (terminal revoked or moved again):
        # this computer forgets the pairing and what came down, and can start over.
        data = body()
        if data.get("confirm") is not True:
            raise TerminalError("Confirme a desistência da recuperação", 409)
        require_restore()
        require_manager_when_paired(data)
        store.ensure_restore_abandonable()
        # The cloud gives the terminal back to the computer it was taken from (best effort:
        # offline, this computer still forgets everything and the old one keeps sending).
        released = sync.release_terminal(store)
        result = store.restore_abandon()
        session.clear()
        return jsonify({**result, "released": released, "restore": store.restore_status()})

    @app.get("/api/restore/backups")
    def restore_backups():
        require_restore("awaiting_backup", "importing", second_pass=True)
        if sync.mirror is None:
            return jsonify({"supported": False, "candidates": []})
        directory = request.args.get("directory", "").strip() or None
        return jsonify({"supported": True, "candidates": mirror_call(
            "procura de cópias", sync.mirror.candidates, store.identity(), directory)})

    @app.post("/api/restore/import-backup")
    def restore_import_backup():
        data = body()
        # Also while a first copy is going up: another disk may hold what an unreadable one lost
        # (the queue never takes the same event twice).
        require_restore("awaiting_backup", "importing", second_pass=True)
        mirror = require_mirror()
        directory, replica_id = data.get("directory"), data.get("replica_id")
        if not isinstance(directory, str) or not directory.strip() or len(directory) > 1000:
            raise TerminalError("Informe a pasta ou o disco onde está a cópia de segurança")
        if replica_id is not None and (not isinstance(replica_id, str) or not 0 < len(replica_id) <= 200):
            raise TerminalError("Cópia de segurança inválida")
        try:
            key = json.loads(store.meta().get("mirror_key") or "null")
        except ValueError:
            key = None
        if not isinstance(key, dict):
            raise TerminalError("Conecte este caixa à internet para liberar a cópia de segurança.", 409)
        found = mirror_call("leitura da cópia", mirror.read_pending, directory.strip(), key, store.identity(),
                            replica_id)
        events = found.get("events") if isinstance(found, dict) else None
        warnings = found.get("warnings") if isinstance(found, dict) else None
        warnings = [str(item)[:300] for item in warnings][:20] if isinstance(warnings, list) else []
        result = store.import_outbox_from_mirror(events if isinstance(events, list) else [])
        if warnings:
            store.record_audit(None, "restore.mirror_warnings", None, "; ".join(warnings)[:300])
        return jsonify({**result, "warnings": warnings, "restore": store.restore_status()})

    @app.post("/api/restore/skip-backup")
    def restore_skip_backup():
        body()
        return jsonify({"restore": store.restore_skip_backup()})

    @app.get("/api/mirror/status")
    @authenticated()
    def mirror_state(_operator):
        return jsonify(mirror_status())

    @app.get("/api/mirror/drives")
    @authenticated(*MANAGER_ROLES)
    def mirror_drives(_operator):
        mirror = require_mirror()
        return jsonify({"drives": mirror_call("lista de discos", mirror.drives, store)})

    @app.post("/api/mirror/config")
    @authenticated(*MANAGER_ROLES)
    def mirror_config(operator):
        data = body()
        mirror = require_mirror()
        directory = data.get("directory")
        if directory is not None and (not isinstance(directory, str) or not directory.strip()
                                      or len(directory) > 1000):
            raise TerminalError("Informe a pasta ou o disco da cópia de segurança")
        directory = directory.strip() if directory else None
        result = mirror_call("configuração", mirror.configure, store, directory,
                             allow_same_volume=os.environ.get("PDV_ENV", "production") in ("development", "test"))
        state = result.get("state") if isinstance(result, dict) else None
        detail = "desligada"
        if directory and state == "ok":
            detail = "ligada: " + directory
        elif directory:
            # configure() does not raise when the first copy fails: it answers with the state.
            problem = (result.get("last_error") or result.get("message") or state or "") if isinstance(result, dict) else ""
            detail = f"ligada, primeira cópia falhou ({problem}): {directory}"
        store.record_audit(operator["id"], "mirror.configured", None, detail[:300])
        store.ensure_config_event()
        return jsonify(result)

    @app.post("/api/mirror/snapshot")
    @authenticated(*MANAGER_ROLES)
    def mirror_snapshot(_operator):
        body()
        mirror = require_mirror()
        return jsonify(mirror_call("cópia imediata", mirror.snapshot_now, store))

    @app.post("/api/bootstrap")
    def bootstrap():
        # A PIN may have changed in the cloud since this terminal last synced.
        # The paired device token authorizes this read-only bootstrap, so it
        # must also be possible from the login screen before local PIN auth.
        throttle_cloud_calls()
        return jsonify(sync.refresh_bootstrap(store))

    @app.post("/api/license/renew")
    @authenticated()
    def license_renew(_operator):
        # "Verificar de novo" of the license and clock screens. Offline it only re-reads the lease.
        throttle_cloud_calls()
        renewed, error = False, ""
        try:
            renewed = sync.renew_lease(store, force=True) is not None
        except TerminalError as exc:
            error = str(exc)
        return jsonify({"renewed": renewed, "error": error, "license": store.lease_status()})

    @app.get("/api/update/status")
    @authenticated()
    def update_state(_operator):
        return jsonify(update_status())

    @app.post("/api/update/apply")
    @authenticated(*MANAGER_ROLES)
    def update_apply(operator):
        if sync.updater is None:
            raise TerminalError("Atualização automática indisponível nesta instalação", 409)
        status = update_status()
        if not status["can_apply"]:
            raise TerminalError(status["blocked_reason"] or "Atualização indisponível no momento", 409)
        store.record_audit(operator["id"], "terminal.update_requested", None, str(status["staged_version"] or ""))
        try:
            return jsonify(sync.updater.request_apply(store))
        except TerminalError:
            raise
        except Exception as exc:
            app.logger.error("Atualização não aplicada: %s: %s", type(exc).__name__, exc)
            raise TerminalError("Não foi possível aplicar a atualização; a versão atual continua em uso", 500) from exc

    @app.post("/api/login")
    def login():
        data = body()
        operator = store.login(data.get("operator_id"), data.get("pin"))
        session.clear()
        session["csrf"] = secrets.token_urlsafe(32)
        session["operator_id"] = operator["id"]
        session["station_id"] = store.current_station()
        session["auth_marker"] = hmac.new(app.secret_key, store.operator_pin_hash(operator["id"]).encode(),
                                           hashlib.sha256).hexdigest()
        return jsonify({"operator": operator, "csrf": session["csrf"]})

    @app.post("/api/logout")
    @authenticated()
    def logout(_operator):
        session.clear()
        return jsonify({"ok": True})

    @app.get("/api/products")
    @authenticated()
    def products(_operator):
        found = store.product_search(request.args.get("q", ""), request.args.get("category", ""))
        return jsonify({**found, "categories": store.categories()})

    @app.get("/api/products/scale/<scale_code>")
    @authenticated()
    def product_by_scale_code(_operator, scale_code):
        # Scale labels must resolve over the whole catalog: the product list is capped at 150 rows.
        return jsonify({"product": store.product_by_scale_code(scale_code)})

    @app.get("/api/products/batch")
    @authenticated()
    def products_batch(_operator):
        # Current price and stock for the products of a cart (resume, reload, catalog refresh).
        return jsonify({"products": store.products_by_ids(request.args.get("ids", "").split(","))})

    @app.get("/api/cash")
    @authenticated()
    def cash(_operator):
        return jsonify({"cash": store.cash_state()})

    @app.get("/api/cash/summary")
    @authenticated(*SELL_ROLES)
    def cash_summary(_operator):
        return jsonify(store.cash_summary())

    @app.post("/api/cash/open")
    @authenticated(*SELL_ROLES)
    def cash_open(operator):
        return jsonify(store.open_cash(operator["id"], body().get("opening_cents")))

    @app.post("/api/cash/movement")
    @authenticated(*SELL_ROLES)
    def cash_movement(operator):
        data = body()
        return jsonify(store.cash_movement(operator["id"], data.get("kind"),
                                           data.get("amount_cents"), data.get("reason")))

    @app.post("/api/cash/close")
    @authenticated(*SELL_ROLES)
    def cash_close(operator):
        data = body()
        return jsonify(store.close_cash(operator["id"], data.get("counted_cents"), data.get("manager_pin")))

    # ---------- operator shift: pause, reports of the day, closing of the day ----------

    @app.post("/api/cash/pause")
    @authenticated(*SELL_ROLES)
    def cash_pause(operator):
        return jsonify({"cash": store.pause_cash(operator, body().get("reason") or "")})

    @app.post("/api/cash/resume")
    @authenticated()
    def cash_resume(operator):
        return jsonify({"cash": store.resume_cash(operator, body().get("pin"))})

    @app.get("/api/cash/shifts")
    @authenticated(*SELL_ROLES)
    def cash_shifts(operator):
        result = store.shifts_of_day(request.args.get("day") or None)
        if operator["role"] not in MANAGER_ROLES:
            # A cashier reprints only their own reports.
            result["shifts"] = [row for row in result["shifts"] if row["operator_id"] == operator["id"]]
            result["older_open"] = [row for row in result["older_open"] if row["operator_id"] == operator["id"]]
        return jsonify(result)

    @app.post("/api/cash/shifts/<session_id>/print")
    @authenticated(*SELL_ROLES)
    def cash_shift_print(operator, session_id):
        return jsonify(store.print_shift_report(session_id, operator["id"],
                                                own_only=operator["role"] not in MANAGER_ROLES))

    @app.post("/api/cash/day/close")
    @authenticated(*SELL_ROLES)
    def cash_day_close(operator):
        data = body()
        return jsonify(store.close_day(operator["id"], data.get("manager_pin"), data.get("day") or None))

    @app.post("/api/sales")
    @authenticated(*SELL_ROLES)
    def sell(operator):
        return jsonify(store.sale(operator, body()))

    @app.get("/api/sales")
    @authenticated()
    def sales(_operator):
        return jsonify({"sales": store.sales()})

    @app.get("/api/sales/<sale_id>")
    @authenticated()
    def sale_detail(_operator, sale_id):
        return jsonify({"sale": store.sale_detail(sale_id)})

    @app.post("/api/sales/<sale_id>/void")
    @authenticated(*SELL_ROLES)
    def void_sale(operator, sale_id):
        data = body()
        return jsonify(store.void_sale(operator, sale_id, data.get("reason"), data.get("manager_pin")))

    @app.get("/api/sales/<sale_id>/returnable")
    @authenticated(*SELL_ROLES)
    def returnable(operator, sale_id):
        return jsonify(store.returnable(sale_id))

    @app.post("/api/sales/<sale_id>/return")
    @authenticated(*SELL_ROLES)
    def return_sale(operator, sale_id):
        return jsonify(store.return_sale(operator, sale_id, body()))

    @app.post("/api/cart/quote")
    @authenticated(*SELL_ROLES)
    def cart_quote(operator):
        return jsonify(store.quote(body().get("items")))

    @app.post("/api/customers/receive")
    @authenticated(*SELL_ROLES)
    def receive_credit(operator):
        return jsonify(store.receive_credit(operator, body()))

    @app.get("/api/customers")
    @authenticated(*SELL_ROLES)
    def customers(operator):
        return jsonify({"customers": store.customers(request.args.get("q", ""))})

    @app.post("/api/sales/<sale_id>/reprint")
    @authenticated(*SELL_ROLES)
    def reprint(operator, sale_id):
        return jsonify(store.reprint(sale_id, operator["id"]))

    @app.get("/api/carts/suspended")
    @authenticated(*SELL_ROLES)
    def carts(_operator):
        return jsonify({"carts": store.suspended_carts()})

    @app.post("/api/carts/suspended")
    @authenticated(*SELL_ROLES)
    def suspend(operator):
        data = body()
        return jsonify(store.suspend_cart(operator["id"], data.get("label"), data.get("cart")))

    @app.post("/api/carts/suspended/<cart_id>/resume")
    @authenticated(*SELL_ROLES)
    def resume(operator, cart_id):
        return jsonify({"cart": store.resume_cart(operator["id"], cart_id)})

    @app.post("/api/stock/adjust")
    @authenticated("owner", "admin", "manager", "inventory")
    def stock_adjust(operator):
        data = body()
        return jsonify(store.adjust_stock(operator, data.get("product_id"),
                                          data.get("delta_milli"), data.get("reason")))

    @app.get("/api/audit")
    @authenticated("owner", "admin", "manager")
    def audit(_operator):
        return jsonify({"events": store.audit()})

    @app.get("/api/print/settings")
    @authenticated()
    def printer_settings(_operator):
        return jsonify({"settings": store.printer_settings(), "jobs": store.print_jobs()})

    @app.get("/api/print/discover")
    @authenticated(*MANAGER_ROLES)
    def discover_printers(_operator):
        return jsonify({"driver": "cups", "queues": cups_queues()})

    @app.post("/api/print/settings")
    @authenticated(*MANAGER_ROLES)
    def set_printer(operator):
        data = body()
        return jsonify(store.save_printer(data.get("kind"), data.get("driver"),
                                          data.get("target"), data.get("format"), operator["id"]))

    @app.post("/api/print/labels")
    @authenticated(*SELL_ROLES)
    def print_label(_operator):
        return jsonify({"error": "A impressão de etiquetas é feita no painel da empresa, em Produtos > Imprimir etiquetas."}), 410

    @app.post("/api/discount/authorize")
    @authenticated(*SELL_ROLES)
    def authorize_discount(operator):
        data = body()
        return jsonify(store.authorize_discount(data.get("pin"), data.get("gross_cents"),
                                                data.get("discount_cents"), operator["id"]))

    @app.post("/api/cart/items/authorize-remove")
    @authenticated(*SELL_ROLES)
    def authorize_item_remove(operator):
        return jsonify(store.authorize_item_removal(operator, body().get("manager_pin")))

    @app.post("/api/print/jobs/<job_id>/retry")
    @authenticated(*SELL_ROLES)
    def retry_print(operator, job_id):
        if body().get("confirm_duplicate") is not True:
            raise TerminalError("Confirme que a impressão pode ser duplicada", 409)
        return jsonify(store.retry_print(operator["id"], job_id))

    @app.post("/api/print/flush")
    @authenticated(*SELL_ROLES)
    def flush_print(_operator):
        if store.current_station() != SERVER_STATION_ID:
            # The additional cashier prints on its own computer: its agent fetches the queue.
            return jsonify({"status": "delegated"})
        return jsonify(sync.print_once(store) or {"status": "empty"})

    @app.get("/api/sync")
    @authenticated()
    def sync_state(_operator):
        return jsonify({"version": sync.VERSION, "sync": store.sync_status(), "license": store.lease_status(),
                        "printer_jobs": store.print_jobs()})

    @app.post("/api/sync/now")
    @authenticated(*MANAGER_ROLES)
    def sync_now(_operator):
        result = sync.sync_once(store)
        if body().get("bootstrap"):
            result["bootstrap"] = sync.refresh_bootstrap(store)
        return jsonify(result)

    @app.get("/api/sync/conflicts")
    @authenticated(*MANAGER_ROLES)
    def sync_conflicts(_operator):
        return jsonify({"conflicts": store.sync_conflicts(request.args.get("status"))})

    @app.post("/api/sync/conflicts/<conflict_id>/resolve")
    @authenticated(*MANAGER_ROLES)
    def resolve_sync_conflict(_operator, conflict_id):
        data = body()
        rows = store.resolve_sync_conflict(conflict_id, data.get("status"), data.get("resolution", ""))
        match = next((row for row in rows if row.get("id") == str(conflict_id)), None)
        return jsonify(match or {"id": str(conflict_id), "status": data.get("status")})

    @app.get("/api/sync/quarantine")
    @authenticated(*MANAGER_ROLES)
    def sync_quarantine(_operator):
        # Events the cloud received but kept for review instead of applying.
        return jsonify({"events": store.quarantined_events(request.args.get("status") or "open")})

    @app.get("/api/catalog/skipped")
    @authenticated(*MANAGER_ROLES)
    def catalog_skipped(_operator):
        # Catalog rows of the last bootstrap that this terminal could not use.
        return jsonify({"items": store.catalog_skipped()})

    @app.get("/api/export")
    @authenticated(*MANAGER_ROLES)
    def export(_operator):
        # Remains available after lease expiry. No token, PIN hashes or secret key.
        response = jsonify(store.export_data())
        response.headers["Content-Disposition"] = 'attachment; filename="pdv-terminal-export.json"'
        return response

    @app.get("/api/backups")
    @authenticated(*MANAGER_ROLES)
    def backups(_operator):
        return jsonify(store.backup_status())

    @app.post("/api/backups")
    @authenticated(*MANAGER_ROLES)
    def create_backup(_operator):
        return jsonify(store.backup_now())

    # ---------- phase 3: additional cashiers of the shop (all of it only on the main cashier) ----------

    @app.get("/api/stations")
    @authenticated(*MANAGER_ROLES)
    def stations_list(_operator):
        return jsonify({"supported": sync.lan is not None, "limit": store.station_limit(), "lan": lan_status(),
                        "replica": replica_status(), "stations": store.stations(), "version": sync.VERSION})

    @app.post("/api/stations/code")
    @authenticated(*MANAGER_ROLES)
    def stations_code(operator):
        data = body()
        if sync.lan is None or lan_status().get("supported") is False:
            raise TerminalError(LAN_UNAVAILABLE, 409)
        result = store.create_station_code(operator["id"], data.get("name") or "")
        lan_poke()  # the listener may have to start now: this can be the first additional cashier
        network = lan_status()
        # The code is useless when the listener did not open: the screen says so right away.
        result["server"] = {"addresses": network.get("addresses") or [], "port": network.get("port"),
                            "hostname": network.get("hostname") or "", "running": bool(network.get("running")),
                            "error": str(network.get("error") or network.get("message") or "")}
        response = jsonify(result)
        response.headers["Cache-Control"] = "no-store"
        return response, 201

    @app.post("/api/stations/<station_id>/rename")
    @authenticated(*MANAGER_ROLES)
    def station_rename(operator, station_id):
        return jsonify(store.rename_station(station_id, body().get("name"), operator["id"]))

    @app.post("/api/stations/<station_id>/revoke")
    @authenticated(*MANAGER_ROLES)
    def station_revoke(operator, station_id):
        if body().get("confirm") is not True:
            raise TerminalError("Confirme a remoção do caixa", 409)
        result = store.revoke_station(station_id, operator["id"])
        lan_poke()
        return jsonify(result)

    @app.post("/api/stations/<station_id>/cash/close")
    @authenticated(*MANAGER_ROLES)
    def station_cash_close(operator, station_id):
        # The computer broke, was removed or was replaced: the manager counts its drawer here.
        data = body()
        return jsonify(store.close_cash(operator["id"], data.get("counted_cents"), data.get("manager_pin"),
                                        station_id=station_id))

    @app.post("/api/mode/station")
    def mode_station():
        """"Este é um caixa adicional": only a computer that never served a shop changes role."""
        if body().get("confirm") is not True:
            raise TerminalError("Confirme que este computador é um caixa adicional", 409)
        try:
            store.ensure_pairable()
        except TerminalError as exc:
            raise TerminalError("Este computador já é o caixa principal de uma loja.", 409) from exc
        write_mode = getattr(paths, "write_mode", None)
        if write_mode is None:
            raise TerminalError(LAN_UNAVAILABLE, 409)
        write_mode(data_dir, "station")
        restart = False
        if sync.runtime is not None:
            try:
                restart = bool(sync.runtime.request_restart("modo-estacao"))
            except Exception as exc:
                app.logger.error("Reinício não pôde ser pedido: %s", type(exc).__name__)
        return jsonify({"mode": "station", "restart": restart})

    @app.post("/api/promotion/regularize")
    def promotion_regularize():
        """After a promotion: bind this computer to the same terminal in the cloud."""
        data = body()
        if not session_manager():
            store.manager_override(data.get("manager_pin"))
        throttle_cloud_calls()
        cloud_url = data.get("cloud_url") or store.meta().get("cloud_url") or os.environ.get("PDV_CLOUD_URL", "")
        result = sync.regularize_promotion(store, cloud_url, data.get("license_key"), data.get("code"))
        answer = {"regularized": True, "promotion": store.promotion_status(), "bootstrap": result.get("bootstrap")}
        if result.get("warning"):
            answer["warning"] = result["warning"]
        return jsonify(answer)

    if config.get("START_WORKER", False) and store is not None:
        worker = sync.Worker(store)
        worker.start()
        app.extensions["terminal_worker"] = worker
        start_mirror(store)
    return app
