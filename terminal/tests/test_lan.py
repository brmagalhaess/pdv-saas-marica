"""Shop server on the local network: certificate, authentication, forbidden routes and the bridge.

Uses a fake store with the methods of the contract (hook 1) and a minimal WSGI app:
nothing here depends on the core of the terminal. Ports are always ephemeral.
"""

import contextlib
import hashlib
import json
import logging
import os
import secrets
import socket
import ssl
import stat
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path
import unittest.mock
from unittest.mock import MagicMock

from terminal import lan, lanclient
from terminal.lanclient import LanClient, LanError

HOST = "127.0.0.1"
# What an agent of a station sends. The native header never comes from a browser: the bridge
# of the station drops every X-PDV-Station* header (see NativeOnlyTests).
BROWSER = {"Host": "127.0.0.1:8010", "Origin": "http://127.0.0.1:8010", "X-PDV-Station-Native": "1"}


def wait_for(condition, timeout=10, message="condition"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(0.02)
    raise AssertionError("timeout waiting for " + message)


class FakeError(Exception):
    """Same shape as terminal.store.TerminalError with the `code` of the contract."""

    def __init__(self, message, status=400, code=None):
        super().__init__(message)
        self.status = status
        self.code = code


class FakeStore:
    """The store methods the LAN layer may call (contract, hook 1)."""

    def __init__(self, limit=2, shop_id=None, terminal_id=None):
        self.limit = limit
        self.shop_id = shop_id or str(uuid.uuid4())
        self.terminal_id = terminal_id or str(uuid.uuid4())
        self.rows = {}      # station_id -> {"name", "token", "revoked", "created"}
        self.code = None
        self.enrolls = []
        self.touched = []
        self.jobs = {}      # station_id -> list of job dicts
        self.sending = {}   # job id -> station id
        self.finished = []  # (station_id, job_id, error)
        self.recovered = []
        self.auth_tokens = []
        self.fail_finish = False
        self._local = threading.local()
        self._lock = threading.Lock()

    # -- identity
    def identity(self):
        return {"tenant_id": "tenant-1", "shop_id": self.shop_id, "terminal_id": self.terminal_id,
                "device_id": "device-1"}

    def meta(self):
        return {"server_generation": "1", "terminal_id": self.terminal_id}

    def connection_info(self):
        return {"paired": True, "tenant_id": "tenant-1", "tenant_name": "Empresa X", "shop_id": self.shop_id,
                "shop_name": "Mercado X", "terminal_id": self.terminal_id, "terminal_name": "Caixa 1"}

    # -- stations
    def station_limit(self):
        used = sum(1 for row in self.rows.values() if not row["revoked"])
        return {"limit": self.limit, "used": used, "available": max(self.limit - used, 0)}

    def stations(self, include_revoked=True):
        rows = [{"id": "server", "name": "Caixa principal", "is_server": True}]
        rows += [{"id": key, "name": row["name"], "is_server": False, "revoked_at": 1 if row["revoked"] else None}
                 for key, row in self.rows.items() if include_revoked or not row["revoked"]]
        return rows

    def add_station(self, name="Caixa 2", station_id=None):
        station_id = station_id or uuid.uuid4().hex
        token = secrets.token_urlsafe(32)
        self.rows[station_id] = {"name": name, "token": token, "revoked": False, "created": len(self.rows)}
        return station_id, token

    def authenticate_station(self, token):
        self.auth_tokens.append(token)
        if not isinstance(token, str) or not 20 <= len(token) <= 128:
            raise FakeError("Este caixa não está vinculado ao caixa principal.", 401, "station_unauthorized")
        for station_id, row in self.rows.items():
            if secrets.compare_digest(row["token"], token):
                if row["revoked"]:
                    raise FakeError("Este caixa foi removido do caixa principal.", 401, "station_revoked")
                active = sorted((item["created"], key) for key, item in self.rows.items() if not item["revoked"])
                if active.index((row["created"], station_id)) >= self.limit:
                    raise FakeError("O plano da loja não inclui mais este caixa adicional. Fale com o responsável "
                                    "pela loja.", 403, "station_limit")
                return {"id": station_id, "name": row["name"]}
        raise FakeError("Este caixa não está vinculado ao caixa principal.", 401, "station_unauthorized")

    def touch_station(self, station_id, *, address="", app_version="", hostname=""):
        self.touched.append((station_id, address, app_version, hostname))

    def enroll_station(self, code, *, name="", hostname="", app_version="", station_id=None, address=""):
        self.enrolls.append({"code": code, "name": name, "hostname": hostname, "app_version": app_version,
                             "station_id": station_id, "address": address})
        digits = "".join(ch for ch in str(code) if ch.isdigit())
        if len(digits) != 6:
            raise FakeError("Digite os 6 números do código.", 400)
        if self.code is None or digits != self.code:
            raise FakeError("Código inválido ou vencido. Peça um novo código no caixa principal.", 401,
                            "invalid_code")
        if station_id in self.rows:
            self.rows[station_id]["revoked"] = False
            self.rows[station_id]["token"] = secrets.token_urlsafe(32)
        else:
            if self.station_limit()["available"] <= 0:
                raise FakeError("O plano desta loja inclui 2 caixa(s) adicional(is) e todos já estão em uso.",
                                409, "station_limit")
            station_id, _token = self.add_station(name or f"Caixa {len(self.rows) + 2}")
        self.code = None
        row = self.rows[station_id]
        return {"station_id": station_id, "station_name": row["name"], "token": row["token"]}

    @contextlib.contextmanager
    def station(self, station_id):
        previous = getattr(self._local, "station", "server")
        self._local.station = station_id
        try:
            yield
        finally:
            self._local.station = previous

    def current_station(self):
        return getattr(self._local, "station", "server")

    # -- printing
    def queue(self, station_id, payload=b"recibo", kind="receipt", driver="tcp", target="127.0.0.1:9100"):
        job = {"id": uuid.uuid4().hex, "kind": kind, "driver": driver, "target": target, "payload": payload,
               "attempts": 0, "status": "queued"}
        self.jobs.setdefault(station_id, []).append(job)
        return job["id"]

    def begin_print(self):
        with self._lock:
            queue = self.jobs.get(self.current_station(), [])
            if not queue:
                return None
            job = queue.pop(0)
            job["attempts"] += 1
            self.sending[job["id"]] = self.current_station()
            return dict(job)

    def finish_print(self, job_id, error=None):
        if self.fail_finish:
            raise RuntimeError("banco ocupado")
        self.sending.pop(job_id, None)
        self.finished.append((self.current_station(), job_id, error))

    def recover_print(self):
        self.recovered.append(self.current_station())
        return 0


class FakeApp:
    """Minimal WSGI app standing for terminal.app: records what reaches it."""

    def __init__(self, store, data_dir):
        self.extensions = {"terminal_store": store, "terminal_data_dir": Path(data_dir)}
        self.config = {}
        self.calls = []
        self.sales = {}
        self.sale_runs = 0
        self._lock = threading.Lock()

    def __call__(self, environ, start_response):
        length = int(environ.get("CONTENT_LENGTH") or 0)
        body = environ["wsgi.input"].read(length) if length else b""
        path, method = environ["PATH_INFO"], environ["REQUEST_METHOD"]
        station = environ.get("pdv2.station")
        with self._lock:
            self.calls.append({"method": method, "path": path, "station": station, "body": body,
                               "environ": {key: value for key, value in environ.items() if isinstance(value, str)}})
        headers = [("Content-Type", "application/json")]
        status = "200 OK"
        if path == "/":
            start_response(status, [("Content-Type", "text/html; charset=utf-8")])
            return [b"<!doctype html><title>Caixa</title>"]
        if path == "/api/login":
            session = (station or {}).get("id", "server") + "." + secrets.token_hex(4)
            headers += [("Set-Cookie", f"pdv2_session={session}; HttpOnly; Path=/; SameSite=Strict"),
                        ("Set-Cookie", "pdv2_hint=1; Path=/"), ("X-PDV-Lan-Error", "station_revoked")]
            payload = {"ok": True, "session": session}
        elif path == "/api/sales" and method == "POST":
            data = json.loads(body or b"{}")
            with self._lock:
                if data.get("request_id") not in self.sales:
                    self.sale_runs += 1
                    self.sales[data.get("request_id")] = {"sale_id": uuid.uuid4().hex, "station": station}
                payload = self.sales[data.get("request_id")]
            status = "201 Created"
        elif path == "/api/print/discover":
            payload = {"driver": "cups", "queues": ["fila-do-principal"]}
        elif path == "/api/denied":
            status, payload = "401 UNAUTHORIZED", {"error": "Faça login com PIN"}
        else:
            payload = {"station": station, "method": method, "path": path, "query": environ.get("QUERY_STRING"),
                       "cookie": environ.get("HTTP_COOKIE"), "host": environ.get("HTTP_HOST"),
                       "origin": environ.get("HTTP_ORIGIN"), "csrf": environ.get("HTTP_X_CSRF_TOKEN"),
                       "remote": environ.get("REMOTE_ADDR"), "scheme": environ.get("wsgi.url_scheme"),
                       "length": len(body), "sha256": hashlib.sha256(body).hexdigest(),
                       "headers": sorted(key for key in environ if key.startswith("HTTP_"))}
        start_response(status, headers)
        return [json.dumps(payload).encode("utf-8")]

    def paths(self):
        with self._lock:
            return [call["path"] for call in self.calls]


class LanCase(unittest.TestCase):
    """A fake store, a fake app and a real TLS listener on an ephemeral port."""

    limit = 2

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self._env = {key: os.environ.get(key) for key in ("PDV_LAN_PORT", "PDV_DISCOVERY_PORT", "PDV_LAN_HOST")}
        os.environ.update(PDV_LAN_PORT="0", PDV_DISCOVERY_PORT="0", PDV_LAN_HOST=HOST)
        self.addCleanup(self._restore_env)
        self.store = FakeStore(limit=self.limit)
        self.app = FakeApp(self.store, self.temp.name)
        self.server = lan.start(self.app)
        self.addCleanup(lambda: self.app.extensions["terminal_lan"].stop())
        self.assertTrue(self.server.running, self.server.error)
        self.port = self.server.port
        self.fingerprint = self.server.fingerprint
        self._clients = []

    def _restore_env(self):
        for key, value in self._env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def client(self, token=None, **options):
        client = LanClient(HOST, self.port, self.fingerprint, token, **options)
        self.addCleanup(client.close)
        return client

    def call(self, method, path, token=None, body=None, headers=None, client=None):
        response = (client or self.client(token)).request(method, path, body=body, headers=headers or dict(BROWSER))
        try:
            raw = response.read()
        finally:
            response.close()
        try:
            data = json.loads(raw) if raw else None
        except ValueError:
            data = raw
        return response, data


class CertificateTests(unittest.TestCase):
    def test_certificate_is_created_once_and_reused(self):
        with tempfile.TemporaryDirectory() as temp:
            with self.assertLogs("pdv2.lan", level="DEBUG") as logs:
                logging.getLogger("pdv2.lan").debug("início")
                first = lan.ensure_certificate(temp, "loja-1")
                key_text = Path(first["key_path"]).read_text()
                written = (Path(first["cert_path"]).stat().st_mtime_ns, Path(first["key_path"]).stat().st_mtime_ns)
                second = lan.ensure_certificate(temp, "loja-1")
            self.assertEqual(first["fingerprint"], second["fingerprint"])
            self.assertRegex(first["fingerprint"], r"^[0-9a-f]{64}$")
            self.assertEqual(written, (Path(second["cert_path"]).stat().st_mtime_ns,
                                       Path(second["key_path"]).stat().st_mtime_ns))
            self.assertEqual(lan.fingerprint(Path(first["cert_path"]).read_bytes()), first["fingerprint"])
            if os.name != "nt":
                self.assertEqual(stat.S_IMODE(Path(first["key_path"]).stat().st_mode), 0o600)
                self.assertEqual(stat.S_IMODE(Path(first["cert_path"]).stat().st_mode), 0o600)
                self.assertEqual(stat.S_IMODE((Path(temp) / "lan").stat().st_mode), 0o700)
            self.assertIn("PRIVATE KEY", key_text)
            body = key_text.split("-----")[2].strip().splitlines()[0]
            self.assertFalse(any(body in line for line in logs.output), "private key must never be logged")

    def test_certificate_content(self):
        from cryptography import x509
        from cryptography.hazmat.primitives.asymmetric import ec

        with tempfile.TemporaryDirectory() as temp:
            made = lan.ensure_certificate(temp, "loja-1")
            certificate = x509.load_pem_x509_certificate(Path(made["cert_path"]).read_bytes())
            self.assertIsInstance(certificate.public_key().curve, ec.SECP256R1)
            self.assertEqual(certificate.subject.rfc4514_string(), "CN=PDV2 loja-1")
            self.assertEqual(certificate.subject, certificate.issuer)
            names = certificate.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
            self.assertEqual(names.get_values_for_type(x509.DNSName), ["pdv2.local"])
            years = (certificate.not_valid_after_utc - certificate.not_valid_before_utc).days / 365
            self.assertGreater(years, 19.9)

    def test_removed_folder_makes_a_new_identity(self):
        with tempfile.TemporaryDirectory() as temp:
            first = lan.ensure_certificate(temp, "loja-1")["fingerprint"]
            for item in (Path(temp) / "lan").iterdir():
                item.unlink()
            self.assertNotEqual(lan.ensure_certificate(temp, "loja-1")["fingerprint"], first)

    def test_restart_keeps_the_fingerprint(self):
        case = LanCase("setUp")
        case.setUp()
        try:
            first, port = case.fingerprint, case.port
            case.server.stop()
            again = lan.start(case.app, port=port)
            self.assertTrue(again.running, again.error)
            self.assertEqual((again.fingerprint, again.port), (first, port))
            self.assertEqual(lanclient.peer_fingerprint(HOST, port), first)
        finally:
            case.doCleanups()


class ListenTests(unittest.TestCase):
    def start(self, store):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        app = FakeApp(store, temp.name)
        server = lan.start(app, host=HOST, port=0)
        self.addCleanup(server.stop)
        return app, server

    def test_listens_only_when_the_shop_may_have_stations(self):
        store = FakeStore(limit=0)
        app, server = self.start(store)
        self.assertFalse(server.running)
        self.assertFalse(lan.status(app)["running"])
        store.limit = 1
        lan.poke(app)
        self.assertTrue(server.running)
        status = lan.status(app)
        self.assertEqual((status["supported"], status["running"], status["port"]), (True, True, server.port))
        self.assertEqual(status["fingerprint"], server.fingerprint)
        self.assertIn(status["network_profile"], ("private", "public", "domain", "unknown"))
        self.assertIsInstance(status["addresses"], list)
        store.limit = 0
        lan.poke(app)
        self.assertFalse(server.running)
        # An existing station keeps the port open even when the plan shrank to zero.
        store.add_station()
        lan.poke(app)
        self.assertTrue(server.running)

    def test_unpaired_or_damaged_never_listens(self):
        store = FakeStore(limit=2)
        store.terminal_id = None
        store.identity = lambda: {"terminal_id": None, "shop_id": None}
        _app, server = self.start(store)
        self.assertFalse(server.running)
        app = FakeApp(None, tempfile.mkdtemp())
        server = lan.start(app, host=HOST, port=0)
        self.addCleanup(server.stop)
        self.assertFalse(server.running)

    def test_store_of_an_older_core_never_listens(self):
        class Old:
            def identity(self):
                return {"terminal_id": "t", "shop_id": "s"}

        _app, server = self.start(Old())
        self.assertFalse(server.running)

    def test_status_without_a_listener(self):
        app = FakeApp(FakeStore(), tempfile.mkdtemp())
        status = lan.status(app)
        self.assertEqual((status["supported"], status["running"], status["fingerprint"]), (True, False, None))
        lan.poke(app)  # nothing to poke: must not raise

    def test_firewall_rule_is_created_once(self):
        calls = []

        def run(argv, **_options):
            calls.append(argv)
            return MagicMock(returncode=1 if "show" in argv else 0)

        self.assertEqual(lan.ensure_firewall(8011, 8012, run=run, platform="win32"), "ok")
        self.assertEqual([call[3] for call in calls], ["show", "add", "add"])
        self.assertIn("name=PDV2 - Servidor da loja", calls[1])
        self.assertIn("localport=8011", calls[1])
        self.assertIn("protocol=UDP", calls[2])
        self.assertIn("remoteip=localsubnet", calls[2])
        calls.clear()
        self.assertEqual(lan.ensure_firewall(run=lambda argv, **_o: calls.append(argv) or MagicMock(returncode=0),
                                             platform="win32"), "ok")
        self.assertEqual(len(calls), 1, "an existing rule is left alone")
        self.assertEqual(lan.ensure_firewall(run=MagicMock(side_effect=OSError("sem netsh")), platform="win32"),
                         "unknown")
        self.assertEqual(lan.ensure_firewall(platform="linux"), "unknown")


class AuthenticationTests(LanCase):
    def test_hello_needs_no_token(self):
        response, data = self.call("GET", "/lan/v1/hello")
        self.assertEqual(response.status, 200)
        self.assertEqual((data["service"], data["api"], data["port"]), ("pdv2-lan", 1, self.port))
        self.assertEqual((data["shop_id"], data["shop_name"], data["terminal_name"]),
                         (self.store.shop_id, "Mercado X", "Caixa 1"))
        self.assertEqual((data["fingerprint"], data["generation"], data["enroll_open"]), (self.fingerprint, 1, True))
        self.assertEqual(self.store.auth_tokens, [])

    def test_refusals_have_distinct_codes_and_the_lan_header(self):
        station_id, token = self.store.add_station()
        cases = [(None, 401, "station_unauthorized"), ("x" * 43, 401, "station_unauthorized")]
        for sent, status, code in cases:
            response, data = self.call("GET", "/api/echo", token=sent)
            self.assertEqual((response.status, data["code"]), (status, code))
            self.assertEqual(response.headers["x-pdv-lan-error"], code)
            self.assertEqual(data["error"], "Este caixa não está vinculado ao caixa principal.")
        self.store.rows[station_id]["revoked"] = True
        response, data = self.call("GET", "/api/echo", token=token)
        self.assertEqual((response.status, data["code"]), (401, "station_revoked"))
        self.assertEqual(response.headers["x-pdv-lan-error"], "station_revoked")
        self.assertEqual(data["error"], "Este caixa foi removido do caixa principal.")
        self.assertEqual(self.app.calls, [], "a refused request never reaches the app")

    def test_station_outside_the_plan_is_403(self):
        _first, token_one = self.store.add_station("Caixa 2")
        _second, token_two = self.store.add_station("Caixa 3")
        self.store.limit = 1
        self.assertEqual(self.call("GET", "/lan/v1/ping", token=token_one)[0].status, 200)
        response, data = self.call("GET", "/lan/v1/ping", token=token_two)
        self.assertEqual((response.status, data["code"]), (403, "station_limit"))
        self.assertEqual(response.headers["x-pdv-lan-error"], "station_limit")

    def test_protocol_version_is_426(self):
        _station, token = self.store.add_station()
        for declared, code, text in (("2", "server_outdated", "Atualize o caixa principal"),
                                     ("0", "station_outdated", "Atualize este caixa")):
            response, data = self.call("GET", "/lan/v1/ping", token=token,
                                       headers={**BROWSER, "X-PDV-Station-Api": declared})
            self.assertEqual((response.status, data["code"]), (426, code))
            self.assertEqual(response.headers["x-pdv-lan-error"], code)
            self.assertIn(text, data["error"])
        response, _data = self.call("GET", "/lan/v1/ping", token=token, headers={**BROWSER, "X-PDV-Station-Api": "1"})
        self.assertEqual(response.status, 200)

    def test_twenty_invalid_tokens_block_the_address(self):
        _station, token = self.store.add_station()
        client = self.client()
        for attempt in range(20):
            response, data = self.call("GET", "/lan/v1/ping", client=client,
                                       headers={**BROWSER, "X-PDV-Station-Token": f"{attempt:02d}" + "z" * 41})
            self.assertEqual((response.status, data["code"]), (401, "station_unauthorized"), attempt)
        response, data = self.call("GET", "/lan/v1/ping", client=client,
                                   headers={**BROWSER, "X-PDV-Station-Token": "y" * 43})
        self.assertEqual((response.status, data["code"]), (429, "too_many_attempts"))
        self.assertEqual(response.headers["x-pdv-lan-error"], "too_many_attempts")
        # Even the right token waits while the address is blocked.
        self.assertEqual(self.call("GET", "/lan/v1/ping", token=token)[0].status, 429)
        self.server._blocked.clear()
        self.assertEqual(self.call("GET", "/lan/v1/ping", token=token)[0].status, 200)

    def test_missing_and_revoked_tokens_do_not_count_as_guesses(self):
        station_id, token = self.store.add_station()
        self.store.rows[station_id]["revoked"] = True
        client = self.client()
        for _attempt in range(25):
            self.assertEqual(self.call("GET", "/lan/v1/ping", client=client)[0].status, 401)
            self.assertEqual(self.call("GET", "/lan/v1/ping", token=token)[1]["code"], "station_revoked")

    def test_ping_and_touch(self):
        station_id, token = self.store.add_station("Caixa 2")
        client = self.client(token)
        headers = {**BROWSER, "X-PDV-Station-Version": "2.1.0", "X-PDV-Station-Host": "PC-CAIXA2"}
        response, data = self.call("GET", "/lan/v1/ping", client=client, headers=headers)
        self.assertEqual(response.status, 200)
        self.assertEqual((data["ok"], data["api"], data["generation"]), (True, 1, 1))
        self.assertEqual(data["station"], {"id": station_id, "name": "Caixa 2"})
        self.assertLess(abs(data["server_time"] - time.time()), 5)
        self.call("GET", "/lan/v1/ping", client=client, headers=headers)
        self.assertEqual(self.store.touched, [(station_id, HOST, "2.1.0", "PC-CAIXA2")],
                         "presence is written at most once every 30 s")

    def test_damaged_database_answers_503(self):
        _station, token = self.store.add_station()
        self.app.extensions["terminal_store"] = None
        response, data = self.call("GET", "/api/session", token=token)
        self.assertEqual((response.status, data["damaged"]), (503, True))
        self.assertIn("banco de dados danificado", data["error"])
        self.assertEqual(self.app.calls, [])

    def test_unknown_lan_route_and_wrong_method(self):
        _station, token = self.store.add_station()
        self.assertEqual(self.call("GET", "/lan/v1/nada", token=token)[0].status, 404)
        self.assertEqual(self.call("POST", "/lan/v1/ping", token=token, body={})[0].status, 405)
        self.assertEqual(self.call("GET", "/lan/v1/nada")[0].status, 401, "native routes need the token too")
        self.assertEqual(self.app.calls, [])

    def test_replica_routes_without_the_module(self):
        _station, token = self.store.add_station()
        if lan.replica_net is not None:
            self.skipTest("replica_net está instalado")
        response, data = self.call("GET", "/lan/v1/replica/status", token=token)
        self.assertEqual((response.status, data["code"]), (404, "replica_unavailable"))

    def test_replica_routes_are_delegated(self):
        station_id, token = self.store.add_station()
        seen = []

        class Replica:
            @staticmethod
            def handle_request(store, station, method, path, query, headers):
                seen.append((store, station, method, path, dict(query), headers.get("Range"), headers.get("range"),
                             "x-pdv-station-token" in headers))
                if path.endswith("/snapshot/a.pdv2snap"):
                    return 206, {"Content-Type": "application/octet-stream", "Content-Length": "6",
                                 "Accept-Ranges": "bytes"}, iter([b"abc", b"def"])
                return 200, {"Content-Type": "application/json", "Cache-Control": "no-store"}, b'{"ok":true}'

        previous, lan.replica_net = lan.replica_net, Replica
        self.addCleanup(setattr, lan, "replica_net", previous)
        client = self.client(token)
        response, data = self.call("GET", "/lan/v1/replica/journal?after=7&wait=0", client=client)
        self.assertEqual((response.status, data), (200, {"ok": True}))
        response, data = self.call("GET", "/lan/v1/replica/snapshot/a.pdv2snap", client=client,
                                   headers={**BROWSER, "Range": "bytes=3-"})
        self.assertEqual((response.status, data, response.headers["accept-ranges"]), (206, b"abcdef", "bytes"))
        response, _data = self.call("POST", "/lan/v1/replica/ack", client=client,
                                    body={"journal_seq": 9, "snapshot_outbox_seq": None})
        self.assertEqual(response.status, 200)
        self.assertEqual(seen[0][:5], (self.store, {"id": station_id, "name": "Caixa 2"}, "GET",
                                       "/lan/v1/replica/journal", {"after": "7", "wait": "0"}))
        self.assertEqual(seen[1][5:], ("bytes=3-", "bytes=3-", False), "headers ignore case; the token stays out")
        self.assertEqual(seen[2][4], {"journal_seq": 9, "snapshot_outbox_seq": None})
        self.assertEqual(self.app.calls, [])


FORBIDDEN = [
    ("POST", "/api/pair"), ("POST", "/api/unpair"), ("POST", "/api/new-profile"), ("POST", "/api/bootstrap"),
    ("POST", "/api/restore/start"), ("GET", "/api/restore/status"), ("POST", "/api/restore/step"),
    ("POST", "/api/restore/retry"), ("POST", "/api/restore/abandon"), ("GET", "/api/restore/backups"),
    ("POST", "/api/restore/import-backup"), ("POST", "/api/restore/skip-backup"),
    ("GET", "/api/export"), ("GET", "/api/backups"), ("POST", "/api/backups"),
    ("GET", "/api/mirror/drives"), ("POST", "/api/mirror/config"), ("POST", "/api/mirror/snapshot"),
    ("POST", "/api/update/apply"),
    ("GET", "/api/stations"), ("POST", "/api/stations/code"), ("POST", "/api/stations/abc/rename"),
    ("POST", "/api/stations/abc/revoke"), ("POST", "/api/stations/abc/cash/close"),
    ("POST", "/api/promotion/regularize"), ("POST", "/api/mode/station"),
]


class ServerOnlyTests(LanCase):
    def test_every_forbidden_route_stops_before_the_app(self):
        _station, token = self.store.add_station()
        client = self.client(token)
        for method, path in FORBIDDEN:
            response, data = self.call(method, path, client=client, body={} if method == "POST" else None)
            self.assertEqual((response.status, data), (403, {
                "error": "Esta operação só pode ser feita no caixa principal da loja.", "code": "server_only"}), path)
            self.assertEqual(response.headers["x-pdv-lan-error"], "server_only")
        self.assertEqual(self.app.calls, [])

    def test_disguised_paths_are_forbidden_too(self):
        _station, token = self.store.add_station()
        client = self.client(token)
        for method, path in (("POST", "/api/%70air"), ("POST", "/api//pair"), ("POST", "/api/pair/"),
                             ("HEAD", "/api/export"), ("GET", "/api/export?x=1"), ("POST", "/API/Pair"),
                             ("DELETE", "/api/stations/abc"), ("GET", "/api/backups/terminal.sqlite3")):
            response, _data = self.call(method, path, client=client, body={} if method == "POST" else None)
            self.assertEqual(response.status, 403, path)
        response, _data = self.call("GET", "/api/x/../pair", client=client)
        self.assertEqual(response.status, 400)
        self.assertEqual(self.app.calls, [])

    def test_allowed_routes_reach_the_app(self):
        _station, token = self.store.add_station()
        client = self.client(token)
        for method, path in (("GET", "/api/mirror/status"), ("GET", "/api/update/status"), ("POST", "/api/sync/now"),
                             ("POST", "/api/license/renew"), ("POST", "/api/print/flush"), ("GET", "/api/session"),
                             ("GET", "/api/restores"), ("GET", "/api/pairs")):
            response, _data = self.call(method, path, client=client, body={} if method == "POST" else None)
            self.assertEqual(response.status, 200, path)
        self.assertEqual(len(self.app.calls), 8)

    def test_canonical_list_matches_the_contract(self):
        self.assertEqual(set(lan.SERVER_ONLY_PATHS), {
            "/api/pair", "/api/unpair", "/api/new-profile", "/api/bootstrap", "/api/restore", "/api/export",
            "/api/backups", "/api/mirror/drives", "/api/mirror/config", "/api/mirror/snapshot",
            "/api/update/apply", "/api/stations", "/api/promotion", "/api/mode/station"})
        self.assertFalse(lan.server_only("/api/mirror/status"))
        self.assertFalse(lan.server_only("/api/sales"))
        self.assertTrue(lan.server_only("/api/restore"))


class BridgeTests(LanCase):
    def test_environ_carries_the_station_and_drops_forged_headers(self):
        station_id, token = self.store.add_station("Caixa 2")
        headers = {**BROWSER, "Cookie": "pdv2_session=abc", "X-CSRF-Token": "csrf-1",
                   "X-PDV-Station-Id": "server", "X-PDV-Station": "server", "X-PDV-Station-Name": "Caixa principal",
                   "X-Forwarded-For": "127.0.0.1", "X-Forwarded-Host": "evil", "Forwarded": "for=127.0.0.1",
                   "X-PDV-Lan-Error": "x", "X_PDV_Station_Id": "server", "X-Outro": "fica"}
        response, data = self.call("POST", "/api/echo?a=1&b=%C3%A7", token=token, body=b'{"x":1}',
                                   headers={**headers, "Content-Type": "application/json"})
        self.assertEqual(response.status, 200)
        self.assertEqual(data["station"], {"id": station_id, "name": "Caixa 2", "version": ""})
        self.assertEqual((data["host"], data["origin"]), ("127.0.0.1:8010", "http://127.0.0.1:8010"))
        self.assertEqual((data["cookie"], data["csrf"]), ("pdv2_session=abc", "csrf-1"))
        self.assertEqual((data["method"], data["path"], data["query"]), ("POST", "/api/echo", "a=1&b=%C3%A7"))
        self.assertEqual((data["remote"], data["scheme"], data["length"]), (HOST, "http", 7))
        self.assertEqual(data["headers"], ["HTTP_ACCEPT_ENCODING", "HTTP_COOKIE", "HTTP_HOST", "HTTP_ORIGIN",
                                           "HTTP_X_CSRF_TOKEN", "HTTP_X_OUTRO"])
        environ = self.app.calls[0]["environ"]
        self.assertEqual(environ["CONTENT_TYPE"], "application/json")
        self.assertFalse(any("STATION" in key or "FORWARDED" in key or "LAN" in key for key in environ
                             if key.startswith("HTTP_")))
        self.assertNotIn(token, json.dumps(environ), "the station token never enters the app")

    def test_a_client_cannot_name_its_own_station(self):
        _first, token = self.store.add_station("Caixa 2")
        other, _other_token = self.store.add_station("Caixa 3")
        _response, data = self.call("GET", "/api/echo", token=token, headers={**BROWSER, "X-PDV-Station-Id": other})
        self.assertNotEqual(data["station"]["id"], other)

    def test_two_set_cookie_lines_and_no_lan_header_from_the_app(self):
        _station, token = self.store.add_station()
        response, _data = self.call("POST", "/api/login", token=token, body={})
        cookies = [value for name, value in response.header_list if name.lower() == "set-cookie"]
        self.assertEqual(len(cookies), 2)
        self.assertTrue(cookies[0].startswith("pdv2_session=") and cookies[0].endswith("SameSite=Strict"))
        self.assertEqual(cookies[1], "pdv2_hint=1; Path=/")
        self.assertNotIn("x-pdv-lan-error", response.headers, "the app cannot fake a refusal of the LAN layer")
        self.assertEqual(int(response.headers["content-length"]) > 0, True)

    def test_app_status_and_body_come_back_untouched(self):
        _station, token = self.store.add_station()
        response, data = self.call("GET", "/api/denied", token=token)
        self.assertEqual((response.status, data), (401, {"error": "Faça login com PIN"}))
        self.assertNotIn("x-pdv-lan-error", response.headers)
        response, data = self.call("GET", "/", token=token)
        self.assertEqual((response.status, response.headers["content-type"]), (200, "text/html; charset=utf-8"))
        self.assertIn(b"<title>Caixa</title>", data)

    def test_body_of_300_kib_is_413(self):
        _station, token = self.store.add_station()
        response, data = self.call("POST", "/api/echo", token=token, body=b"x" * (300 * 1024))
        self.assertEqual((response.status, data["code"]), (413, "too_large"))
        self.assertEqual(self.app.calls, [])
        response, data = self.call("POST", "/api/echo", token=token, body=b"x" * (256 * 1024))
        self.assertEqual((response.status, data["length"]), (200, 256 * 1024))

    def test_keep_alive_serves_many_requests_on_one_connection(self):
        _station, token = self.store.add_station()
        client = self.client(token)
        for index in range(5):
            response, data = self.call("GET", f"/api/echo?n={index}", client=client)
            self.assertEqual((response.status, data["query"]), (200, f"n={index}"))
        self.assertEqual(self.server._listener._count, 1)

    def test_app_exception_becomes_500_without_details(self):
        _station, token = self.store.add_station()
        self.app.__class__ = type("Broken", (FakeApp,), {"__call__": lambda *_a: (_ for _ in ()).throw(
            RuntimeError("segredo interno"))})
        self.addCleanup(setattr, self.app, "__class__", FakeApp)
        with self.assertLogs("pdv2.lan", level="ERROR") as logs:
            response, data = self.call("GET", "/api/echo", token=token)
        self.assertEqual((response.status, data["code"]), (500, "server_error"))
        self.assertNotIn("segredo interno", json.dumps(data) + "".join(logs.output))

    def test_plain_http_and_garbage_do_not_stop_the_listener(self):
        _station, token = self.store.add_station()
        for payload in (b"GET / HTTP/1.1\r\nHost: x\r\n\r\n", b"\x00" * 64, b""):
            with socket.create_connection((HOST, self.port), timeout=2) as raw:
                raw.sendall(payload)
        self.assertEqual(self.call("GET", "/lan/v1/ping", token=token)[0].status, 200)

    def test_tls_1_2_is_the_minimum(self):
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        with socket.create_connection((HOST, self.port), timeout=2) as raw:
            with context.wrap_socket(raw) as tls:
                self.assertIn(tls.version(), ("TLSv1.2", "TLSv1.3"))
        self.assertEqual(self.server.context.minimum_version, ssl.TLSVersion.TLSv1_2)

    def test_connections_above_the_limit_get_503(self):
        _station, token = self.store.add_station()
        self.addCleanup(setattr, lan, "MAX_CONNECTIONS", lan.MAX_CONNECTIONS)
        self.addCleanup(setattr, lan, "CONNECTIONS_PER_STATION", lan.CONNECTIONS_PER_STATION)
        lan.MAX_CONNECTIONS, lan.CONNECTIONS_PER_STATION = 2, 0
        self.server.evaluate()
        held = [self.client(token) for _ in range(2)]
        for client in held:
            self.assertEqual(self.call("GET", "/lan/v1/ping", client=client)[0].status, 200)
        response, data = self.call("GET", "/lan/v1/ping", token=token)
        self.assertEqual((response.status, data["code"]), (503, "server_busy"))
        # A POST with a body is read before the refusal: the caixa gets "busy", never a reset
        # that would look like "sent and lost".
        for _attempt in range(5):
            response, data = self.call("POST", "/api/sales", token=token, body=b"x" * 200_000)
            self.assertEqual((response.status, data["code"], response.headers["x-pdv-lan-error"]),
                             (503, "server_busy", "server_busy"))
        self.assertEqual(self.app.calls, [])
        held[0].close()
        wait_for(lambda: self.server._listener._count <= 1, message="connection released")
        self.assertEqual(self.call("GET", "/lan/v1/ping", token=token)[0].status, 200)

    def test_limit_has_room_for_every_station_of_the_plan(self):
        # About nine connections per caixa in use: four caixas need 36, more than the old 32.
        self.assertGreaterEqual(self.server.connection_limit, 128)
        self.store.limit = 50
        self.server.evaluate()
        self.assertEqual(self.server.connection_limit, lan.CONNECTIONS_PER_STATION * 51)
        self.store.limit = 5000
        self.server.evaluate()
        self.assertEqual(self.server.connection_limit, lan.MAX_CONNECTIONS_CEILING)
        self.store.limit = 2
        self.server.evaluate()
        _station, token = self.store.add_station()
        held = [self.client(token) for _ in range(40)]
        statuses = [self.call("GET", "/lan/v1/ping", client=client)[0].status for client in held]
        self.assertEqual(statuses, [200] * 40)

    def test_answers_leave_without_nagle_and_in_one_write(self):
        _station, token = self.store.add_station()
        client = self.client(token)
        self.assertEqual(self.call("GET", "/lan/v1/ping", client=client)[0].status, 200)
        sockets = list(self.server._listener._sockets)
        self.assertEqual(len(sockets), 1)
        self.assertTrue(sockets[0].getsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY))
        self.assertGreater(lan._Handler.wbufsize, 0, "headers and body are buffered into one write")
        writes = []
        real = ssl.SSLSocket.send

        def send(sock, data, *args):
            if sock is sockets[0]:
                writes.append(bytes(data))
            return real(sock, data, *args)

        with unittest.mock.patch.object(ssl.SSLSocket, "send", send):
            response, data = self.call("GET", "/api/echo", client=client)
        self.assertEqual((response.status, data["path"]), (200, "/api/echo"))
        self.assertEqual(len(writes), 1, "one TLS record carries headers and body")
        self.assertIn(b"\r\n\r\n{", writes[0])

    def test_stopping_the_listener_drops_open_connections(self):
        _station, token = self.store.add_station()
        client = self.client(token)
        self.assertEqual(self.call("GET", "/lan/v1/ping", client=client)[0].status, 200)
        self.server.stop()
        self.assertFalse(self.server.running)
        with self.assertRaises(LanError):
            self.call("GET", "/lan/v1/ping", client=client)


