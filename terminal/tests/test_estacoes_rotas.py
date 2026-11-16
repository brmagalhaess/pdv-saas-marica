"""Fase 3 (estações): app Flask visto pelo caixa principal e por um caixa adicional, rotas de
"Caixas da loja", regularização da promoção e as funções puras das telas (via node)."""

import json
import shutil
import subprocess
import tempfile
import time
import types
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from terminal import app as terminal_app
from terminal import sync, updater
from terminal.app import SERVER_ONLY_ENDPOINTS, create_app
from terminal.store import SERVER_ONLY_MESSAGE, TerminalError

try:  # discovered with "-s terminal/tests" or imported as terminal.tests.<module>
    from test_estacoes_modelo import MIRROR_KEY, StationCase
except ImportError:  # pragma: no cover
    from terminal.tests.test_estacoes_modelo import MIRROR_KEY, StationCase

STATIC = Path(__file__).resolve().parents[1] / "static"
TEMPLATES = Path(__file__).resolve().parents[1] / "templates"

# Section 2.2 of the phase 3 contract: what an additional cashier may never reach.
SERVER_ONLY_ROUTES = [
    ("POST", "/api/pair"), ("POST", "/api/unpair"), ("POST", "/api/new-profile"), ("POST", "/api/bootstrap"),
    ("POST", "/api/restore/start"), ("GET", "/api/restore/status"), ("POST", "/api/restore/step"),
    ("POST", "/api/restore/retry"), ("POST", "/api/restore/abandon"), ("GET", "/api/restore/backups"),
    ("POST", "/api/restore/import-backup"), ("POST", "/api/restore/skip-backup"), ("GET", "/api/export"),
    ("GET", "/api/backups"), ("POST", "/api/backups"), ("GET", "/api/mirror/drives"),
    ("POST", "/api/mirror/config"), ("POST", "/api/mirror/snapshot"), ("POST", "/api/update/apply"),
    ("GET", "/api/stations"), ("POST", "/api/stations/code"), ("POST", "/api/stations/abc/rename"),
    ("POST", "/api/stations/abc/revoke"), ("POST", "/api/stations/abc/cash/close"),
    ("POST", "/api/mode/station"), ("POST", "/api/promotion/regularize"),
]


class FakeLan:
    """Stand-in for terminal/lan.py (hooks of section 2.4)."""

    def __init__(self):
        self.pokes = 0
        self.state = {"supported": True, "running": True, "port": 8011, "addresses": ["192.168.0.10"],
                      "hostname": "CAIXA-PRINCIPAL", "fingerprint": "ab" * 32, "network_profile": "private",
                      "firewall": "ok", "error": ""}

    def status(self, _app):
        return dict(self.state)

    def poke(self, _app):
        self.pokes += 1


class FakeResponse:
    def __init__(self, body, status=200):
        self.body, self.status_code, self.ok = body, status, status < 400

    def json(self):
        return self.body


class RouteCase(StationCase):
    def setUp(self):
        super().setUp()
        self.linked = self.link("Caixa 2")
        self.station_id = self.linked["station_id"]
        self.environ = {"pdv2.station": {"id": self.station_id, "name": "Caixa 2"}}
        self.station_client = self.app.test_client()

    def use_lan(self, module="fake"):
        self.lan = FakeLan() if module == "fake" else module
        watcher = patch.object(sync, "lan", self.lan)
        watcher.start()
        self.addCleanup(watcher.stop)
        return self.lan

    # ---- the browser of the main computer
    def csrf(self):
        return self.client.get("/api/session").json["csrf"]

    def login(self, operator_id=None, pin="123987"):
        response = self.client.post("/api/login", json={"operator_id": operator_id or self.cashier_id, "pin": pin},
                                    headers={"X-CSRF-Token": self.csrf()})
        self.assertEqual(response.status_code, 200, response.json)

    def post(self, path, payload=None):
        return self.client.post(path, json=payload or {}, headers={"X-CSRF-Token": self.csrf()})

    # ---- the browser of the additional cashier, as terminal/lan.py delivers its requests
    def sget(self, path, client=None):
        return (client or self.station_client).get(path, environ_overrides=self.environ)

    def scsrf(self, client=None):
        return self.sget("/api/session", client).json["csrf"]

    def slogin(self, operator_id=None, pin="112233", client=None):
        response = (client or self.station_client).post(
            "/api/login", json={"operator_id": operator_id or self.second_id, "pin": pin},
            headers={"X-CSRF-Token": self.scsrf(client)}, environ_overrides=self.environ)
        self.assertEqual(response.status_code, 200, response.json)

    def spost(self, path, payload=None, client=None, **headers):
        return (client or self.station_client).post(
            path, json=payload or {}, headers={"X-CSRF-Token": self.scsrf(client), **headers},
            environ_overrides=self.environ)


