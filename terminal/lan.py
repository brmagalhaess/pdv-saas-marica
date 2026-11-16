"""Shop server on the local network: pinned TLS, station tokens and the in-process bridge.

Only the caixa principal runs this. Stations (caixas adicionais) connect to TCP 8011
with TLS; the certificate is self-signed and its SHA-256 is fixed on the station when
it is linked. Every request carries the station token. Requests that are not native
`/lan/v1/*` routes enter the SAME Flask app, in process, with
`environ["pdv2.station"] = {"id", "name"}`: an HTTP client cannot forge an environ key
that has no `HTTP_` prefix, so that key is the only source of truth about who is asking.

Secrets (station tokens, the 6-digit code, the TLS private key) never reach a log.
"""

import base64
import datetime
import hashlib
import hmac
import inspect
import io
import json
import logging
import os
import re
import socket
import ssl
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote

from . import discovery, lanclient, paths

try:  # the network replica belongs to another module and may not be installed
    from . import replica_net
except ImportError:
    replica_net = None

LAN_API = 1
MIN_API = 1
DEFAULT_PORT = 8011
# Floor of simultaneous connections. A station in use holds about nine (six threads of its
# local web server, the monitor, the print agent and the replica puller), so the real limit
# grows with the stations of the plan: see LanServer.connection_limit.
MAX_CONNECTIONS = 128
CONNECTIONS_PER_STATION = 12
MAX_CONNECTIONS_CEILING = 1024
BUSY_DRAIN_BYTES = 2 * 256 * 1024
MAX_BODY = 256 * 1024
IDLE_SECONDS = 30
HANDSHAKE_SECONDS = 10
REEVALUATE_SECONDS = 30
TOUCH_SECONDS = 30
BAD_TOKEN_LIMIT = 20
BAD_TOKEN_WINDOW_SECONDS = 60
BAD_TOKEN_BLOCK_SECONDS = 60
ENROLL_LIMIT = 10
ENROLL_WINDOW_SECONDS = 10 * 60
FIREWALL_RULE = "PDV2 - Servidor da loja"
CERT_YEARS = 20

SUPERSEDED_MESSAGE = ("Este computador não é mais o caixa principal desta loja: outro computador assumiu. "
                      "Procure o novo caixa principal.")
_CHALLENGE = re.compile(r"[0-9a-f]{32}")
SERVER_ONLY_MESSAGE = "Esta operação só pode ser feita no caixa principal da loja."
UNAVAILABLE_MESSAGE = "Rede entre caixas indisponível nesta instalação"
# Canonical list (contract 2.2): the path itself and everything below it, any method.
SERVER_ONLY_PATHS = (
    "/api/pair", "/api/unpair", "/api/new-profile", "/api/bootstrap", "/api/restore", "/api/export",
    "/api/backups", "/api/mirror/drives", "/api/mirror/config", "/api/mirror/snapshot",
    "/api/update/apply", "/api/stations", "/api/promotion", "/api/mode/station",
)
HOP_BY_HOP = frozenset(("connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te",
                        "trailer", "transfer-encoding", "upgrade", "proxy-connection"))
_JOB_DONE = re.compile(r"/lan/v1/print/([A-Za-z0-9_-]{1,64})/done")
_STATION_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
SERVER_STATION_ID = "server"
PREVIOUS_SERVER_PREFIX = "anterior-"
_BUSY_ERRNOS = (48, 98, 10013, 10048)  # EADDRINUSE (macOS, Linux), WSAEACCES, WSAEADDRINUSE
_BUSY = json.dumps({"error": "O caixa principal está ocupado. Tente de novo em instantes.",
                    "code": "server_busy"}, ensure_ascii=False).encode("utf-8")
log = logging.getLogger("pdv2.lan")


# ------------------------------------------------------------- certificate

def fingerprint(cert_pem):
    """SHA-256 of the DER certificate: 64 lowercase hex characters."""
    text = cert_pem.decode("ascii") if isinstance(cert_pem, (bytes, bytearray)) else str(cert_pem)
    return hashlib.sha256(ssl.PEM_cert_to_DER_cert(text.strip())).hexdigest()


def _write_private(path, data):
    temporary = path.with_name(path.name + ".tmp")
    temporary.unlink(missing_ok=True)
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    try:
        path.chmod(0o600)
    except OSError:
        pass


def _load_context(cert_path, key_path):
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(str(cert_path), str(key_path))
    return context