class EnrollTests(LanCase):
    def enroll(self, code, client=None, **extra):
        body = {"code": code, "name": "Caixa 2", "hostname": "PC-CAIXA2", "app_version": "2.1.0",
                "station_id": None, **extra}
        return self.call("POST", "/lan/v1/enroll", body=body, client=client)

    def test_right_code_returns_token_and_fingerprint(self):
        self.store.code = "483920"
        response, data = self.enroll("483920")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual((data["api"], data["station_name"], data["fingerprint"]), (1, "Caixa 2", self.fingerprint))
        self.assertEqual(data["token"], self.store.rows[data["station_id"]]["token"])
        self.assertEqual(data["server"], {
            "shop_id": self.store.shop_id, "shop_name": "Mercado X", "tenant_name": "Empresa X",
            "terminal_id": self.store.terminal_id, "terminal_name": "Caixa 1", "host": socket.gethostname(),
            "port": self.port, "generation": 1})
        self.assertEqual(self.store.enrolls[0], {"code": "483920", "name": "Caixa 2", "hostname": "PC-CAIXA2",
                                                 "app_version": "2.1.0", "station_id": None, "address": HOST})
        self.assertEqual(self.call("GET", "/lan/v1/ping", token=data["token"])[0].status, 200)

    def test_wrong_code_is_401_and_bad_body_is_400(self):
        self.store.code = "483920"
        response, data = self.enroll("111111")
        self.assertEqual((response.status, data["code"]), (401, "invalid_code"))
        self.assertIn("Código inválido ou vencido", data["error"])
        self.assertEqual(self.enroll("12")[0].status, 400)
        self.assertEqual(self.enroll(["483920"])[0].status, 400)
        self.assertEqual(self.call("POST", "/lan/v1/enroll", body=b"nada")[0].status, 400)
        self.assertEqual(self.call("GET", "/lan/v1/enroll")[0].status, 405)

    def test_eleventh_attempt_of_one_address_is_429(self):
        self.store.code = "483920"
        client = self.client()
        for attempt in range(10):
            self.assertEqual(self.enroll("000000", client=client)[0].status, 401, attempt)
        response, data = self.enroll("483920", client=client)
        self.assertEqual((response.status, data["code"]), (429, "too_many_attempts"))
        self.assertIn("Muitas tentativas", data["error"])
        self.assertEqual(len(self.store.enrolls), 10, "the 11th attempt never reaches the store")

    def test_full_plan_is_409(self):
        self.store.add_station("Caixa 2")
        self.store.add_station("Caixa 3")
        self.store.code = "483920"
        response, data = self.enroll("483920")
        self.assertEqual((response.status, data["code"]), (409, "station_limit"))
        self.assertEqual(self.store.code, "483920", "the code is not consumed")

    def test_previous_station_id_goes_with_a_relink(self):
        station_id, old_token = self.store.add_station("Caixa 2")
        self.store.add_station("Caixa 3")
        self.store.code = "483920"
        response, data = self.enroll("483920", station_id=station_id)
        self.assertEqual((response.status, data["station_id"]), (200, station_id))
        self.assertNotEqual(data["token"], old_token)
        self.assertEqual(self.store.enrolls[0]["station_id"], station_id)
        self.store.code = "483921"
        self.assertEqual(self.enroll("483921", station_id="../../x")[0].status, 409)
        self.assertIsNone(self.store.enrolls[-1]["station_id"], "a malformed id is not passed on")

    def test_reserved_identities_are_never_given_to_another_computer(self):
        # The row "server" always exists in the real database, and "anterior-…" after a promotion.
        for reserved in ("server", "SERVER", "anterior-1a2b3c4d"):
            self.store.rows[reserved] = {"name": "Caixa principal", "token": secrets.token_urlsafe(32),
                                         "revoked": False, "created": -1}
        self.store.limit = 10
        for number, reserved in enumerate(("server", "SERVER", "anterior-1a2b3c4d")):
            self.store.code = f"48392{number}"
            response, data = self.enroll(self.store.code, station_id=reserved)
            self.assertEqual(response.status, 200)
            self.assertIsNone(self.store.enrolls[-1]["station_id"], "a reserved id is not passed on")
            self.assertNotIn(data["station_id"].lower(), ("server", "anterior-1a2b3c4d"))
        # Even a token that the database accepts as "server" is nobody on the network.
        response, data = self.call("GET", "/lan/v1/ping", token=self.store.rows["server"]["token"])
        self.assertEqual((response.status, data["code"]), (401, "station_unauthorized"))
        self.store.queue("server", b"do principal")
        response, data = self.call("POST", "/lan/v1/print/next", token=self.store.rows["server"]["token"], body={})
        self.assertEqual(response.status, 401)
        self.assertEqual(len(self.store.jobs["server"]), 1, "the queue of the caixa principal stays there")
        response, _data = self.call("GET", "/api/echo", token=self.store.rows["anterior-1a2b3c4d"]["token"])
        self.assertEqual(response.status, 401)
        self.assertEqual(self.app.calls, [])

    def test_secrets_are_not_logged(self):
        self.store.code = "483920"
        with self.assertLogs("pdv2.lan", level="DEBUG") as logs:
            logging.getLogger("pdv2.lan").debug("início")
            _response, data = self.enroll("483920")
            self.call("GET", "/lan/v1/ping", token=data["token"])
            self.enroll("111111")
        text = "\n".join(logs.output)
        self.assertNotIn(data["token"], text)
        self.assertNotIn("483920", text)


