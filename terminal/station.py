"""Caixa adicional (station): a local bridge to the shop server.

The browser of a station still talks only to 127.0.0.1:8010. This small Flask app
answers `/health` and everything under `/station/` by itself and forwards the rest,
untouched, to the caixa principal over the pinned TLS connection of `lanclient`.
A station never opens a database: with the server out of reach it stops selling,
explains why and offers the assisted promotion.

The station token lives in <data_dir>/station.json (0600) and never reaches a log.
"""

import base64
import json
import logging
import os
import re
import socket
import threading
import time
from pathlib import Path
from urllib.parse import quote, urlparse

from flask import Flask, Response, jsonify, redirect, render_template, request, send_from_directory
from flask.sessions import SessionInterface
from werkzeug.datastructures import Headers

from . import discovery, lanclient, paths, printers, runtime
from .lanclient import LanClient, LanError

try:  # network replica and promotion belong to another module and may be absent
    from . import replica_net
except ImportError:
    replica_net = None
try:
    from . import promotion
except ImportError:
    promotion = None

STATE_FILE = "station.json"
PREVIOUS_FILE = "station-anterior.json"
PRINT_PENDING_FILE = "station-print-pending.json"
DEFAULT_LAN_PORT = 8011
MAX_BODY = 256 * 1024
BLOCK = 64 * 1024
CONNECT_SECONDS = 3.0
READ_SECONDS = 30.0
SLOW_READ_SECONDS = 75.0
SLOW_ROUTES = ("/api/sync/now", "/api/license/renew")
PING_UP_SECONDS = 10.0
PING_DOWN_SECONDS = 2.0
PRINT_SECONDS = 2.0
REDISCOVER_SECONDS = 10.0
PROMOTE_AFTER_SECONDS = 60.0
ALL_METHODS = ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]
HOP_BY_HOP = frozenset(("connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te",
                        "trailer", "transfer-encoding", "upgrade", "proxy-connection"))
LOCAL_STATIC = ("station.js", "station.css", "favicon.svg", "theme.js")
_FINGERPRINT = re.compile(r"[0-9a-f]{64}")
_ADDRESS = re.compile(r"[A-Za-z0-9._:\-\[\]]{1,255}")

UNREACHABLE_TEXT = "O caixa principal está fora do ar. Este caixa volta sozinho quando a conexão voltar."
TIMEOUT_TEXT = ("O caixa principal não respondeu a tempo. Confira na lista de vendas se a operação foi "
                "concluída antes de repetir.")
REFUSAL_TEXTS = {
    "station_revoked": "Este caixa foi removido do caixa principal.",
    "station_unauthorized": "Este caixa não está vinculado ao caixa principal.",
    "station_outdated": "Atualize este caixa: a versão do PDV2 dele é antiga.",
    "server_outdated": "Atualize o caixa principal: a versão do PDV2 dele é antiga.",
}
NOT_FOUND_TEXT = ("Não encontrei o caixa principal neste endereço. Confira o número e se o computador "
                  "principal está ligado.")
PROMOTION_UNAVAILABLE = "Assumir como caixa principal não está disponível nesta instalação."
log = logging.getLogger("pdv2.station")


def _now_ms():
    return int(time.time() * 1000)


def default_lan_port(env=None):
    env = os.environ if env is None else env
    try:
        port = int(env.get("PDV_LAN_PORT") or DEFAULT_LAN_PORT)
    except ValueError:
        return DEFAULT_LAN_PORT
    return port if 0 < port < 65536 else DEFAULT_LAN_PORT


# ------------------------------------------------------------------- files

def _read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return None


def _write_private_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.unlink(missing_ok=True)
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(json.dumps(value, ensure_ascii=False))
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def _valid_state(value):
    if not isinstance(value, dict) or not isinstance(value.get("station_id"), str) or not value["station_id"]:
        return False
    server = value.get("server")
    if not isinstance(value.get("token"), str) or not value["token"] or not isinstance(server, dict):
        return False
    port = server.get("port")
    return (isinstance(server.get("address"), str) and bool(server["address"])
            and isinstance(port, int) and not isinstance(port, bool) and 0 < port < 65536
            and isinstance(server.get("fingerprint"), str) and bool(_FINGERPRINT.fullmatch(server["fingerprint"])))


def load_state(data_dir):
    """Content of station.json, or None when this station is not linked."""
    value = _read_json(Path(data_dir) / STATE_FILE)
    return value if _valid_state(value) else None


def save_state(data_dir, state):
    if not _valid_state(state):
        raise ValueError("Estado de caixa adicional inválido")
    _write_private_json(Path(data_dir) / STATE_FILE, state)


def load_previous(data_dir):
    value = _read_json(Path(data_dir) / PREVIOUS_FILE)
    return value if _valid_state(value) else None


class _NoSession(SessionInterface):
    """The bridge never owns a cookie: the session belongs to the app of the server."""

    def open_session(self, app, request):  # noqa: ARG002
        return self.make_null_session(app)

    def save_session(self, app, session, response):  # noqa: ARG002
        return None