def ensure_certificate(data_dir, shop_id):
    """Creates the server certificate once; later starts reuse it.

    Deleting <data_dir>/lan creates a new identity: every station has to be linked again.
    """
    folder = Path(data_dir) / "lan"
    folder.mkdir(parents=True, exist_ok=True)
    try:
        folder.chmod(0o700)
    except OSError:
        pass
    cert_path, key_path = folder / "cert.pem", folder / "key.pem"
    if cert_path.is_file() and key_path.is_file():
        try:
            _load_context(cert_path, key_path)
            return {"cert_path": cert_path, "key_path": key_path, "fingerprint": fingerprint(cert_path.read_bytes())}
        except (OSError, ssl.SSLError, ValueError):
            log.warning("Certificado da rede entre caixas ilegível; criando outro")
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, ("PDV2 " + str(shop_id or "")).strip()[:64])])
    now = datetime.datetime.now(datetime.timezone.utc)
    certificate = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
                   .serial_number(x509.random_serial_number())
                   .not_valid_before(now - datetime.timedelta(days=1))
                   .not_valid_after(now + datetime.timedelta(days=365 * CERT_YEARS))
                   .add_extension(x509.SubjectAlternativeName([x509.DNSName("pdv2.local")]), critical=False)
                   .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
                   .sign(key, hashes.SHA256()))
    _write_private(key_path, key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                               serialization.NoEncryption()))
    cert_pem = certificate.public_bytes(serialization.Encoding.PEM)
    _write_private(cert_path, cert_pem)
    return {"cert_path": cert_path, "key_path": key_path, "fingerprint": fingerprint(cert_pem)}


# ---------------------------------------------------------------- firewall