class PrintRouteTests(LanCase):
    def test_jobs_of_the_station_only(self):
        first, token = self.store.add_station("Caixa 2")
        second, other_token = self.store.add_station("Caixa 3")
        job_id = self.store.queue(first, b"\x1b@recibo")
        self.store.queue("server", b"do principal")
        response, data = self.call("POST", "/lan/v1/print/next", token=other_token, body={})
        self.assertEqual((response.status, data), (200, {"job": None}))
        response, data = self.call("POST", "/lan/v1/print/next", token=token, body={})
        self.assertEqual(data["job"], {"id": job_id, "kind": "receipt", "driver": "tcp", "target": "127.0.0.1:9100",
                                       "attempts": 1, "payload_b64": "G0ByZWNpYm8="})
        response, data = self.call("POST", f"/lan/v1/print/{job_id}/done", token=token, body={"error": None})
        self.assertEqual((response.status, data), (200, {"ok": True}))
        self.call("POST", f"/lan/v1/print/{job_id}/done", token=token, body={"error": "sem papel"})
        self.assertEqual(self.store.finished, [(first, job_id, None), (first, job_id, "sem papel")])
        response, data = self.call("POST", "/lan/v1/print/recover", token=other_token, body={})
        self.assertEqual((response.status, data), (200, {"recovered": 0}))
        self.assertEqual(self.store.recovered, [second])
        self.assertEqual(len(self.store.jobs["server"]), 1, "the queue of the server is never handed out")
        self.assertEqual(self.call("POST", "/lan/v1/print/a%20b/done", token=token, body={})[0].status, 404)
        self.assertEqual(self.app.calls, [])