class StationEntryTests(RouteCase):
    def test_every_server_only_route_refuses_an_additional_cashier(self):
        self.slogin(self.manager_id, "654321")
        for method, path in SERVER_ONLY_ROUTES:
            response = self.sget(path) if method == "GET" else self.spost(path, {"confirm": True})
            self.assertEqual((response.status_code, response.json),
                             (403, {"error": SERVER_ONLY_MESSAGE, "code": "server_only"}), path)
        # ...and the list covers every endpoint that really exists.
        self.assertLessEqual(SERVER_ONLY_ENDPOINTS, set(self.app.view_functions))
        rules = {rule.endpoint for rule in self.app.url_map.iter_rules()}
        matched = set()
        adapter = self.app.url_map.bind("127.0.0.1")
        for method, path in SERVER_ONLY_ROUTES:
            matched.add(adapter.match(path, method=method)[0])
        self.assertEqual(matched, set(SERVER_ONLY_ENDPOINTS) & rules)
        self.assertEqual(matched, set(SERVER_ONLY_ENDPOINTS))

    def test_the_main_computer_still_reaches_those_routes(self):
        self.login(self.manager_id, "654321")
        self.assertEqual(self.client.get("/api/backups").status_code, 200)
        self.assertEqual(self.client.get("/api/export").status_code, 200)
        self.assertEqual(self.client.get("/api/stations").status_code, 200)

    def test_what_the_additional_cashier_may_do(self):
        self.slogin()
        opened = self.spost("/api/cash/open", {"opening_cents": 1500})
        self.assertEqual(opened.status_code, 200, opened.json)
        sale = self.spost("/api/sales", {"request_id": "rota-1",
                                         "items": [{"product_id": self.product_id, "quantity_milli": 1000}],
                                         "payments": [{"method": "cash", "amount_cents": 10000}]})
        self.assertEqual((sale.status_code, sale.json["station_id"], sale.json["station_name"]),
                         (200, self.station_id, "Caixa 2"))
        self.assertEqual(self.sget("/api/cash/summary").json["expected_cents"], 11500)
        self.assertEqual(self.sget("/api/cash").json["cash"]["station_id"], self.station_id)
        self.assertIsNone(self.store.cash_state())  # the main cashier stays closed
        for path in ("/api/products", "/api/sales", "/api/print/settings", "/api/mirror/status",
                     "/api/update/status", "/api/sync"):
            self.assertEqual(self.sget(path).status_code, 200, path)
        self.assertEqual(self.sget("/api/sales").json["sales"][0]["station_name"], "Caixa 2")
        # The station thread context never leaks into the next request of the main computer.
        self.assertEqual(self.store.current_station(), "server")
        self.assertIsNone(self.client.get("/api/session").json["cash"])

    def test_session_tells_which_cashier_this_is(self):
        server = self.client.get("/api/session").json
        self.assertEqual(server["station"], {"id": "server", "name": "Caixa principal", "is_server": True})
        self.assertEqual(server["stations_limit"], {"limit": 2, "used": 1, "available": 1})
        self.assertEqual(server["promotion"]["state"], "none")
        self.assertEqual(server["license"]["max_stations"], 2)
        station = self.sget("/api/session").json
        self.assertEqual(station["station"], {"id": self.station_id, "name": "Caixa 2", "is_server": False})
        self.assertTrue(station["paired"])
        self.assertEqual(self.client.get("/health").json,
                         {"status": "ok", "service": "terminal-local", "mode": "server"})

    def test_operator_session_is_bound_to_the_cashier_it_logged_in(self):
        # A session cookie of the main computer is worth nothing on an additional cashier...
        self.login()
        self.assertEqual(self.client.get("/api/session").json["operator"]["name"], "Ana")
        self.assertIsNone(self.sget("/api/session", self.client).json["operator"])
        self.assertEqual(self.sget("/api/cash", self.client).status_code, 401)
        self.assertEqual(self.client.get("/api/cash").status_code, 200)
        # ...and the other way around.
        self.slogin()
        self.assertEqual(self.sget("/api/session").json["operator"]["name"], "Caio")
        self.assertIsNone(self.station_client.get("/api/session").json["operator"])
        self.assertEqual(self.station_client.get("/api/cash").status_code, 401)
        other = {"pdv2.station": {"id": "outra-estacao", "name": "Caixa 9"}}
        self.assertEqual(self.station_client.get("/api/cash", environ_overrides=other).status_code, 401)
        self.assertEqual(self.sget("/api/cash").status_code, 200)

    def test_manager_session_of_another_cashier_does_not_authorize_maintenance(self):
        self.slogin(self.manager_id, "654321")
        # Same cookie presented on the main computer: maintenance asks for the PIN again.
        refused = self.station_client.post("/api/unpair", json={"confirm": True},
                                           headers={"X-CSRF-Token": self.station_client.get("/api/session").json["csrf"]})
        self.assertEqual(refused.status_code, 403)
        self.assertIn("PIN de gerente", refused.json["error"])

    def test_host_origin_and_csrf_are_still_required(self):
        body = {"operator_id": self.second_id, "pin": "112233"}
        no_csrf = self.station_client.post("/api/login", json=body, environ_overrides=self.environ)
        self.assertEqual(no_csrf.status_code, 403)
        self.assertIn("CSRF", no_csrf.json["error"])
        wrong_origin = self.spost("/api/login", body, Origin="http://192.168.0.10:8011")
        self.assertEqual((wrong_origin.status_code, wrong_origin.json["error"]), (403, "Origem não permitida"))
        right_origin = self.spost("/api/login", body, Origin="http://localhost")
        self.assertEqual(right_origin.status_code, 200)
        wrong_host = self.station_client.get("/api/session", headers={"Host": "192.168.0.10:8011"},
                                             environ_overrides=self.environ)
        self.assertEqual((wrong_host.status_code, wrong_host.json["error"]), (403, "Host não permitido"))

    def test_a_forged_header_is_not_a_station(self):
        forged = self.client.get("/api/session", headers={"X-PDV-Station": self.station_id,
                                                          "Pdv2.Station": self.station_id})
        self.assertTrue(forged.json["station"]["is_server"])

    def test_flush_is_delegated_to_the_cashier_that_owns_the_queue(self):
        target = str(Path(self.temp.name) / "impressora-estacao")
        with self.store.station(self.station_id):
            self.store.save_printer("receipt", "file", target, "escpos58")
            self.store.open_cash(self.second_id, 0)
            self.sell("com-recibo", self.second)
        self.slogin()
        self.assertEqual(self.sget("/api/print/settings").json["settings"][0]["target"], target)
        with patch.object(sync, "print_once") as printed:
            self.assertEqual(self.spost("/api/print/flush").json, {"status": "delegated"})
            printed.assert_not_called()
        self.assertEqual(self.sget("/api/print/settings").json["jobs"][0]["status"], "queued")
        self.login()
        self.assertEqual(self.client.get("/api/print/settings").json, {"settings": [], "jobs": []})
        self.assertEqual(self.post("/api/print/flush").json, {"status": "empty"})

    def test_pin_lockout_through_the_routes_is_per_cashier(self):
        for _ in range(5):
            wrong = self.spost("/api/login", {"operator_id": self.cashier_id, "pin": "000000"})
            self.assertEqual(wrong.status_code, 401)
        self.assertEqual(self.spost("/api/login", {"operator_id": self.cashier_id, "pin": "123987"}).status_code, 429)
        self.login()  # the same operator, on the main computer

    def test_damaged_database_tells_the_station_without_file_paths(self):
        folder = Path(self.temp.name) / "danificado"
        folder.mkdir()
        path = folder / "terminal.sqlite3"
        path.write_bytes(b"isto nao e um banco sqlite, e so lixo" * 200)
        with self.assertLogs("terminal.app", level="ERROR"):
            app = create_app({"TESTING": True, "DATA_DIR": str(folder), "DB_PATH": path, "START_WORKER": False})
        self.assertIsNone(app.extensions["terminal_store"])
        client = app.test_client()
        for path_name in ("/api/session", "/api/products", "/api/cash"):
            response = client.get(path_name, environ_overrides=self.environ)
            self.assertEqual((response.status_code, response.json),
                             (503, {"error": "O caixa principal está com o banco de dados danificado. "
                                             "Chame o suporte.", "damaged": True}), path_name)
            self.assertNotIn(str(folder), response.get_data(as_text=True))
        self.assertEqual(client.get("/", environ_overrides=self.environ).status_code, 200)
        self.assertEqual(client.get("/health", environ_overrides=self.environ).status_code, 503)
        self.assertEqual(client.get("/api/export", environ_overrides=self.environ).json["code"], "server_only")
        self.assertIn("damaged", client.get("/api/session").json)  # the main computer still gets the guide

    def test_data_dir_is_published_for_the_network_modules(self):
        self.assertEqual(Path(self.app.extensions["terminal_data_dir"]), Path(self.temp.name))
        self.assertNotIn("terminal_lan", self.app.extensions)  # create_app never opens a network port


