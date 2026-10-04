"""Browser UI and local JSON API. Browser never calls the cloud directly."""

import json
import os
import secrets
import hashlib
import hmac
from functools import wraps
from pathlib import Path
from urllib.parse import urlparse

from flask import Flask, jsonify, render_template, request, session

from . import sync
from .printers import cups_queues
from .store import MANAGER_ROLES, SELL_ROLES, Store, TerminalError


def _default_data_dir():
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "PDV2"
    return Path.home() / ".local" / "share" / "pdv2"


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


def create_app(config=None):
    config = dict(config or {})
    data_dir = Path(config.get("DATA_DIR") or os.environ.get("PDV_TERMINAL_DATA") or _default_data_dir())
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
    store = Store(db_path)
    worker = None
    app.extensions["terminal_store"] = store
    allowed_hosts = set(config.get("ALLOWED_HOSTS") or ("localhost", "127.0.0.1", "[::1]"))

    @app.before_request
    def local_security():
        host = request.host.rsplit(":", 1)[0]
        if host not in allowed_hosts:
            raise TerminalError("Host não permitido", 403)
        if request.method in ("POST", "PUT", "PATCH", "DELETE"):
            origin = request.headers.get("Origin")
            if origin:
                parsed = urlparse(origin)
                if parsed.scheme not in ("http", "https") or parsed.netloc != request.host:
                    raise TerminalError("Origem não permitida", 403)
            if not session.get("csrf") or not secrets.compare_digest(
                    request.headers.get("X-CSRF-Token", ""), session["csrf"]):
                raise TerminalError("Sessão/CSRF inválido; recarregue a página", 403)

    @app.errorhandler(TerminalError)
    def error(exc):
        return jsonify({"error": str(exc)}), exc.status

    @app.errorhandler(404)
    def not_found(_exc):
        return jsonify({"error": "Rota não encontrada"}), 404

    def authenticated(*roles):
        def decorate(func):
            @wraps(func)
            def wrapper(*args, **kwargs):
                operator_id = session.get("operator_id")
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

    @app.get("/")
    def index():
        return render_template("index.html")

    @app.get("/favicon.ico")
    def favicon():
        return app.send_static_file("favicon.svg")

    @app.get("/health")
    def health():
        return jsonify({"status": "ok", "service": "terminal-local"})

    @app.get("/api/session")
    def session_state():
        if not session.get("csrf"):
            session["csrf"] = secrets.token_urlsafe(32)
        operator = None
        if session.get("operator_id"):
            try:
                operator = store.operator(session["operator_id"])
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
                        "cash": store.cash_state(), "sync": store.sync_status()})

    @app.post("/api/unpair")
    def unpair():
        data = body()
        if data.get("confirm") is not True:
            raise TerminalError("Confirme a troca de empresa", 409)
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
        current = store
        previous_pointer = data_dir / "active-profile.json"
        previous_contents = previous_pointer.read_bytes() if previous_pointer.exists() else None
        old_worker = worker
        if old_worker:
            old_worker.stop()
            worker = None
        try:
            if current.pending_events():
                raise TerminalError("Sincronize os eventos pendentes antes de criar outro perfil", 409)
            if current.cash_state():
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
            raise

    @app.post("/api/pair")
    def pair():
        data = body()
        result = sync.pair(store, data.get("cloud_url"), data.get("license_key"), data.get("code"))
        return jsonify({"paired": True, "bootstrap": result})

    @app.post("/api/bootstrap")
    def bootstrap():
        # A PIN may have changed in the cloud since this terminal last synced.
        # The paired device token authorizes this read-only bootstrap, so it
        # must also be possible from the login screen before local PIN auth.
        return jsonify(sync.refresh_bootstrap(store))

    @app.post("/api/login")
    def login():
        data = body()
        operator = store.login(data.get("operator_id"), data.get("pin"))
        session.clear()
        session["csrf"] = secrets.token_urlsafe(32)
        session["operator_id"] = operator["id"]
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
        return jsonify({"products": store.products(request.args.get("q", ""), request.args.get("category", "")),
                        "categories": store.categories()})

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
    def authorize_discount(_operator):
        data = body()
        return jsonify(store.authorize_discount(data.get("pin"), data.get("gross_cents"),
                                                data.get("discount_cents")))

    @app.post("/api/print/jobs/<job_id>/retry")
    @authenticated(*SELL_ROLES)
    def retry_print(operator, job_id):
        if body().get("confirm_duplicate") is not True:
            raise TerminalError("Confirme que a impressão pode ser duplicada", 409)
        return jsonify(store.retry_print(operator["id"], job_id))

    @app.post("/api/print/flush")
    @authenticated(*SELL_ROLES)
    def flush_print(_operator):
        return jsonify(sync.print_once(store) or {"status": "empty"})

    @app.get("/api/sync")
    @authenticated()
    def sync_state(_operator):
        return jsonify({"sync": store.sync_status(), "license": store.lease_status(),
                        "printer_jobs": store.print_jobs()})

    @app.post("/api/sync/now")
    @authenticated(*MANAGER_ROLES)
    def sync_now(_operator):
        result = sync.sync_once(store)
        if body().get("bootstrap"):
            result["bootstrap"] = sync.refresh_bootstrap(store)
        return jsonify(result)

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

    if config.get("START_WORKER", False):
        worker = sync.Worker(store)
        worker.start()
        app.extensions["terminal_worker"] = worker
    return app