class PinnedClientTests(unittest.TestCase):
    """The fingerprint is compared before any request byte leaves the station."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        made = lan.ensure_certificate(self.temp.name, "loja")
        self.fingerprint = made["fingerprint"]
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(str(made["cert_path"]), str(made["key_path"]))
        self.listener = socket.create_server((HOST, 0))
        self.listener.settimeout(0.2)
        self.port = self.listener.getsockname()[1]
        self.received = []
        self.handshakes = 0
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._serve, args=(context,), daemon=True)
        self.thread.start()
        self.addCleanup(self._close)

    def _close(self):
        self.stop.set()
        self.thread.join(timeout=3)
        self.listener.close()

    def _serve(self, context):
        while not self.stop.is_set():
            try:
                raw, _address = self.listener.accept()
            except OSError:
                continue
            try:
                raw.settimeout(2)
                with context.wrap_socket(raw, server_side=True) as tls:
                    self.handshakes += 1
                    while True:
                        data = tls.recv(4096)
                        if not data:
                            break
                        self.received.append(data)
            except OSError:
                pass
            finally:
                raw.close()

    def test_wrong_fingerprint_sends_nothing(self):
        token = "segredo-" + "t" * 40
        client = LanClient(HOST, self.port, "0" * 64, token)
        with self.assertRaises(LanError) as raised:
            client.request("POST", "/lan/v1/print/next", body={"x": 1})
        self.assertEqual((raised.exception.code, raised.exception.sent), ("fingerprint_mismatch", False))
        self.assertIsInstance(raised.exception, OSError)
        with self.assertRaises(LanError):
            client.request("GET", "/api/session")
        wait_for(lambda: self.handshakes >= 2, message="handshakes")
        time.sleep(0.2)
        self.assertEqual(self.received, [], "no request byte, and never the token, reaches an impostor")
        client.close()

    def test_right_fingerprint_talks_and_peer_fingerprint_reads_it(self):
        self.assertEqual(lanclient.peer_fingerprint(HOST, self.port), self.fingerprint)
        client = LanClient(HOST, self.port, self.fingerprint, "t" * 43, timeout=0.5)
        with self.assertRaises(LanError) as raised:
            client.request("POST", "/api/sales", body={"request_id": "r1"})
        self.assertEqual((raised.exception.code, raised.exception.sent), ("timeout", True))
        wait_for(lambda: self.received, message="request bytes")
        self.assertIn(b"POST /api/sales HTTP/1.1", b"".join(self.received))
        self.assertEqual(b"".join(self.received).count(b"POST /api/sales"), 1, "a POST is never repeated")
        client.close()

    def test_refused_and_unreachable(self):
        with socket.socket() as probe:
            probe.bind((HOST, 0))
            free = probe.getsockname()[1]
        client = LanClient(HOST, free, self.fingerprint, "t" * 43)
        with self.assertRaises(LanError) as raised:
            client.request("GET", "/lan/v1/ping")
        self.assertEqual((raised.exception.code, raised.exception.sent, raised.exception.kind),
                         ("unreachable", False, "refused"))
        with self.assertRaises(LanError) as raised:
            lanclient.peer_fingerprint(HOST, free, timeout=1)
        self.assertEqual(raised.exception.code, "unreachable")

    def test_plain_server_is_a_tls_error(self):
        plain = socket.create_server((HOST, 0))
        self.addCleanup(plain.close)

        def answer():
            try:
                connection, _address = plain.accept()
                connection.sendall(b"HTTP/1.1 200 OK\r\n\r\n")
                connection.close()
            except OSError:
                pass

        threading.Thread(target=answer, daemon=True).start()
        with self.assertRaises(LanError) as raised:
            LanClient(HOST, plain.getsockname()[1], self.fingerprint).request("GET", "/lan/v1/hello")
        self.assertEqual(raised.exception.code, "tls")


if __name__ == "__main__":
    unittest.main()