class StationsRoutesTests(RouteCase):
    def test_listing_needs_a_manager_and_never_shows_the_token(self):
        self.assertEqual(self.client.get("/api/stations").status_code, 401)
        self.login()
        self.assertEqual(self.client.get("/api/stations").status_code, 403)
        self.login(self.manager_id, "654321")
        without = self.client.get("/api/stations").json
        self.assertEqual((without["supported"], without["replica"]), (sync.lan is not None, without["replica"]))
        self.use_lan()
        data = self.client.get("/api/stations").json
        self.assertEqual(set(data), {"supported", "limit", "lan", "replica", "stations"})
        self.assertTrue(data["supported"])
        self.assertEqual(data["limit"], {"limit": 2, "used": 1, "available": 1})
        self.assertEqual(data["lan"]["addresses"], ["192.168.0.10"])
        self.assertEqual([(row["name"], row["is_server"], row["linked"]) for row in data["stations"]],
                         [("Caixa principal", True, True), ("Caixa 2", False, True)])
        self.assertNotIn(self.linked["token"], json.dumps(data))
        self.assertNotIn("token_hash", json.dumps(data))

    def test_without_the_network_module_the_screen_is_told(self):
        self.login(self.manager_id, "654321")
        with patch.object(sync, "lan", None), patch.object(sync, "replica_net", None):
            data = self.client.get("/api/stations").json
            self.assertEqual((data["supported"], data["replica"]), (False, {"supported": False}))
            self.assertEqual(data["lan"], {"supported": False, "running": False,
                                           "message": "Rede entre caixas indisponível nesta instalação"})
            refused = self.post("/api/stations/code", {})
            self.assertEqual((refused.status_code, refused.json["error"]),
                             (409, "Rede entre caixas indisponível nesta instalação"))

    def test_a_failing_network_module_never_breaks_the_screen(self):
        broken = types.SimpleNamespace(status=lambda _app: 1 / 0, poke=lambda _app: 1 / 0)
        replica = types.SimpleNamespace(status=lambda store: {"supported": True, "state": "ok", "message": "",
                                                              "stations": {self.station_id: {"behind": 0}}})
        self.use_lan(broken)
        self.login(self.manager_id, "654321")
        with patch.object(sync, "replica_net", replica), self.assertLogs("terminal.app", level="ERROR"):
            data = self.client.get("/api/stations").json
            self.assertEqual((data["lan"]["running"], data["replica"]["state"]), (False, "ok"))
            self.assertEqual(self.post("/api/stations/code", {}).status_code, 201)

    def test_code_rename_revoke_and_closing_the_cashier_of_another_computer(self):
        lan = self.use_lan()
        self.login()
        self.assertEqual(self.post("/api/stations/code", {}).status_code, 403)
        self.login(self.manager_id, "654321")
        created = self.post("/api/stations/code", {"name": "Caixa do fundo"})
        self.assertEqual(created.status_code, 201, created.json)
        self.assertRegex(created.json["code"], r"^\d{6}$")
        self.assertEqual(created.json["expires_in"], 300)
        self.assertEqual(created.json["server"], {"addresses": ["192.168.0.10"], "port": 8011,
                                                  "hostname": "CAIXA-PRINCIPAL"})
        self.assertEqual(created.headers["Cache-Control"], "no-store")
        self.assertEqual(lan.pokes, 1)
        new = self.store.enroll_station(created.json["code"])
        self.assertEqual(new["station_name"], "Caixa do fundo")
        full = self.post("/api/stations/code", {})
        self.assertEqual((full.status_code, full.json["code"]), (409, "station_limit"))

        renamed = self.post(f"/api/stations/{new['station_id']}/rename", {"name": "Balcão"})
        self.assertEqual((renamed.status_code, renamed.json["name"]), (200, "Balcão"))
        missing = self.post("/api/stations/nao-existe/rename", {"name": "Outro"})
        self.assertEqual((missing.status_code, missing.json["error"]), (404, "Caixa não encontrado"))

        with self.store.station(self.station_id):
            self.store.open_cash(self.second_id, 3000)
        self.assertEqual(self.post(f"/api/stations/{self.station_id}/revoke", {}).status_code, 409)
        revoked = self.post(f"/api/stations/{self.station_id}/revoke", {"confirm": True})
        self.assertEqual(revoked.status_code, 200, revoked.json)
        self.assertTrue(revoked.json["revoked_at"])
        self.assertTrue(revoked.json["cash_open"])  # it stays open until the manager counts the drawer
        self.assertEqual(lan.pokes, 2)
        with self.assertRaises(TerminalError) as refused:
            self.store.authenticate_station(self.linked["token"])
        self.assertEqual(refused.exception.code, "station_revoked")
        self.assertEqual(self.post("/api/stations/server/revoke", {"confirm": True}).status_code, 409)

        wrong_pin = self.post(f"/api/stations/{self.station_id}/cash/close", {"counted_cents": 3000,
                                                                               "manager_pin": "000000"})
        self.assertEqual(wrong_pin.status_code, 403)
        closed = self.post(f"/api/stations/{self.station_id}/cash/close", {"counted_cents": 2900,
                                                                            "manager_pin": "654321"})
        self.assertEqual((closed.status_code, closed.json["expected_cents"], closed.json["difference_cents"]),
                         (200, 3000, -100))
        again = self.post(f"/api/stations/{self.station_id}/cash/close", {"counted_cents": 0,
                                                                           "manager_pin": "654321"})
        self.assertEqual((again.status_code, again.json["error"]), (409, "Abra o caixa antes da operação"))
        self.assertFalse(self.store.any_cash_open())

    def test_update_waits_for_every_cashier_to_close(self):
        fake = types.SimpleNamespace(status=lambda store: {
            "current_version": "2.0.0", "available": {"version": "2.1.0"}, "staged_version": "2.1.0",
            "state": "staged", "error": "", "can_apply": True, "blocked_reason": ""})
        self.login(self.manager_id, "654321")
        with patch.object(sync, "updater", fake):
            self.assertTrue(self.client.get("/api/update/status").json["can_apply"])
            with self.store.station(self.station_id):
                self.store.open_cash(self.second_id, 0)
            status = self.client.get("/api/update/status").json
            self.assertEqual((status["can_apply"], status["blocked_reason"]),
                             (False, "Feche o caixa em todos os computadores para atualizar"))
            refused = self.post("/api/update/apply", {})
            self.assertEqual(refused.status_code, 409)
            self.assertIn("todos os computadores", refused.json["error"])
        # The updater itself asks the same question before applying or downloading.
        self.assertIsNone(self.store.cash_state())
        self.assertTrue(updater._cash_open(self.store))
        older = types.SimpleNamespace(cash_state=lambda: None)
        self.assertFalse(updater._cash_open(older))
        self.assertTrue(updater._cash_open(types.SimpleNamespace(cash_state=lambda: {"id": "c1"})))

    def test_new_profile_waits_for_every_cashier_to_close(self):
        with self.store.station(self.station_id):
            self.store.open_cash(self.second_id, 0)
        self.store.ack_events([event["event_id"] for event in self.store.pending_events()])
        refused = self.post("/api/new-profile", {"confirm": True, "manager_pin": "654321"})
        self.assertEqual((refused.status_code, refused.json["error"]),
                         (409, "Feche o caixa antes de criar outro perfil"))


class ModeStationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.app = create_app({"TESTING": True, "DATA_DIR": self.temp.name, "START_WORKER": False})
        self.client = self.app.test_client()
        self.written = []

    def post(self, payload):
        return self.client.post("/api/mode/station", json=payload,
                                headers={"X-CSRF-Token": self.client.get("/api/session").json["csrf"]})

    def writer(self):
        return patch.object(terminal_app.paths, "write_mode", create=True,
                            side_effect=lambda data_dir, mode: self.written.append((Path(data_dir), mode)))

    def test_an_empty_computer_becomes_an_additional_cashier(self):
        restarts = []
        runtime = types.SimpleNamespace(request_restart=lambda reason: restarts.append(reason) or True)
        with self.writer(), patch.object(sync, "runtime", runtime):
            self.assertEqual(self.post({}).status_code, 409)
            self.assertEqual(self.written, [])
            answer = self.post({"confirm": True})
        self.assertEqual((answer.status_code, answer.json), (200, {"mode": "station", "restart": True}))
        self.assertEqual(self.written, [(Path(self.temp.name), "station")])
        self.assertEqual(restarts, ["modo-estacao"])

    def test_without_a_supervisor_the_person_reopens_the_program(self):
        with self.writer(), patch.object(sync, "runtime", None):
            self.assertEqual(self.post({"confirm": True}).json, {"mode": "station", "restart": False})
        failing = types.SimpleNamespace(request_restart=lambda reason: 1 / 0)
        with self.writer(), patch.object(sync, "runtime", failing), self.assertLogs("terminal.app", level="ERROR"):
            self.assertEqual(self.post({"confirm": True}).json, {"mode": "station", "restart": False})
        with patch.object(terminal_app.paths, "write_mode", None, create=True):
            refused = self.post({"confirm": True})
        self.assertEqual((refused.status_code, refused.json["error"]),
                         (409, "Rede entre caixas indisponível nesta instalação"))

    def test_csrf_is_required(self):
        self.assertEqual(self.client.post("/api/mode/station", json={"confirm": True}).status_code, 403)