# ------------------------------------------------------------------ station

class Station:
    def __init__(self, data_dir, config=None):
        config = config or {}
        self.data_dir = Path(data_dir)
        self.client_factory = config.get("CLIENT_FACTORY") or LanClient
        self.discover = config.get("DISCOVER") or (lambda shop_id=None: discovery.discover(shop_id))
        self.fingerprint_of = config.get("PEER_FINGERPRINT") or lanclient.peer_fingerprint
        self.send_bytes = config.get("SEND_BYTES")
        self.ping_up = float(config.get("PING_UP_SECONDS", PING_UP_SECONDS))
        self.ping_down = float(config.get("PING_DOWN_SECONDS", PING_DOWN_SECONDS))
        self.print_seconds = float(config.get("PRINT_SECONDS", PRINT_SECONDS))
        self.rediscover_seconds = float(config.get("REDISCOVER_SECONDS", REDISCOVER_SECONDS))
        self.promote_after = float(config.get("PROMOTE_AFTER_SECONDS", PROMOTE_AFTER_SECONDS))
        self.lock = threading.RLock()
        self.state = load_state(self.data_dir)
        self.status = "down" if self.state else "unlinked"
        self.reason = ""
        self.last_ok_at = None
        self.down_since = _now_ms() if self.state else None
        self._down_mono = time.monotonic() if self.state else None
        self.error = ""
        self.error_kind = ""
        self.ip_change = None
        self.relink = None
        self.promoting = False
        self.promoted = False
        self.promotion_error = ""
        self._client = None
        self._last_discover = 0.0
        self._stop = threading.Event()
        self._monitor_wake = threading.Event()
        self._print_wake = threading.Event()
        self._threads = []
        self._puller = None
        self._recovered = False
        self._print_lock = threading.Lock()

    # -- identity
    @property
    def linked(self):
        return self.state is not None

    def identity_headers(self):
        return [("X-PDV-Station-Version", paths.source_version()),
                ("X-PDV-Station-Host", socket.gethostname()[:64])]

    def client(self):
        with self.lock:
            if self.state is None:
                raise LanError("unreachable", "sem vínculo")
            if self._client is None:
                server = self.state["server"]
                self._client = self.client_factory(server["address"], server["port"], server["fingerprint"],
                                                   self.state["token"], timeout=READ_SECONDS)
            return self._client

    def _reset_client(self):
        with self.lock:
            client, self._client = self._client, None
        if client is not None:
            try:
                client.close()
            except OSError:
                pass

    # -- status transitions
    def mark_up(self):
        with self.lock:
            if self.status in ("unlinked", "revoked"):
                return
            self.status, self.reason = "up", ""
            self.last_ok_at = _now_ms()
            self.down_since = self._down_mono = None
            self.error = self.error_kind = ""
            self.relink = None

    def mark_down(self, exc=None):
        with self.lock:
            if exc is not None:
                self.error_kind = getattr(exc, "kind", "") or ("identity" if getattr(
                    exc, "code", "") == "fingerprint_mismatch" else "")
                self.error = {"refused": "conexão recusada", "timeout": "sem resposta",
                              "identity": "outra identidade"}.get(self.error_kind, "sem conexão")
            if self.status in ("unlinked", "revoked"):
                return
            if self.down_since is None:
                self.down_since, self._down_mono = _now_ms(), time.monotonic()
            if self.status != "relink":
                self.status, self.reason = "down", ""

    def note_response(self, response):
        """Reads the refusal of the LAN layer, if any. Returns its code or None."""
        code = response.headers.get("x-pdv-lan-error")
        with self.lock:
            if code in ("station_revoked", "station_unauthorized"):
                if self.status != "unlinked":
                    self.status, self.reason = "revoked", code
            elif code in ("station_outdated", "server_outdated"):
                if self.status not in ("unlinked", "revoked"):
                    self.status, self.reason = "incompatible", code
                    self.down_since = self._down_mono = None
            elif code != "server_busy" and response.status < 500:
                self.mark_up()
        return code

    def can_promote(self):
        with self.lock:
            return bool(promotion is not None and self.status == "down" and not self.promoted
                        and self._down_mono is not None
                        and time.monotonic() - self._down_mono >= self.promote_after)

    # -- requests of the background threads
    def lan_request(self, method, path, body=None, timeout=10.0):
        try:
            response = self.client().request(method, path, body=body, headers=self.identity_headers(),
                                             timeout=timeout)
        except LanError as exc:
            self.mark_down(exc)
            raise
        self.note_response(response)
        return response

    def lan_json(self, method, path, body=None, timeout=10.0):
        """(status, parsed body or None)."""
        response = self.lan_request(method, path, body, timeout)
        try:
            try:
                return response.status, response.json()
            except LanError as exc:
                self.mark_down(exc)
                raise
        finally:
            response.close()

    def server_alive(self):
        """Fresh contact, outside the connection pool: True when the server answers anything."""
        with self.lock:
            if self.state is None:
                return False
            server, token = dict(self.state["server"]), self.state["token"]
        client = self.client_factory(server["address"], server["port"], server["fingerprint"], token,
                                     timeout=CONNECT_SECONDS)
        try:
            client.request("GET", "/lan/v1/ping", headers=self.identity_headers(), timeout=CONNECT_SECONDS).close()
            return True
        except OSError:
            return False
        finally:
            client.close()

    # -- monitor and rediscovery
    def check_once(self):
        """One round of the monitor. Returns the status afterwards."""
        with self.lock:
            if self.state is None or self.status == "revoked" or self.promoted:
                return self.status
        try:
            status, body = self.lan_json("GET", "/lan/v1/ping", timeout=5.0)
            if status == 200 and isinstance(body, dict):
                self._note_generation(body.get("generation"))
        except LanError as exc:
            self.rediscover(include_last=exc.code == "fingerprint_mismatch")
        return self.status

    def _note_generation(self, generation):
        with self.lock:
            if (self.state is not None and isinstance(generation, int)
                    and self.state["server"].get("generation") != generation):
                self.state["server"]["generation"] = generation
                self._save()

    def _save(self):
        try:
            save_state(self.data_dir, self.state)
        except (OSError, ValueError) as exc:
            log.error("Não foi possível gravar o vínculo deste caixa: %s", type(exc).__name__)

    def _hello_at(self, address, port, presented):
        """Public description of whoever answers there, with that certificate pinned. No token."""
        client = self.client_factory(address, port, presented, None, timeout=CONNECT_SECONDS)
        try:
            response = client.request("GET", "/lan/v1/hello", timeout=CONNECT_SECONDS)
            try:
                body = response.json() if response.status == 200 else None
            finally:
                response.close()
        except OSError:
            return None
        finally:
            client.close()
        if not isinstance(body, dict) or body.get("service") != "pdv2-lan" or body.get("fingerprint") != presented:
            return None
        return body

    def rediscover(self, include_last=False, force=False):
        """Looks for the server elsewhere. Returns "ip_changed", "relink" or None."""
        with self.lock:
            if self.state is None:
                return None
            server = dict(self.state["server"])
            known = list(self.state.get("addresses") or [])
        pinned, port = server["fingerprint"], server["port"]
        candidates = [(server["address"], port)] if include_last else []
        candidates += [(address, port) for address in known if address != server["address"]]
        now = time.monotonic()
        if force or now - self._last_discover >= self.rediscover_seconds:
            self._last_discover = now
            if server.get("host"):
                try:
                    for item in socket.getaddrinfo(server["host"], port, socket.AF_INET, socket.SOCK_STREAM):
                        candidates.append((item[4][0], port))
                except (OSError, UnicodeError):
                    pass
            try:
                for item in self.discover(server.get("shop_id")) or []:
                    candidates.append((item["address"], int(item.get("port") or port)))
            except Exception as exc:
                log.warning("Busca do caixa principal falhou: %s", type(exc).__name__)
        seen = set()
        for address, number in candidates:
            if (address, number) in seen or (not include_last and (address, number) == (server["address"], port)):
                continue
            seen.add((address, number))
            try:
                presented = self.fingerprint_of(address, number, timeout=2.0)
            except OSError:
                continue
            if presented == pinned:
                self._adopt_address(address, number)
                return "ip_changed"
            # Another certificate: ask who it is WITHOUT the token.
            other = self._hello_at(address, number, presented)
            if other and server.get("shop_id") and other.get("shop_id") == server["shop_id"]:
                with self.lock:
                    self.status, self.reason = "relink", ""
                    self.relink = {"host": other.get("host") or address, "address": address, "port": number,
                                   "fingerprint": presented, "shop_name": other.get("shop_name"),
                                   "terminal_name": other.get("terminal_name")}
                return "relink"
        return None

    def _adopt_address(self, address, port):
        with self.lock:
            server = self.state["server"]
            old = server["address"]
            if (old, server["port"]) != (address, port):
                self.ip_change = {"old": old, "new": address, "at": time.monotonic()}
            server["address"], server["port"] = address, port
            others = [item for item in self.state.get("addresses") or [] if item != address]
            self.state["addresses"] = [address, *others][:5]
            self._save()
        self._reset_client()
        try:
            self.lan_json("GET", "/lan/v1/ping", timeout=5.0)
        except LanError:
            pass

    def diagnosis(self):
        with self.lock:
            status, kind, change = self.status, self.error_kind, self.ip_change
            relink = self.relink
        items = []
        if status == "down":
            has_network = bool(discovery.local_addresses())
            items.append({"id": "network", "ok": has_network, "title": "Este computador está sem rede",
                          "help": "Confira o cabo de rede ou o Wi-Fi."})
            items.append({"id": "public_profile", "ok": discovery.network_profile() != "public",
                          "title": "A rede está marcada como Pública no Windows",
                          "help": "Em Configurações → Rede, mude para Particular neste computador e no caixa "
                                  "principal."})
            items.append({"id": "server_off", "ok": not has_network or kind == "timeout",
                          "title": "O computador principal está desligado ou com o PDV2 fechado",
                          "help": "Ligue o computador principal e abra o PDV2 nele."})
            items.append({"id": "blocked", "ok": not has_network or kind != "timeout",
                          "title": "O caixa principal não respondeu (rede bloqueada)",
                          "help": "Confira se os dois computadores estão na mesma rede e se o firewall permite "
                                  "o PDV2."})
        if change and time.monotonic() - change["at"] < 600:
            items.append({"id": "ip_changed", "ok": True, "help": "",
                          "title": f"O endereço do caixa principal mudou (era {change['old']}, agora "
                                   f"{change['new']}). Já foi corrigido."})
        if status == "relink" and relink:
            items.append({"id": "identity_changed", "ok": False, "title": "O caixa principal mudou de computador",
                          "help": "Peça ao gerente o código de 6 números em Sistema → Adicionar caixa e digite "
                                  "aqui."})
        return items

    def status_payload(self):
        with self.lock:
            state, status = self.state, self.status
            server = None
            if state is not None:
                details = state["server"]
                server = {"host": details.get("host"), "shop_name": details.get("shop_name"),
                          "terminal_name": details.get("terminal_name"), "address": details.get("address"),
                          "port": details.get("port"), "last_ok_at": self.last_ok_at,
                          "down_since": self.down_since, "error": self.error}
            payload = {"mode": "station", "linked": state is not None, "state": status,
                       "station": {"id": state["station_id"], "name": state.get("station_name") or ""}
                       if state is not None else None,
                       "server": server, "reason": self.reason,
                       "relink": dict(self.relink) if self.relink else None}
        payload["diagnosis"] = self.diagnosis()
        payload["replica"] = _guarded(lambda: replica_net.puller_status(self.data_dir)) if replica_net else None
        payload["promotion"] = _guarded(lambda: promotion.status(self.data_dir)) if promotion else None
        payload["can_promote"] = self.can_promote()
        payload["version"] = paths.source_version()
        return payload

    # -- linking
    def known_identity(self):
        """(shop_id, fingerprint, station_id) of the current or the previous link."""
        with self.lock:
            state = self.state
        state = state or load_previous(self.data_dir)
        if not state:
            return None, None, None
        return state["server"].get("shop_id"), state["server"].get("fingerprint"), state.get("station_id")

    def describe(self, item):
        shop_id, fingerprint, _station = self.known_identity()
        return {"address": item.get("address"), "port": item.get("port"), "host": item.get("host"),
                "shop_id": item.get("shop_id"), "shop_name": item.get("shop_name"),
                "terminal_name": item.get("terminal_name"), "fingerprint": item.get("fingerprint"),
                "api": item.get("api"), "version": item.get("version"), "generation": item.get("generation"),
                "same_shop": bool(shop_id) and item.get("shop_id") == shop_id,
                "known": bool(fingerprint) and item.get("fingerprint") == fingerprint}

    def probe(self, address, port):
        presented = self.fingerprint_of(address, port, timeout=CONNECT_SECONDS)
        body = self._hello_at(address, port, presented)
        if body is None:
            raise LanError("unreachable", "sem resposta")
        return self.describe({**body, "address": address, "port": port, "fingerprint": presented})

    def link(self, address, port, fingerprint, code, name):
        """(http status, body). Saves station.json when the server accepts the code."""
        client = self.client_factory(address, port, fingerprint, None, timeout=10.0)
        try:
            hello = client.request("GET", "/lan/v1/hello", timeout=5.0)
            try:
                described = hello.json() if hello.status == 200 else None
            finally:
                hello.close()
            shop_id, _fingerprint, previous_id = self.known_identity()
            same_shop = (isinstance(described, dict) and bool(shop_id) and described.get("shop_id") == shop_id)
            response = client.request("POST", "/lan/v1/enroll", body={
                "code": code, "name": name, "hostname": socket.gethostname()[:64],
                "app_version": paths.source_version(), "station_id": previous_id if same_shop else None})
            try:
                status, body = response.status, response.json()
            finally:
                response.close()
        finally:
            client.close()
        if status != 200:
            body = body if isinstance(body, dict) else {}
            return status, {"error": body.get("error") or "O caixa principal recusou o vínculo.",
                            "code": body.get("code")}
        server = body.get("server") if isinstance(body, dict) else None
        if (not isinstance(server, dict) or not isinstance(body.get("token"), str)
                or not isinstance(body.get("station_id"), str) or body.get("fingerprint") != fingerprint):
            return 409, {"error": "O caixa principal respondeu com outra identidade. Procure de novo.",
                         "code": "fingerprint_mismatch"}
        state = {"v": 1, "station_id": body["station_id"], "station_name": body.get("station_name") or name,
                 "token": body["token"],
                 "server": {"address": address, "port": port, "host": server.get("host"),
                            "fingerprint": fingerprint, "shop_id": server.get("shop_id"),
                            "shop_name": server.get("shop_name"), "tenant_name": server.get("tenant_name"),
                            "terminal_id": server.get("terminal_id"), "terminal_name": server.get("terminal_name"),
                            "generation": server.get("generation") or 1},
                 "addresses": [address], "linked_at": _now_ms()}
        save_state(self.data_dir, state)
        with self.lock:
            self.state = state
            self.status, self.reason = "up", ""
            self.last_ok_at = _now_ms()
            self.down_since = self._down_mono = None
            self.error = self.error_kind = ""
            self.relink = self.ip_change = None
            self._recovered = False
        self._reset_client()
        self._monitor_wake.set()
        self._print_wake.set()
        return 200, {"linked": True, "station": {"id": state["station_id"], "name": state["station_name"]},
                     "server": {key: value for key, value in state["server"].items() if key != "fingerprint"}}

    def unlink(self):
        with self.lock:
            if self.state is not None:
                os.replace(self.data_dir / STATE_FILE, self.data_dir / PREVIOUS_FILE)
            self.state = None
            self.status, self.reason = "unlinked", ""
            self.down_since = self._down_mono = self.last_ok_at = None
            self.relink = self.ip_change = None
        self._reset_client()

    # -- printing on THIS computer
    def _pending_path(self):
        return self.data_dir / PRINT_PENDING_FILE

    def pending_results(self):
        value = _read_json(self._pending_path())
        if not isinstance(value, list):
            return []
        return [item for item in value if isinstance(item, dict) and isinstance(item.get("id"), str)]

    def _save_pending(self, items):
        try:
            if items:
                _write_private_json(self._pending_path(), items)
            else:
                self._pending_path().unlink(missing_ok=True)
        except OSError as exc:
            log.error("Não foi possível gravar o resultado de impressão pendente: %s", type(exc).__name__)

    def _confirm(self, job_id, error):
        try:
            status, _body = self.lan_json("POST", f"/lan/v1/print/{quote(job_id, safe='')}/done", {"error": error})
        except LanError:
            return False
        # A refusal that will never change (unknown job) is not worth repeating forever.
        return status == 200 or status in (400, 404)

    def print_once(self):
        """One round of the print agent. Returns how many jobs were sent to the printer."""
        with self._print_lock:
            with self.lock:
                if self.state is None or self.status == "revoked" or self.promoted:
                    return 0
            if not self._recovered:
                status, _body = self.lan_json("POST", "/lan/v1/print/recover", {})
                if status != 200:
                    return 0
                self._recovered = True
            pending = self.pending_results()
            if pending:
                left = [item for item in pending if not self._confirm(item["id"], item.get("error"))]
                self._save_pending(left)
                if left:
                    return 0
            printed = 0
            while printed < 20 and not self._stop.is_set():
                status, body = self.lan_json("POST", "/lan/v1/print/next", {})
                job = body.get("job") if status == 200 and isinstance(body, dict) else None
                if not isinstance(job, dict) or not isinstance(job.get("id"), str):
                    break
                error = None
                try:
                    payload = base64.b64decode(job.get("payload_b64") or "", validate=True)
                    (self.send_bytes or printers.send_bytes)(job.get("driver"), job.get("target"), payload, job["id"])
                except Exception as exc:
                    error = (str(exc) or type(exc).__name__)[:300]
                printed += 1
                if not self._confirm(job["id"], error):
                    self._save_pending(self.pending_results() + [{"id": job["id"], "error": error}])
                    break
            return printed

    def wake_print(self):
        self._print_wake.set()

    def wake_monitor(self):
        self._monitor_wake.set()

    # -- promotion
    def start_promotion(self, pin):
        with self.lock:
            if self.promoting or self.promoted:
                return False
            self.promoting, self.promotion_error = True, ""
            station_id, name = self.state["station_id"], self.state.get("station_name") or ""
        threading.Thread(target=self._promote, args=(pin, station_id, name), name="pdv-promotion",
                         daemon=True).start()
        return True

    def _promote(self, pin, station_id, name):
        try:
            promotion.promote(self.data_dir, pin, station_id=station_id, station_name=name)
        except Exception as exc:
            with self.lock:
                self.promoting = False
                self.promotion_error = (str(exc) if isinstance(getattr(exc, "status", None), int)
                                        else "Não foi possível assumir como caixa principal. Chame o suporte.")
            if not isinstance(getattr(exc, "status", None), int):
                log.error("Promoção falhou: %s", type(exc).__name__)
            return
        # From here on this computer IS the server: the mode goes first, the rest is tidying.
        try:
            paths.write_mode(self.data_dir, "server")
            with self.lock:
                self.promoted = True
                if (self.data_dir / STATE_FILE).exists():
                    os.replace(self.data_dir / STATE_FILE, self.data_dir / PREVIOUS_FILE)
            self.stop(timeout=3.0)
        except Exception as exc:
            log.error("Promoção concluída, mas a troca de modo falhou: %s", type(exc).__name__)
        finally:
            with self.lock:
                self.promoting = False
        log.info("Este computador assumiu como caixa principal; reiniciando")
        runtime.request_restart("promocao")

    def promotion_payload(self):
        payload = _guarded(lambda: promotion.status(self.data_dir)) if promotion else None
        if not isinstance(payload, dict):
            payload = {"available": False, "reason": PROMOTION_UNAVAILABLE, "state": "idle", "step": "",
                       "error": ""}
        with self.lock:
            if self.promotion_error and payload.get("state") != "running":
                payload = {**payload, "state": "failed", "error": payload.get("error") or self.promotion_error}
            if self.promoting and payload.get("state") in (None, "idle"):
                payload = {**payload, "state": "running"}
            payload["finished"] = self.promoted
            payload["down_seconds"] = (int(time.monotonic() - self._down_mono)
                                       if self._down_mono is not None else 0)
        payload["can_promote"] = self.can_promote()
        payload["wait_seconds"] = int(self.promote_after)
        return payload

    # -- background
    def _loop(self, action, wake, interval):
        while not self._stop.is_set():
            try:
                action()
            except OSError:
                pass  # network trouble: the status already says so
            except Exception as exc:
                log.error("Falha em segundo plano no caixa adicional: %s", type(exc).__name__)
            wake.wait(interval())
            wake.clear()

    def _replica_client(self):
        return self.client()

    def start(self):
        if self._threads:
            return
        self._stop.clear()
        for name, action, wake, interval in (
                ("pdv-station-monitor", self.check_once, self._monitor_wake,
                 lambda: self.ping_up if self.status == "up" else self.ping_down),
                ("pdv-station-print", lambda: self.status == "up" and self.print_once(), self._print_wake,
                 lambda: self.print_seconds)):
            thread = threading.Thread(target=self._loop, args=(action, wake, interval), name=name, daemon=True)
            thread.start()
            self._threads.append(thread)
        if replica_net is not None:
            try:
                self._puller = replica_net.start_puller(self.data_dir, self._replica_client)
            except Exception as exc:
                log.error("A cópia de segurança pela rede não iniciou: %s", type(exc).__name__)

    def stop(self, timeout=5.0):
        self._stop.set()
        self._monitor_wake.set()
        self._print_wake.set()
        threads, self._threads = self._threads, []
        for thread in threads:
            if thread is not threading.current_thread():
                thread.join(timeout=timeout)
        puller, self._puller = self._puller, None
        if puller is not None:
            try:
                puller.stop(timeout=timeout)
            except Exception as exc:
                log.error("A cópia de segurança pela rede não parou: %s", type(exc).__name__)
        self._reset_client()