def ensure_firewall(port=DEFAULT_PORT, discovery_port=discovery.PORT, *, run=subprocess.run, platform=None):
    """Windows: makes sure the rule of the installer exists. Best effort, idempotent."""
    if not paths.is_windows(platform):
        return "unknown"
    options = {"capture_output": True, "text": True, "timeout": 10,
               "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
    try:
        if run(["netsh", "advfirewall", "firewall", "show", "rule", f"name={FIREWALL_RULE}"],
               **options).returncode == 0:
            return "ok"
        created = True
        for protocol, number in (("TCP", port), ("UDP", discovery_port)):
            result = run(["netsh", "advfirewall", "firewall", "add", "rule", f"name={FIREWALL_RULE}", "dir=in",
                          "action=allow", f"protocol={protocol}", f"localport={number}",
                          "remoteip=localsubnet", "profile=any"], **options)  # every profile: see ESTACOES.md
            created = created and result.returncode == 0
        return "ok" if created else "missing"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


# ------------------------------------------------------------------ helpers

def default_port(env=None):
    env = os.environ if env is None else env
    try:
        port = int(env.get("PDV_LAN_PORT") or DEFAULT_PORT)
    except ValueError:
        return DEFAULT_PORT
    return port if 0 <= port < 65536 else DEFAULT_PORT


def disabled(env=None):
    """PDV_LAN_DISABLED=1: this installation never serves other cashiers (the Docker terminal,
    whose container network the other computers of the shop cannot reach)."""
    env = os.environ if env is None else env
    return str(env.get("PDV_LAN_DISABLED") or "").strip() == "1"


def normalize_path(path):
    """'/api//pair/' -> '/api/pair'. None when the path has dot segments."""
    segments = [segment for segment in path.split("/") if segment]
    if any(segment in (".", "..") for segment in segments):
        return None
    return "/" + "/".join(segments)


def server_only(path):
    """True for the routes a station may never call (contract 2.2)."""
    normalized = (normalize_path(path) or "").lower()
    return any(normalized == item or normalized.startswith(item + "/") for item in SERVER_ONLY_PATHS)


def _clean(value, limit):
    if not isinstance(value, str):
        return ""
    return "".join(ch for ch in value if ch.isprintable()).strip()[:limit]


def _data_dir(app):
    value = app.extensions.get("terminal_data_dir") or app.config.get("DATA_DIR")
    if value:
        return Path(value)
    if os.environ.get("PDV_TERMINAL_DATA"):
        return Path(os.environ["PDV_TERMINAL_DATA"])
    store = app.extensions.get("terminal_store")
    if store is not None and getattr(store, "path", None):
        return Path(store.path).parent
    return Path(paths.data_dir())


def _generation(meta):
    try:
        return max(1, int(meta.get("server_generation") or 1))
    except (TypeError, ValueError):
        return 1


def _copy_key(store):
    """Key of the shop copy (meta.mirror_key), the secret shared with the cashiers that keep it."""
    try:
        value = json.loads(store.meta().get("mirror_key") or "null")
    except (ValueError, AttributeError, TypeError):
        return None
    key = value.get("key") if isinstance(value, dict) else None
    return key if isinstance(key, str) and key else None


def prove_generation(client_factory, address, port, presented, key, terminal_id, above):
    """Asks whoever answers at that address, with that certificate pinned, to prove it is a newer
    generation of `terminal_id`. Returns the proven generation (> above) or None."""
    if (not isinstance(address, str) or isinstance(port, bool) or not isinstance(port, int)
            or not isinstance(presented, str) or not key):
        return None
    challenge = os.urandom(16).hex()
    client = client_factory(address, port, presented, None, timeout=3.0)
    try:
        response = client.request("GET", "/lan/v1/hello?challenge=" + challenge, timeout=3.0)
        try:
            body = response.json() if response.status == 200 else None
        finally:
            response.close()
    except OSError:
        return None
    finally:
        client.close()
    if not isinstance(body, dict) or body.get("terminal_id") != terminal_id or body.get("fingerprint") != presented:
        return None
    generation = body.get("generation")
    if isinstance(generation, bool) or not isinstance(generation, int) or generation <= above:
        return None
    expected = lanclient.generation_proof(key, challenge, terminal_id, generation, presented)
    proof = body.get("proof")
    if not expected or not isinstance(proof, str) or not hmac.compare_digest(expected, proof):
        return None
    return generation


def reserved_station(station_id):
    """True for the identities that never belong to another computer: the caixa principal
    itself and the caixa of the computer that was the principal before a promotion."""
    text = str(station_id or "").lower()
    return text == SERVER_STATION_ID or text.startswith(PREVIOUS_SERVER_PREFIX)


class _Headers(dict):
    """Request headers handed to replica_net: lookups ignore case."""

    def __init__(self, pairs):
        super().__init__((str(name).lower(), value) for name, value in pairs)

    def get(self, key, default=None):
        return super().get(str(key).lower(), default)

    def __getitem__(self, key):
        return super().__getitem__(str(key).lower())

    def __contains__(self, key):
        return super().__contains__(str(key).lower())


class _Refused(Exception):
    def __init__(self, status, message, code, close=False):
        super().__init__(message)
        self.status, self.message, self.code, self.close = status, message, code, close


# ------------------------------------------------------------------ handler

class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    timeout = IDLE_SECONDS
    server_version = "PDV2-LAN"
    sys_version = ""
    # Headers and body leave in ONE write (flushed at the end of each request): two small
    # writes would meet Nagle + delayed ACK and cost up to ~200 ms per answer on a real network.
    wbufsize = 64 * 1024

    def log_message(self, format, *args):  # noqa: A002 - signature of the base class
        return None  # nothing about a request is written anywhere

    def version_string(self):
        return self.server_version

    # -- output
    def _send(self, status, body, headers=(), close=False):
        self.send_response(status)
        for name, value in headers:
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        if close:
            self.send_header("Connection", "close")
        self.end_headers()
        if self.command != "HEAD" and body:
            self.wfile.write(body)

    def _json(self, status, payload, headers=(), close=False):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send(status, body, [("Content-Type", "application/json; charset=utf-8"),
                                  ("Cache-Control", "no-store"), *headers], close)

    def _refuse(self, status, message, code, close=False, extra=None):
        self._json(status, {"error": message, "code": code, **(extra or {})},
                   [("X-PDV-Lan-Error", code)], close)

    # -- entry
    def _handle(self):
        try:
            self._dispatch()
        except _Refused as refusal:
            self._refuse(refusal.status, refusal.message, refusal.code, refusal.close)
        except (OSError, ssl.SSLError):
            self.close_connection = True
        except Exception as exc:  # never a traceback with request data
            log.error("Falha ao atender um caixa adicional: %s", type(exc).__name__)
            self.close_connection = True
            try:
                self._refuse(500, "Erro interno no caixa principal. Tente de novo.", "server_error", close=True)
            except (OSError, ssl.SSLError):
                pass

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = do_OPTIONS = _handle

    def _read_body(self):
        if self.headers.get("Transfer-Encoding"):
            raise _Refused(411, "Envie o tamanho do conteúdo.", "length_required", close=True)
        raw = self.headers.get("Content-Length")
        try:
            length = int(raw) if raw not in (None, "") else 0
        except ValueError:
            length = -1
        if length < 0:
            raise _Refused(400, "Requisição inválida.", "bad_request", close=True)
        if length > MAX_BODY:
            # Swallow a moderately oversized body so the refusal is read instead of a reset.
            remaining = length if length <= 8 * MAX_BODY else 0
            while remaining > 0:
                chunk = self.rfile.read(min(65536, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
            raise _Refused(413, "Conteúdo grande demais.", "too_large", close=True)
        body = self.rfile.read(length) if length else b""
        if len(body) != length:
            raise OSError("corpo incompleto")
        return body

    def _dispatch(self):
        manager = self.server.manager
        address = self.client_address[0]
        if not self.path.startswith("/"):
            raise _Refused(400, "Requisição inválida.", "bad_request", close=True)
        raw_path, _mark, query = self.path.partition("?")
        path = unquote(raw_path, encoding="latin-1")
        if normalize_path(path) is None or "\x00" in path:
            raise _Refused(400, "Requisição inválida.", "bad_request", close=True)
        body = self._read_body()
        store = manager.app.extensions.get("terminal_store")
        if store is None:
            self._refuse(503, "O caixa principal está com o banco de dados danificado. Chame o suporte.",
                         "server_damaged", extra={"damaged": True})
            return
        if path == "/lan/v1/hello":
            self._method("GET")
            challenge = (parse_qs(query).get("challenge") or [""])[0]
            self._json(200, manager.hello(store, challenge if _CHALLENGE.fullmatch(challenge) else None))
            return
        if manager.superseded:
            # A newer generation of this terminal is on the network: nobody links or sells here.
            raise _Refused(409, SUPERSEDED_MESSAGE, "server_superseded")
        if path == "/lan/v1/enroll":
            self._method("POST")
            self._enroll(manager, store, address, body)
            return
        station = self._authenticate(manager, store, address)
        normalized = (normalize_path(path) or "").lower()
        if normalized == "/lan" or normalized.startswith("/lan/"):
            # Native routes are for the agents of the station, never for its browser: the local
            # bridge refuses /lan/* and cannot send this header (it drops every X-PDV-Station*).
            if self.headers.get(lanclient.NATIVE_HEADER[0]) != lanclient.NATIVE_HEADER[1]:
                raise _Refused(404, "Rota não encontrada.", "not_found")
            self._native(manager, store, station, path, query, body)
        elif server_only(path):
            raise _Refused(403, SERVER_ONLY_MESSAGE, "server_only")
        else:
            self._bridge(manager, station, path, query, body)

    def _method(self, *allowed):
        if self.command not in allowed:
            raise _Refused(405, "Método não permitido.", "method_not_allowed")

    def _body_object(self, body):
        try:
            value = json.loads(body.decode("utf-8")) if body else {}
        except (ValueError, UnicodeDecodeError):
            value = None
        if not isinstance(value, dict):
            raise _Refused(400, "Envie um objeto JSON.", "bad_request")
        return value

    @staticmethod
    def _store_error(exc, default_code):
        """A TerminalError of the store becomes a refusal; anything else goes up."""
        status = getattr(exc, "status", None)
        if isinstance(status, bool) or not isinstance(status, int) or not 400 <= status < 600:
            raise exc
        return _Refused(status, str(exc), getattr(exc, "code", None) or default_code)

    # -- authentication
    def _authenticate(self, manager, store, address):
        if manager.blocked(address):
            raise _Refused(429, "Muitas tentativas. Aguarde um minuto.", "too_many_attempts")
        declared = self.headers.get("X-PDV-Station-Api")
        if declared is not None:
            try:
                api = int(declared)
            except ValueError:
                api = 0
            if api > LAN_API:
                raise _Refused(426, "Atualize o caixa principal: a versão do PDV2 dele é antiga.",
                               "server_outdated")
            if api < MIN_API:
                raise _Refused(426, "Atualize este caixa: a versão do PDV2 dele é antiga.", "station_outdated")
        token = self.headers.get("X-PDV-Station-Token") or ""
        try:
            found = store.authenticate_station(token)
        except Exception as exc:
            refusal = self._store_error(exc, "station_unauthorized")
            if token and refusal.code == "station_unauthorized" and manager.bad_token(address):
                raise _Refused(429, "Muitas tentativas. Aguarde um minuto.", "too_many_attempts") from None
            raise refusal from None
        station = {"id": str(found["id"]), "name": str(found.get("name") or "")}
        if reserved_station(station["id"]):
            # Whatever the database says, nobody on the network is the caixa principal.
            raise _Refused(401, "Este caixa não está vinculado ao caixa principal.", "station_unauthorized")
        manager.touch(store, station["id"], address, _clean(self.headers.get("X-PDV-Station-Version"), 20),
                      _clean(self.headers.get("X-PDV-Station-Host"), 64))
        return station

    # -- native routes
    def _enroll(self, manager, store, address, body):
        if not manager.enroll_attempt(address):
            raise _Refused(429, "Muitas tentativas. Aguarde alguns minutos e peça um novo código no caixa "
                                "principal.", "too_many_attempts")
        data = self._body_object(body)
        code = data.get("code")
        if isinstance(code, int) and not isinstance(code, bool):
            code = str(code)
        previous = data.get("station_id")
        if not isinstance(code, str) or any(
                data.get(key) is not None and not isinstance(data.get(key), str)
                for key in ("name", "hostname", "app_version", "station_id")):
            raise _Refused(400, "Dados de vínculo inválidos.", "bad_request")
        if previous is not None and (not _STATION_ID.fullmatch(previous) or reserved_station(previous)):
            previous = None  # malformed or reserved ("server", "anterior-…"): links as a new caixa
        try:
            result = store.enroll_station(code[:32], name=_clean(data.get("name"), 40),
                                          hostname=_clean(data.get("hostname"), 64),
                                          app_version=_clean(data.get("app_version"), 20),
                                          station_id=previous, address=address)
        except Exception as exc:
            raise self._store_error(exc, "invalid_code") from None
        if reserved_station(result.get("station_id")):
            log.error("Vínculo recusado: o banco devolveu uma identidade reservada")
            raise _Refused(409, "Não foi possível vincular este caixa. Peça um novo código no caixa principal.",
                           "invalid_code")
        manager.poke()
        info = manager.server_info(store)
        self._json(200, {"api": LAN_API, "station_id": result["station_id"],
                         "station_name": result["station_name"], "token": result["token"],
                         "fingerprint": manager.fingerprint, "server": info})

    def _native(self, manager, store, station, path, query, body):
        if path == "/lan/v1/ping":
            self._method("GET")
            self._json(200, {"ok": True, "api": LAN_API, "version": paths.source_version(), "station": station,
                             "server_time": int(time.time()), "generation": _generation(store.meta())})
            return
        if path.startswith("/lan/v1/replica/"):
            self._replica(store, station, path, query, body)
            return
        try:
            if path == "/lan/v1/print/next":
                self._method("POST")
                with store.station(station["id"]):
                    job = store.begin_print()
                self._json(200, {"job": None if not job else {
                    "id": job["id"], "kind": job.get("kind"), "driver": job.get("driver"),
                    "target": job.get("target"), "attempts": job.get("attempts"),
                    "payload_b64": base64.b64encode(bytes(job.get("payload") or b"")).decode("ascii")}})
                return
            done = _JOB_DONE.fullmatch(path)
            if done:
                self._method("POST")
                error = self._body_object(body).get("error")
                with store.station(station["id"]):
                    store.finish_print(done.group(1), error=_clean(str(error), 300) if error else None)
                self._json(200, {"ok": True})
                return
            if path == "/lan/v1/print/recover":
                self._method("POST")
                with store.station(station["id"]):
                    recovered = store.recover_print()
                self._json(200, {"recovered": int(recovered or 0)})
                return
        except _Refused:
            raise
        except Exception as exc:
            raise self._store_error(exc, "print_error") from None
        raise _Refused(404, "Rota não encontrada.", "not_found")

    def _replica(self, store, station, path, query, body):
        if replica_net is None:
            raise _Refused(404, "Cópia de segurança pela rede indisponível nesta instalação", "replica_unavailable")
        values = {key: items[0] for key, items in parse_qs(query, keep_blank_values=True).items()}
        if body:
            # The contract signature has no body: the fields of a JSON object go with the query.
            try:
                fields = json.loads(body.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                fields = None
            if isinstance(fields, dict):
                for key, value in fields.items():
                    values.setdefault(key, value)
        headers = _Headers((name, value) for name, value in self.headers.items()
                           if name.lower() != "x-pdv-station-token")
        extra = {}
        try:
            parameters = inspect.signature(replica_net.handle_request).parameters
            if "body" in parameters or any(item.kind is item.VAR_KEYWORD for item in parameters.values()):
                extra["body"] = body
        except (TypeError, ValueError):
            pass
        status, out_headers, content = replica_net.handle_request(store, dict(station), self.command, path,
                                                                  values, headers, **extra)
        out = [(name, value) for name, value in dict(out_headers or {}).items()
               if name.lower() not in HOP_BY_HOP and name.lower() != "content-length"]
        if isinstance(content, (bytes, bytearray)):
            self._send(status, bytes(content), out)
            return
        length = next((value for name, value in dict(out_headers or {}).items()
                       if name.lower() == "content-length"), None)
        self.send_response(status)
        for name, value in out:
            self.send_header(name, str(value))
        if length is not None:
            self.send_header("Content-Length", str(length))
        else:
            self.send_header("Connection", "close")
        self.end_headers()
        try:
            for chunk in content or ():
                if chunk and self.command != "HEAD":
                    self.wfile.write(chunk)
        except Exception:
            self.close_connection = True  # a half-sent body cannot be repaired
            raise OSError("cópia interrompida") from None
        finally:
            close = getattr(content, "close", None)
            if close:
                close()

    # -- the Flask app, in process
    def _environ(self, station, path, query, body):
        environ = {
            "REQUEST_METHOD": self.command, "SCRIPT_NAME": "", "PATH_INFO": path, "QUERY_STRING": query,
            # Without a Host header the app must refuse: never a loopback default.
            "SERVER_NAME": "pdv2.lan", "SERVER_PORT": str(self.server.server_address[1]),
            "SERVER_PROTOCOL": "HTTP/1.1", "REMOTE_ADDR": self.client_address[0],
            "CONTENT_LENGTH": str(len(body)),
            "wsgi.version": (1, 0), "wsgi.url_scheme": "http", "wsgi.input": io.BytesIO(body),
            "wsgi.errors": sys.stderr, "wsgi.multithread": True, "wsgi.multiprocess": False,
            "wsgi.run_once": False,
            "pdv2.station": {"id": station["id"], "name": station["name"],
                             "version": _clean(self.headers.get("X-PDV-Station-Version"), 20)},
        }
        if self.headers.get("Content-Type"):
            environ["CONTENT_TYPE"] = self.headers.get("Content-Type")
        collected = {}
        for name, value in self.headers.items():
            lower = name.lower()
            if ("_" in name or lower in HOP_BY_HOP or lower in ("content-type", "content-length", "forwarded")
                    or lower.startswith(("x-pdv-station", "x-forwarded", "x-pdv-lan"))):
                continue
            collected.setdefault(lower, []).append(value)
        for lower, items in collected.items():
            environ["HTTP_" + lower.upper().replace("-", "_")] = ("; " if lower == "cookie" else ", ").join(items)
        return environ

    def _bridge(self, manager, station, path, query, body):
        state = {}
        written = []

        def start_response(status, headers, exc_info=None):
            state["status"], state["headers"] = status, list(headers)
            return written.append

        iterable = manager.app(self._environ(station, path, query, body), start_response)
        try:
            chunks = list(iterable)
        finally:
            close = getattr(iterable, "close", None)
            if close:
                close()
        content = b"".join(written + chunks)
        code_text, _space, reason = state["status"].partition(" ")
        code = int(code_text)
        self.send_response_only(code, reason or None)
        declared = None
        for name, value in state["headers"]:
            lower = name.lower()
            if lower == "content-length":
                declared = value
            elif lower not in HOP_BY_HOP and not lower.startswith("x-pdv-lan"):
                self.send_header(name, value)  # one line each: two Set-Cookie stay two
        if self.command == "HEAD":
            self.send_header("Content-Length", declared if declared is not None else str(len(content)))
            content = b""
        elif code >= 200 and code not in (204, 304):
            self.send_header("Content-Length", str(len(content)))
        else:
            content = b""
        self.end_headers()
        if content:
            self.wfile.write(content)


# ----------------------------------------------------------------- listener

def _drain_request(sock):
    """Reads one request (headers and declared body) and throws it away. Best effort."""
    data = b""
    try:
        while b"\r\n\r\n" not in data and len(data) < 65536:
            chunk = sock.recv(8192)
            if not chunk:
                return
            data += chunk
        head, _mark, rest = data.partition(b"\r\n\r\n")
        found = re.search(rb"(?im)^content-length:[ \t]*(\d{1,9})[ \t]*\r?$", head)
        remaining = min(int(found.group(1)) if found else 0, BUSY_DRAIN_BYTES) - len(rest)
        while remaining > 0:
            chunk = sock.recv(min(65536, remaining))
            if not chunk:
                return
            remaining -= len(chunk)
    except (OSError, ssl.SSLError, ValueError):
        return


class _Listener(ThreadingHTTPServer):
    daemon_threads = True
    # On Windows SO_REUSEADDR would let a second process take the same port in silence.
    allow_reuse_address = os.name != "nt"
    request_queue_size = 64

    def __init__(self, address, manager):
        self.manager = manager
        self._guard = threading.Lock()
        self._count = 0
        self._sockets = set()
        super().__init__(address, _Handler)

    def process_request(self, request, client_address):
        # Accept thread: nothing slow here. The TLS handshake runs in the connection thread.
        with self._guard:
            limit = self.manager.connection_limit
            refuse = self._count >= 2 * limit
            if not refuse:
                self._count += 1
                over = self._count > limit
                self._sockets.add(request)
        if refuse:
            self.shutdown_request(request)
            return
        threading.Thread(target=self._serve, args=(request, client_address, over), name="pdv-lan-conn",
                         daemon=True).start()

    def _serve(self, request, client_address, over):
        tls = None
        try:
            request.settimeout(3 if over else HANDSHAKE_SECONDS)
            try:
                request.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            except OSError:
                pass
            tls = self.manager.context.wrap_socket(request, server_side=True)
            with self._guard:
                self._sockets.discard(request)
                self._sockets.add(tls)
            if over:
                # Read the request first: closing with unread data becomes a reset, and the
                # station would take "busy" for "sent and lost".
                _drain_request(tls)
                tls.sendall(b"HTTP/1.1 503 Service Unavailable\r\nContent-Type: application/json; charset=utf-8\r\n"
                            b"X-PDV-Lan-Error: server_busy\r\nConnection: close\r\nContent-Length: "
                            + str(len(_BUSY)).encode("ascii") + b"\r\n\r\n" + _BUSY)
                return
            tls.settimeout(IDLE_SECONDS)
            _Handler(tls, client_address, self)
        except (OSError, ssl.SSLError):
            pass  # scanner, wrong protocol or a cable pulled: nothing to report
        except Exception as exc:
            log.error("Falha na conexão de um caixa adicional: %s", type(exc).__name__)
        finally:
            with self._guard:
                self._count -= 1
                self._sockets.discard(request)
                self._sockets.discard(tls)
            for item in (tls, request):
                if item is not None:
                    try:
                        item.close()
                    except OSError:
                        pass

    def drop_connections(self):
        with self._guard:
            sockets = list(self._sockets)
        for item in sockets:
            try:
                item.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                item.close()
            except OSError:
                pass


# ------------------------------------------------------------------ manager

class LanServer:
    """Opens and closes the listener as the shop gains or loses the right to stations."""

    def __init__(self, app, host=None, port=None):
        self.app = app
        self.host = host or os.environ.get("PDV_LAN_HOST") or "0.0.0.0"
        self.port = default_port() if port is None else int(port)
        self.discovery_port = None
        self.context = None
        self.fingerprint = None
        self.error = ""
        self.firewall = "unknown"
        self._listener = None
        self._thread = None
        self._responder = None
        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._supervisor = None
        self._firewall_checked = False
        self._limits = threading.Lock()
        self._bad_tokens = {}
        self._blocked = {}
        self._enrolls = {}
        self._touched = {}
        self.connection_limit = MAX_CONNECTIONS
        self.superseded = False
        self.discover = app.config.get("LAN_DISCOVER") or (
            None if app.config.get("TESTING") else discovery.discover)
        self.client_factory = app.config.get("LAN_CLIENT_FACTORY") or lanclient.LanClient

    # -- state
    @property
    def running(self):
        return self._listener is not None

    def _store(self):
        return self.app.extensions.get("terminal_store")

    def _wanted(self):
        store = self._store()
        if self._stop.is_set() or disabled():
            return False
        if store is None:
            # Damaged database on a computer that already served stations: keep answering, so the
            # stations say "the main cashier is damaged" instead of "it is off" (and never offer
            # to take over while this computer is on, showing the restore screen).
            folder = _data_dir(self.app) / "lan"
            return bool(self.app.extensions.get("terminal_damage")
                        and (folder / "cert.pem").is_file() and (folder / "key.pem").is_file())
        try:
            if not store.identity().get("terminal_id"):
                return False
            if int(store.station_limit().get("limit") or 0) > 0:
                return True
            return any(not item.get("is_server") for item in store.stations())
        except Exception:
            return False  # a store without stations (older core) never opens the port

    def _connection_limit(self):
        """Room for every caixa of the plan (and the ones already linked) in the busiest hour."""
        store = self._store()
        stations = 0
        if store is not None:
            try:
                stations = max(int(store.station_limit().get("limit") or 0),
                               sum(1 for item in store.stations() if not item.get("is_server")))
            except Exception:
                stations = 0
        return min(max(MAX_CONNECTIONS, CONNECTIONS_PER_STATION * (stations + 1)), MAX_CONNECTIONS_CEILING)

    def evaluate(self):
        with self._lock:
            wanted = self._wanted()
            self.connection_limit = self._connection_limit()
            if wanted and not self.running:
                self._open()
            elif not wanted and self.running:
                self._close()
            return self.running

    def _open(self):
        try:
            store = self._store()
            if store is None:
                # Damaged: only the certificate the stations already know (never a new identity).
                folder = _data_dir(self.app) / "lan"
                certificate = {"cert_path": folder / "cert.pem", "key_path": folder / "key.pem",
                               "fingerprint": fingerprint((folder / "cert.pem").read_bytes())}
            else:
                certificate = ensure_certificate(_data_dir(self.app), store.identity().get("shop_id"))
                try:
                    self.superseded = bool(store.superseded())
                except Exception:
                    self.superseded = False
            self.context = _load_context(certificate["cert_path"], certificate["key_path"])
            self.fingerprint = certificate["fingerprint"]
            listener = _Listener((self.host, self.port), self)
        except OSError as exc:
            self.error = (f"Não foi possível abrir a rede entre caixas (a porta {self.port} está em uso por outro "
                          "programa)" if isinstance(exc, OSError) and getattr(exc, "errno", None) in _BUSY_ERRNOS
                          else "Não foi possível abrir a rede entre caixas neste computador")
            log.error("%s: %s", self.error, type(exc).__name__)
            return
        except Exception as exc:
            self.error = "Não foi possível abrir a rede entre caixas neste computador"
            log.error("%s: %s", self.error, type(exc).__name__)
            return
        self.port = listener.server_address[1]
        self._listener = listener
        self._thread = threading.Thread(target=listener.serve_forever, kwargs={"poll_interval": 0.2},
                                        name="pdv-lan-accept", daemon=True)
        self._thread.start()
        self.error = ""
        try:
            self._responder = discovery.start_responder(self._discovery_info, port=self.discovery_port)
            self.discovery_port = self._responder.port
        except OSError:
            self._responder = None  # stations still connect by address
            log.warning("Busca automática entre caixas indisponível (porta em uso)")
        log.info("Rede entre caixas ligada na porta %s", self.port)

    def _close(self):
        listener, self._listener = self._listener, None
        responder, self._responder = self._responder, None
        if responder is not None:
            responder.stop()
        if listener is not None:
            listener.shutdown()
            listener.server_close()
            listener.drop_connections()
            log.info("Rede entre caixas desligada")

    def start(self):
        self.evaluate()
        self._supervisor = threading.Thread(target=self._supervise, name="pdv-lan", daemon=True)
        self._supervisor.start()
        return self

    def _supervise(self):
        while not self._stop.is_set():
            if self.running and not self._firewall_checked:
                self._firewall_checked = True
                self.firewall = ensure_firewall(self.port, self.discovery_port or discovery.PORT)
            try:
                self.check_superseded()
            except Exception as exc:
                log.error("Falha ao procurar outro caixa principal na rede: %s", type(exc).__name__)
            self._wake.wait(REEVALUATE_SECONDS)
            self._wake.clear()
            if self._stop.is_set():
                break
            try:
                self.evaluate()
            except Exception as exc:
                log.error("Falha ao reavaliar a rede entre caixas: %s", type(exc).__name__)

    def check_superseded(self):
        """Looks for a NEWER generation of this same terminal on the network (a cashier that took
        over while this computer was off). Finding one, this computer stops selling for good.

        The discovery answer is only a hint: the other side must prove, over TLS with the
        certificate it announced, that it holds the key of the shop copy. Returns True when
        this computer is (now or already) superseded.
        """
        store = self._store()
        if self.superseded or store is None or not self.running or self.discover is None:
            return self.superseded
        identity = store.identity()
        terminal_id, shop_id = identity.get("terminal_id"), identity.get("shop_id")
        key = _copy_key(store)
        if not terminal_id or not shop_id or key is None:
            return False
        own = _generation(store.meta())
        for item in self.discover(shop_id) or []:
            if not isinstance(item, dict):
                continue
            generation, presented = item.get("generation"), item.get("fingerprint")
            if (item.get("terminal_id") != terminal_id or presented == self.fingerprint
                    or isinstance(generation, bool) or not isinstance(generation, int) or generation <= own):
                continue
            proven = prove_generation(self.client_factory, item.get("address"), item.get("port"), presented,
                                      key, terminal_id, own)
            if proven is None:
                continue
            try:
                store.mark_superseded(proven, presented)
            except Exception as exc:
                log.error("Não foi possível registrar que outro computador assumiu: %s", type(exc).__name__)
                continue
            self.superseded = True
            log.error("Outro computador assumiu como caixa principal desta loja; este parou de vender")
            return True
        return False

    def poke(self):
        try:
            self.evaluate()
        except Exception as exc:
            log.error("Falha ao reavaliar a rede entre caixas: %s", type(exc).__name__)
        self._wake.set()

    def stop(self, timeout=5.0):
        self._stop.set()
        self._wake.set()
        with self._lock:
            self._close()
        if self._supervisor is not None and self._supervisor is not threading.current_thread():
            self._supervisor.join(timeout=timeout)
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    # -- descriptions
    def server_info(self, store):
        info = store.connection_info()
        return {"shop_id": info.get("shop_id"), "shop_name": info.get("shop_name"),
                "tenant_name": info.get("tenant_name"), "terminal_id": info.get("terminal_id"),
                "terminal_name": info.get("terminal_name"), "host": socket.gethostname(), "port": self.port,
                "generation": _generation(store.meta())}

    def hello(self, store, challenge=None):
        info = self.server_info(store)
        proof = None
        if challenge:
            proof = lanclient.generation_proof(_copy_key(store), challenge, info.get("terminal_id"),
                                               info["generation"], self.fingerprint)
        if proof:
            info["proof"] = proof
        try:
            enroll_open = int(store.station_limit().get("limit") or 0) > 0
        except Exception:
            enroll_open = False
        info.pop("tenant_name", None)
        return {"service": "pdv2-lan", "api": LAN_API, "version": paths.source_version(), **info,
                "fingerprint": self.fingerprint, "enroll_open": enroll_open}

    def _discovery_info(self):
        store = self._store()
        if store is None or not self.running or self.superseded:
            return None
        try:
            details = self.hello(store)
        except Exception:
            return None
        if not details.get("shop_id"):
            return None
        details.pop("service", None)
        details.pop("enroll_open", None)
        return details

    # -- limits
    def blocked(self, address):
        with self._limits:
            until = self._blocked.get(address)
            if until is None:
                return False
            if time.monotonic() >= until:
                del self._blocked[address]
                return False
            return True

    def bad_token(self, address):
        """Counts one invalid token. True when this address is now blocked."""
        now = time.monotonic()
        with self._limits:
            moments = [moment for moment in self._bad_tokens.get(address, ()) if now - moment < BAD_TOKEN_WINDOW_SECONDS]
            moments.append(now)
            self._bad_tokens[address] = moments
            if len(self._bad_tokens) > 1024:
                self._bad_tokens = {address: moments}
            if len(moments) > BAD_TOKEN_LIMIT:
                self._blocked[address] = now + BAD_TOKEN_BLOCK_SECONDS
                self._bad_tokens.pop(address, None)
                return True
            return False

    def enroll_attempt(self, address):
        """False from the 11th attempt of one address within 10 minutes."""
        now = time.monotonic()
        with self._limits:
            moments = [moment for moment in self._enrolls.get(address, ()) if now - moment < ENROLL_WINDOW_SECONDS]
            allowed = len(moments) < ENROLL_LIMIT
            if allowed:
                moments.append(now)
            self._enrolls[address] = moments
            if len(self._enrolls) > 1024:
                self._enrolls = {address: moments}
            return allowed

    def touch(self, store, station_id, address, version, hostname):
        now = time.monotonic()
        with self._limits:
            last = self._touched.get(station_id)
            if last is not None and now - last < TOUCH_SECONDS:
                return
            self._touched[station_id] = now
        try:
            store.touch_station(station_id, address=address, app_version=version, hostname=hostname)
        except Exception as exc:
            log.warning("Não foi possível registrar a presença de um caixa: %s", type(exc).__name__)


# ---------------------------------------------------------------- interface

def start(app, *, host=None, port=None):
    """Starts the supervisor of the listener (and of discovery). Never raises for a busy port."""
    previous = app.extensions.get("terminal_lan")
    if previous is not None:
        previous.stop()
    server = LanServer(app, host, port)
    app.extensions["terminal_lan"] = server
    return server.start()


def poke(app):
    server = app.extensions.get("terminal_lan")
    if server is not None:
        server.poke()


def status(app):
    server = app.extensions.get("terminal_lan")
    result = {"supported": not disabled(), "running": False, "port": default_port() or DEFAULT_PORT,
              "addresses": discovery.local_addresses(), "hostname": socket.gethostname(), "fingerprint": None,
              "network_profile": discovery.network_profile(), "firewall": "unknown", "error": ""}
    if server is None:
        return result
    result.update(running=server.running, port=server.port, fingerprint=server.fingerprint,
                  firewall=server.firewall, error=server.error, superseded=server.superseded)
    if result["fingerprint"] is None:
        try:
            result["fingerprint"] = fingerprint((_data_dir(app) / "lan" / "cert.pem").read_bytes())
        except (OSError, ValueError):
            pass
    return result