class ModeStationPairedTests(RouteCase):
    def test_a_computer_that_serves_a_shop_never_changes_role(self):
        written = []
        with patch.object(terminal_app.paths, "write_mode", create=True,
                          side_effect=lambda *args: written.append(args)):
            refused = self.post("/api/mode/station", {"confirm": True})
        self.assertEqual((refused.status_code, refused.json["error"]),
                         (409, "Este computador já é o caixa principal de uma loja."))
        self.assertEqual(written, [])


class RegularizeTests(RouteCase):
    BODY = {"license_key": "PDV2-ABCD-EFGH-IJKL", "code": "ABCD-EFGH-IJKL", "manager_pin": "654321"}

    def setUp(self):
        super().setUp()
        self.store.finish_promotion(self.station_id, "Caixa 2", MIRROR_KEY, self.manager_id)
        self.calls = []

    def pairing(self, **changes):
        answer = {"token": "token-novo-" + "z" * 40, "tenant_id": self.tenant_id, "shop_id": self.shop_id,
                  "terminal_id": self.terminal_id, "public_key": self.public, "recovery": True,
                  "lease": self.signed(max_stations=2, device_id=self.store.promotion_device_id(),
                                       issued=int(time.time())),
                  "capabilities": ["stations", "restore"]}
        answer.update(changes)
        return answer

    def cloud(self, pair_answer, bootstrap=None):
        def request(method, url, **kwargs):
            self.calls.append((method, url, kwargs))
            if url.endswith("/api/v1/terminal/pair"):
                return FakeResponse(pair_answer)
            if url.endswith("/api/v1/terminal/release"):
                return FakeResponse({"released": True})
            if "/terminal/bootstrap" in url and bootstrap is not None:
                return FakeResponse(bootstrap)
            return FakeResponse({"error": "indisponível"}, 503)
        return patch.object(sync.requests, "request", side_effect=request)

    def test_the_promoted_computer_is_bound_to_the_same_terminal(self):
        session = self.client.get("/api/session").json
        self.assertEqual((session["promotion"]["state"], session["paired"], session["license"]["can_sell"]),
                         ("awaiting_cloud", True, True))
        with self.cloud(self.pairing()):
            no_pin = self.post("/api/promotion/regularize", dict(self.BODY, manager_pin="000000"))
            self.assertEqual(no_pin.status_code, 403)
            self.assertEqual(self.calls, [])
            answer = self.post("/api/promotion/regularize", self.BODY)
        self.assertEqual(answer.status_code, 200, answer.json)
        self.assertEqual((answer.json["regularized"], answer.json["promotion"]["state"], answer.json["bootstrap"]),
                         (True, "done", None))
        self.assertIn("warning", answer.json)  # the first catalog load failed: the worker repeats it
        method, url, sent = self.calls[0]
        self.assertEqual((method, url), ("POST", "http://127.0.0.1:8000/api/v1/terminal/pair"))
        self.assertEqual(sent["json"]["device_id"], self.store.promotion_device_id())
        self.assertEqual((sent["json"]["license_key"], sent["json"]["code"]), (self.BODY["license_key"], self.BODY["code"]))
        self.assertFalse(any("/terminal/release" in call[1] for call in self.calls))
        meta = self.store.meta()
        self.assertEqual((meta["device_id"], meta["promotion_state"]), (self.store.promotion_device_id(), "done"))
        self.assertTrue(meta["token"].startswith("token-novo-"))
        self.assertFalse(self.store.restore_status()["active"])
        self.assertTrue(self.store.lease_status()["can_sell"])
        # Nothing secret reached the audit trail.
        trail = json.dumps(self.store.audit())
        for secret in (meta["token"], self.BODY["license_key"], self.BODY["code"], MIRROR_KEY["key"]):
            self.assertNotIn(secret, trail)
        with self.cloud(self.pairing()):
            again = self.post("/api/promotion/regularize", self.BODY)
        self.assertEqual(again.status_code, 409)

    def test_a_logged_manager_needs_no_pin_and_the_catalog_comes_with_it(self):
        self.login(self.manager_id, "654321")
        bootstrap = {"products": self.products, "operators": self.operators, "customers": self.customers,
                     "public_key": self.public, "next_cursor": None}
        answer_pairing = self.pairing()
        bootstrap["lease"] = answer_pairing["lease"]
        with self.cloud(answer_pairing, bootstrap):
            answer = self.post("/api/promotion/regularize", {"license_key": self.BODY["license_key"],
                                                             "code": self.BODY["code"]})
        self.assertEqual(answer.status_code, 200, answer.json)
        self.assertEqual(answer.json["bootstrap"]["products"], 1)
        self.assertNotIn("warning", answer.json)

    def test_a_code_of_another_terminal_is_given_back_to_its_computer(self):
        foreign = self.pairing(terminal_id=str(uuid.uuid4()), token="token-alheio-" + "y" * 40)
        with self.cloud(foreign):
            refused = self.post("/api/promotion/regularize", self.BODY)
        self.assertEqual((refused.status_code, refused.json["code"]), (409, "other_terminal"))
        self.assertIn("Trocar de computador", refused.json["error"])
        self.assertEqual([call[1].rsplit("/", 1)[1] for call in self.calls], ["pair", "release"])
        self.assertEqual(self.calls[1][2]["headers"]["Authorization"], "Bearer " + foreign["token"])
        self.assertEqual(self.store.promotion_status()["state"], "awaiting_cloud")
        self.assertNotIn("token", self.store.meta())
        self.assertNotIn(foreign["token"], json.dumps(self.store.audit()))

    def test_cloud_refusals_and_bad_input_change_nothing(self):
        with self.cloud({"error": "Código inválido"}) as _:
            with patch.object(sync.requests, "request", return_value=FakeResponse({"error": "Código inválido"}, 400)):
                refused = self.post("/api/promotion/regularize", self.BODY)
        self.assertEqual(refused.status_code, 502)
        self.assertIn("Código inválido", refused.json["error"])
        with patch.object(sync.requests, "request", side_effect=sync.requests.ConnectionError("sem rede")):
            offline = self.post("/api/promotion/regularize", self.BODY)
        self.assertEqual(offline.status_code, 503)
        with patch.object(sync.requests, "request") as never:
            self.assertEqual(self.post("/api/promotion/regularize", dict(self.BODY, code="x")).status_code, 400)
            self.assertEqual(self.post("/api/promotion/regularize",
                                       dict(self.BODY, cloud_url="http://example.com")).status_code, 400)
            never.assert_not_called()
        self.assertEqual(self.store.promotion_status()["state"], "awaiting_cloud")

    def test_the_worker_stays_quiet_until_the_cloud_knows_this_computer(self):
        worker = sync.Worker(self.store)
        with patch.object(sync.requests, "request") as never, patch.object(sync, "updater", None):
            worker.run_once()
            never.assert_not_called()


class ScreenTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = (TEMPLATES / "index.html").read_text(encoding="utf-8")
        cls.script = (STATIC / "terminal.js").read_text(encoding="utf-8")
        cls.style = (STATIC / "terminal.css").read_text(encoding="utf-8")

    def test_page_has_the_station_elements_in_plain_portuguese(self):
        for element_id in ("stationModeBtn", "stationsCard", "stationAddBtn", "stationsList", "serverDownView",
                           "promotionCard", "promotionForm", "stationPrinterHint", "identityPlace"):
            self.assertIn(f'id="{element_id}"', self.html, element_id)
        for text in ("Este é um caixa adicional (a loja já tem um caixa principal)", "Caixas da loja",
                     "Adicionar caixa", "O caixa principal está fora do ar",
                     "Este caixa volta sozinho quando a conexão voltar. O que você já lançou continua na tela.",
                     "Ver o que aconteceu", "Este computador assumiu como caixa principal.", "Regularizar agora",
                     "Impressora deste caixa"):
            self.assertIn(text, self.html, text)
        self.assertIn('href="/station/"', self.html)
        self.assertLess(self.html.index('id="restoreOpenBtn"'), self.html.index('id="stationModeBtn"'))

    def test_script_follows_the_contract(self):
        for piece in ("/api/mode/station", "/api/stations/code", "/api/promotion/regularize", "/cash/close",
                      "Preparando este computador…", "Feche e abra o PDV2 de novo.", "Vale por 5 minutos",
                      "Se ele não encontrar sozinho, digite lá o endereço:", "Remover caixa", "Renomear",
                      "Fechar o caixa deste computador", "location.replace('/station/')", "error.code = body.code",
                      "setInterval(probeServer, 2000)", "health.mode === 'station'"):
            self.assertIn(piece, self.script, piece)
        # The overlay never throws the sale away: no reset of the request id or of the cart in it.
        start = self.script.index("function showServerDown")
        overlay = self.script[start:self.script.index("/* ---------- barcode reader bursts", start)]
        for forbidden in ("saleRequest", "clearLocalSale", "resetSale", "sessionStorage", "state.cart ="):
            self.assertNotIn(forbidden, overlay, forbidden)
        for selector in (".server-down", ".promotion-card", ".enroll-code"):
            self.assertIn(selector, self.style)