def _guarded(call):
    try:
        return call()
    except Exception as exc:
        log.warning("Consulta de estado falhou: %s", type(exc).__name__)
        return None


# -------------------------------------------------------------------- app

def create_app(config=None):
    config = dict(config or {})
    data_dir = Path(config.get("DATA_DIR") or os.environ.get("PDV_TERMINAL_DATA") or paths.data_dir())
    data_dir.mkdir(parents=True, exist_ok=True)
    try:
        data_dir.chmod(0o700)
    except OSError:
        pass
    app = Flask(__name__, template_folder="templates", static_folder=None)
    app.config.update(MAX_CONTENT_LENGTH=MAX_BODY)
    app.config.update({key: value for key, value in config.items() if key.isupper()})
    app.session_interface = _NoSession()
    station = Station(data_dir, config)
    app.extensions["station"] = station
    app.extensions["terminal_data_dir"] = data_dir
    allowed_hosts = set(config.get("ALLOWED_HOSTS") or ("localhost", "127.0.0.1", "[::1]"))
    static_dir = Path(__file__).resolve().parent / "static"

    def refuse(status, message, code=None):
        payload = {"error": message}
        if code:
            payload["code"] = code
        return jsonify(payload), status

    @app.before_request
    def local_security():
        host = request.host.rsplit(":", 1)[0]
        if host not in allowed_hosts:
            return refuse(403, "Host não permitido")
        if request.path.startswith("/station/") and request.method not in ("GET", "HEAD", "OPTIONS"):
            origin = request.headers.get("Origin")
            if origin:
                parsed = urlparse(origin)
                if parsed.scheme not in ("http", "https") or parsed.netloc != request.host:
                    return refuse(403, "Origem não permitida")
            if request.headers.get("X-PDV-Station-Local") != "1":
                return refuse(403, "Origem não permitida")
        return None

    @app.errorhandler(413)
    def too_large(_exc):
        return refuse(413, "Conteúdo grande demais.", "too_large")

    def body():
        value = request.get_json(silent=True)
        if not isinstance(value, dict):
            return None
        return value

    def target_of(data):
        """(address, port) typed or chosen on the screen, or None."""
        address = data.get("address")
        port = data.get("port", default_lan_port())
        if (not isinstance(address, str) or not _ADDRESS.fullmatch(address.strip())
                or isinstance(port, bool) or not isinstance(port, int) or not 0 < port < 65536):
            return None
        return address.strip(), port

    # -- local routes
    @app.get("/health")
    def health():
        with station.lock:
            server = "unlinked" if not station.linked else ("up" if station.status == "up" else "down")
        return jsonify({"status": "ok", "service": "terminal-local", "mode": "station", "server": server})

    @app.get("/station/")
    def page():
        response = Response(render_template("station.html", version=paths.source_version()), mimetype="text/html")
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/station/static/<name>")
    def station_static(name):
        if name not in LOCAL_STATIC:
            return refuse(404, "Rota não encontrada")
        return send_from_directory(static_dir, name)

    @app.get("/station/api/status")
    def api_status():
        return jsonify(station.status_payload())

    @app.post("/station/api/discover")
    def api_discover():
        try:
            found = station.discover(None) or []
        except Exception as exc:
            log.warning("Busca do caixa principal falhou: %s", type(exc).__name__)
            found = []
        # "Procurar de novo": the monitor also looks everywhere on its next round.
        station._last_discover = 0.0
        station.wake_monitor()
        return jsonify({"servers": [station.describe(item) for item in found if isinstance(item, dict)]})

    @app.post("/station/api/probe")
    def api_probe():
        target = target_of(body() or {})
        if target is None:
            return refuse(404, NOT_FOUND_TEXT)
        try:
            return jsonify(station.probe(*target))
        except OSError:
            return refuse(404, NOT_FOUND_TEXT)

    @app.post("/station/api/link")
    def api_link():
        data = body() or {}
        target = target_of(data)
        fingerprint = data.get("fingerprint")
        code = "".join(ch for ch in str(data.get("code") or "") if ch.isdigit())
        name = data.get("name") if isinstance(data.get("name"), str) else ""
        if target is None or not isinstance(fingerprint, str) or not _FINGERPRINT.fullmatch(fingerprint):
            return refuse(400, "Escolha o caixa principal antes de conectar.")
        if len(code) != 6:
            return refuse(400, "Digite os 6 números do código.")
        try:
            status, payload = station.link(target[0], target[1], fingerprint, code, name.strip()[:40])
        except LanError as exc:
            if exc.code == "fingerprint_mismatch":
                return refuse(409, "O caixa principal respondeu com outra identidade. Procure de novo.",
                              "fingerprint_mismatch")
            return refuse(502, "Não consegui falar com o caixa principal. Confira a rede e tente de novo.",
                          "server_unreachable")
        except (OSError, ValueError):
            return refuse(500, "Não foi possível gravar o vínculo neste computador.")
        return jsonify(payload), status

    @app.post("/station/api/unlink")
    def api_unlink():
        if (body() or {}).get("confirm") is not True:
            return refuse(400, "Confirme para desvincular este caixa.")
        try:
            station.unlink()
        except OSError:
            return refuse(500, "Não foi possível desvincular este caixa.")
        return jsonify({"linked": False})

    @app.post("/station/api/mode/server")
    def api_mode_server():
        if (body() or {}).get("confirm") is not True:
            return refuse(400, "Confirme para voltar ao modo de caixa principal.")
        if station.linked:
            return refuse(409, "Este caixa está ligado a um caixa principal. Desvincule antes de mudar.")
        try:
            paths.write_mode(data_dir, "server")
        except OSError:
            return refuse(500, "Não foi possível mudar o modo deste computador.")
        return jsonify({"mode": "server", "restart": bool(runtime.request_restart("modo-servidor"))})

    @app.get("/station/api/promotion")
    def api_promotion():
        return jsonify(station.promotion_payload())

    def promotion_error(exc):
        status = getattr(exc, "status", None)
        if isinstance(status, bool) or not isinstance(status, int):
            log.error("Promoção: falha inesperada: %s", type(exc).__name__)
            return refuse(500, "Não foi possível conferir a cópia deste caixa. Chame o suporte.")
        return refuse(status, str(exc), getattr(exc, "code", None))

    @app.post("/station/api/promotion/verify")
    def api_promotion_verify():
        if promotion is None:
            return refuse(409, PROMOTION_UNAVAILABLE)
        try:
            manager = promotion.verify_manager(data_dir, str((body() or {}).get("manager_pin") or ""))
        except Exception as exc:
            return promotion_error(exc)
        return jsonify({"ok": True, "manager": manager.get("name") or ""})

    @app.post("/station/api/promotion/start")
    def api_promotion_start():
        data = body() or {}
        if promotion is None:
            return refuse(409, PROMOTION_UNAVAILABLE)
        if data.get("confirm") is not True or not data.get("manager_pin"):
            return refuse(400, "Confirme e digite o PIN do gerente.")
        if not station.linked:
            return refuse(409, "Este caixa não está ligado a um caixa principal.")
        if not station.can_promote():
            if station.status == "up":
                return refuse(409, "O caixa principal voltou a responder. Não é preciso assumir.", "server_alive")
            return refuse(409, "Aguarde: só é possível assumir depois de um minuto com o caixa principal "
                               "fora do ar.", "too_early")
        if station.server_alive():
            station.wake_monitor()
            return refuse(409, "O caixa principal voltou a responder. Não é preciso assumir.", "server_alive")
        if not station.start_promotion(str(data["manager_pin"])):
            return refuse(409, "A troca já está em andamento.", "running")
        return jsonify({"state": "running"}), 202

    # -- everything else belongs to the server
    def failure(exc, wants_html):
        if wants_html:
            return redirect("/station/", 302)
        if exc.code == "timeout" or exc.sent:
            return refuse(504, TIMEOUT_TEXT, "server_timeout")
        return refuse(503, UNREACHABLE_TEXT, "server_unreachable")

    @app.route("/", defaults={"path": ""}, methods=ALL_METHODS)
    @app.route("/<path:path>", methods=ALL_METHODS)
    def forward(path):
        if path == "station" or path.startswith("station/"):
            if path == "station":
                return redirect("/station/", 302)
            return refuse(404, "Rota não encontrada")  # reserved prefix: never forwarded
        wants_html = request.method == "GET" and (
            request.path == "/" or "text/html" in request.headers.get("Accept", ""))
        if not station.linked:
            return redirect("/station/", 302)
        with station.lock:
            revoked = station.reason if station.status == "revoked" else None
        if revoked:
            return redirect("/station/", 302) if wants_html else refuse(403, REFUSAL_TEXTS[revoked], revoked)
        if (request.content_length or 0) > MAX_BODY:
            return refuse(413, "Conteúdo grande demais.", "too_large")
        content = request.get_data(cache=False)
        named = {token.strip().lower() for token in request.headers.get("Connection", "").split(",") if token.strip()}
        headers = []
        for name, value in request.headers.items():
            lower = name.lower()
            if (lower in HOP_BY_HOP or lower in named or lower in ("content-length", "forwarded")
                    or lower.startswith(("x-pdv-station", "x-forwarded", "x-pdv-lan"))):
                continue
            headers.append((name, value))  # Host, Origin, Cookie, X-CSRF-Token, Content-Type: untouched
        headers += station.identity_headers()
        target = quote(request.environ.get("PATH_INFO", "/").encode("latin-1"), safe="/-._~!$&'()*+,;=:@")
        if request.query_string:
            target += "?" + request.query_string.decode("latin-1")
        slow = request.method == "POST" and request.path in SLOW_ROUTES
        has_body = bool(content) or request.method in ("POST", "PUT", "PATCH")
        try:
            # GET repeats once inside the client on a dropped connection; POST never repeats.
            upstream = station.client().request(request.method, target, body=content if has_body else None,
                                                headers=headers, timeout=SLOW_READ_SECONDS if slow else READ_SECONDS)
        except LanError as exc:
            station.mark_down(exc)
            station.wake_monitor()
            return failure(exc, wants_html)
        code = station.note_response(upstream)
        if code in ("station_revoked", "station_unauthorized"):
            upstream.close()
            return redirect("/station/", 302) if wants_html else refuse(403, REFUSAL_TEXTS[code], code)
        if code in ("station_outdated", "server_outdated") and wants_html:
            upstream.close()
            return redirect("/station/", 302)
        if request.method == "GET" and request.path == "/api/print/discover" and upstream.status == 200:
            # The server checked the login; the printers are the ones of THIS computer.
            upstream.close()
            return jsonify({"driver": "cups", "queues": printers.cups_queues()})
        if request.method == "POST" and 200 <= upstream.status < 300 and (
                request.path == "/api/sales" or request.path.startswith(("/api/sales/", "/api/print/"))
                or request.path == "/api/customers/receive"):
            station.wake_print()

        def stream():
            try:
                while True:
                    chunk = upstream.read(BLOCK)
                    if not chunk:
                        break
                    yield chunk
            except LanError as exc:
                station.mark_down(exc)
                station.wake_monitor()
            finally:
                upstream.close()

        response = Response(stream(), status=upstream.status, direct_passthrough=True)
        out = Headers()
        for name, value in upstream.header_list:
            lower = name.lower()
            if lower in HOP_BY_HOP or lower.startswith("x-pdv-lan") or lower in ("server", "date"):
                continue
            out.add(name, value)  # Set-Cookie stays literal, one line each
        response.headers = out
        return response

    if config.get("START_BACKGROUND"):
        start_background(app)
    return app


def start_background(app):
    app.extensions["station"].start()


def stop_background(app, timeout=5.0):
    app.extensions["station"].stop(timeout=timeout)