@unittest.skipUnless(shutil.which("node"), "node não está disponível")
class StationCoreTests(unittest.TestCase):
    """Pure functions of terminal-core.js, run in node."""

    @classmethod
    def setUpClass(cls):
        script = """
const C = require(process.argv[1]);
const seen = new Date(2026, 10, 16, 9, 5).getTime();
const stations = [
  {id: 'server', name: 'Caixa principal', is_server: true, linked: true, online: true, cash_open: true},
  {id: 'a', name: 'Caixa 2', linked: true, online: true, cash_open: true},
  {id: 'b', name: 'Caixa 3', linked: true, online: false, last_seen_at: seen, cash_open: false},
  {id: 'c', name: 'Caixa 4', linked: false, online: false, cash_open: false},
  {id: 'd', name: 'Antigo', linked: false, revoked_at: 5, cash_open: true},
  {id: 'e', name: 'Sumiu', linked: false, revoked_at: 5, cash_open: false},
];
const out = {
  code: [C.enrollCodeText('483920'), C.enrollCodeText(' 48 39 20 '), C.enrollCodeText('12'), C.enrollCodeText(null)],
  server: C.stationNotice({station: {id: 'server', name: 'Caixa principal', is_server: true}, stations_limit: {limit: 2, used: 1},
    connection: {shop_name: 'Mercado X', terminal_name: 'Caixa 1'}}),
  alone: C.stationNotice({station: {id: 'server', name: 'Caixa principal', is_server: true}, stations_limit: {limit: 0, used: 0},
    connection: {shop_name: 'Mercado X', terminal_name: 'Caixa 1'}}),
  old: C.stationNotice({connection: {tenant_name: 'Empresa'}}),
  station: C.stationNotice({station: {id: 'a', name: 'Caixa 2', is_server: false}, connection: {shop_name: 'Mercado X', terminal_name: 'Caixa 1'}}),
  empty: C.stationNotice(null),
  card: C.stationsCard({supported: true, limit: {limit: 4, used: 3, available: 1}, lan: {supported: true, running: true, network_profile: 'private'},
    replica: {supported: true, stations: {a: {behind: 0}, b: {behind: 7}}}, stations}),
  full: C.stationsCard({supported: true, limit: {limit: 1, used: 1}, lan: {supported: true}, stations: stations.slice(0, 2)}),
  relink: C.stationsCard({supported: true, limit: {limit: 1, used: 1}, lan: {supported: true}, stations: [stations[0], stations[3]]}),
  noPlan: C.stationsCard({supported: true, limit: {limit: 0, used: 0}, lan: {supported: true}, stations: stations.slice(0, 1)}),
  noLan: C.stationsCard({supported: false, limit: {limit: 2, used: 0}, lan: {supported: false}, stations: []}),
  publicNetwork: C.stationsCard({supported: true, limit: {limit: 2, used: 0}, lan: {supported: true, network_profile: 'public'}, stations: []}),
  nothing: C.stationsCard(null),
  promotion: [C.promotionCard(null), C.promotionCard({state: 'none'}), C.promotionCard({state: 'done'}),
    C.promotionCard({state: 'awaiting_cloud', previous_server_cash_open: false, pending_events: 0}),
    C.promotionCard({state: 'awaiting_cloud', previous_server_cash_open: true, pending_events: 12})],
  codes: [C.SERVER_DOWN_CODES, C.STATION_GONE_CODES],
};
console.log(JSON.stringify(out));
"""
        result = subprocess.run(["node", "-e", script, str(STATIC / "terminal-core.js")],
                                capture_output=True, text=True, timeout=60, check=False)
        if result.returncode:
            raise AssertionError(result.stderr)
        cls.out = json.loads(result.stdout)

    def test_enroll_code_is_shown_in_two_groups(self):
        self.assertEqual(self.out["code"], ["483 920", "483 920", "12", ""])

    def test_identity_of_the_cashier_in_the_status_bar(self):
        self.assertEqual((self.out["server"]["place"], self.out["server"]["isStation"], self.out["server"]["hidden"]),
                         ("Mercado X · Caixa principal", False, []))
        # A shop with a single computer keeps the name it gave the terminal.
        self.assertEqual(self.out["alone"]["place"], "Mercado X · Caixa 1")
        self.assertEqual(self.out["old"]["place"], "Empresa · Caixa")
        self.assertEqual(self.out["empty"]["place"], "Loja · Caixa")
        station = self.out["station"]
        self.assertEqual((station["place"], station["isStation"], station["printerHint"]),
                         ("Mercado X · Caixa 2", True, "Impressora deste caixa"))
        self.assertLessEqual({"backupCard", "mirrorChooseBtn", "updateApplyBtn", "stationsCard", "changeConnection",
                              "newProfileLoginBtn"}, set(station["hidden"]))

    def test_stations_card(self):
        card = self.out["card"]
        self.assertEqual(card["summary"], "Seu plano inclui 4 caixa(s) adicional(is); 3 em uso.")
        self.assertEqual((card["canAdd"], card["addBlocked"], card["warning"]), (True, "", ""))
        rows = {row["id"]: row for row in card["rows"]}
        self.assertEqual(list(rows), ["server", "a", "b", "c", "d"])  # a removed one stays only while its cashier is open
        self.assertEqual((rows["server"]["status"], rows["server"]["canRevoke"], rows["server"]["canCloseCash"],
                          rows["server"]["copy"]), ("Este computador", False, False, ""))
        self.assertEqual((rows["a"]["status"], rows["a"]["cash"], rows["a"]["copy"], rows["a"]["canCloseCash"],
                          rows["a"]["canRevoke"], rows["a"]["canRename"]),
                         ("Conectado agora", "Caixa aberto", "Cópia de segurança em dia", True, True, True))
        self.assertEqual((rows["b"]["status"], rows["b"]["cash"], rows["b"]["copy"], rows["b"]["canCloseCash"]),
                         ("Visto por último às 09:05", "Caixa fechado", "Cópia de segurança atrasada", False))
        self.assertEqual(rows["c"]["status"], "Precisa ser conectado de novo")
        self.assertEqual((rows["d"]["status"], rows["d"]["canCloseCash"], rows["d"]["canRevoke"], rows["d"]["canRename"]),
                         ("Removido", True, False, False))

    def test_adding_is_blocked_with_a_reason(self):
        self.assertEqual((self.out["full"]["canAdd"], self.out["relink"]["canAdd"]), (False, True))
        self.assertIn("já estão em uso", self.out["full"]["addBlocked"])
        self.assertFalse(self.out["noPlan"]["canAdd"])
        self.assertIn("não inclui caixas adicionais", self.out["noPlan"]["summary"])
        self.assertEqual(self.out["noLan"]["addBlocked"], "Rede entre caixas indisponível nesta instalação.")
        self.assertEqual(self.out["publicNetwork"]["warning"],
                         "A rede deste computador está marcada como Pública no Windows. Mude para Particular para os "
                         "outros caixas enxergarem este.")
        self.assertEqual((self.out["nothing"]["rows"], self.out["nothing"]["canAdd"]), ([], False))

    def test_promotion_card(self):
        none, plain, done, waiting, with_cash = self.out["promotion"]
        self.assertEqual((none, plain, done), (None, None, None))
        self.assertIn("Terminais → Trocar de computador", waiting["text"])
        self.assertEqual(waiting["warning"], "")
        self.assertEqual(with_cash["warning"], "O caixa do computador anterior ficou aberto. Conte o dinheiro e feche "
                                               "em Sistema → Caixas da loja.")
        self.assertIn("12 registro(s) aguardam o envio.", with_cash["text"])
        self.assertEqual(self.out["codes"], [["server_unreachable", "server_timeout"],
                                             ["station_revoked", "station_unauthorized"]])


if __name__ == "__main__":
    unittest.main()
