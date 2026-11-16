"""Fase 3, correções da integração: o que só apareceu quando núcleo, rede, promoção e nuvem se encontraram.

Escuta TLS de verdade (terminal.lan) em portas efêmeras, com o banco de mentira de test_lan onde o
banco não é o assunto. Os ensaios com o terminal inteiro estão em test_e2e_estacoes.py e test_e2e_promocao.py.
"""

import json
import os
import socket
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from terminal import lan, lanclient, station
from terminal.lanclient import LanClient, LanError

try:
    from terminal.tests.test_lan import BROWSER, HOST, FakeApp, FakeStore, LanCase
    from terminal.tests.test_station import BASE, LOCAL, FakePromotion, StationCase
except ImportError:  # descoberto como módulo de topo
    from test_lan import BROWSER, HOST, FakeApp, FakeStore, LanCase
    from test_station import BASE, LOCAL, FakePromotion, StationCase

KEY = "k" * 43 + "="
PAGE = {"Accept": "text/html,application/xhtml+xml"}
FROM_BROWSER = {"Host": "127.0.0.1:8010", "Origin": BASE}


class KeyedStore(FakeStore):
    """Banco de mentira com a chave da cópia da loja e a geração do caixa principal."""

    def __init__(self, *args, generation=1, key=KEY, **kwargs):
        super().__init__(*args, **kwargs)
        self.generation, self.key = generation, key
        self.superseded_by = None

    def meta(self):
        values = {"server_generation": str(self.generation), "terminal_id": self.terminal_id}
        if self.key:
            values["mirror_key"] = json.dumps({"key_id": "chave-1", "key": self.key})
        return values

    def mark_superseded(self, generation, fingerprint=""):
        self.superseded_by = (generation, fingerprint)
        return True

    def superseded(self):
        return self.superseded_by is not None


def serve(case, store):
    """Mais uma escuta TLS de verdade, para outro banco, no mesmo computador."""
    temp = tempfile.TemporaryDirectory()
    case.addCleanup(temp.cleanup)
    app = FakeApp(store, temp.name)
    server = lan.start(app, host=HOST, port=0)
    case.addCleanup(server.stop)
    case.assertTrue(server.running, server.error)
    return app, server


def announce(server, store):
    return {"address": HOST, "port": server.port, "fingerprint": server.fingerprint, "shop_id": store.shop_id,
            "terminal_id": store.terminal_id, "shop_name": "Mercado X", "terminal_name": "Caixa 1",
            "host": "CAIXA-NOVO", "api": 1, "version": "2.1.0", "generation": store.generation}


# ------------------------------------------------------------------ rotas nativas

class NativeRoutesTests(LanCase):
    def test_native_routes_only_answer_the_agents_of_a_station(self):
        station_id, token = self.store.add_station("Caixa 2")
        self.store.queue(station_id, b"recibo")
        for method, path in (("GET", "/lan/v1/ping"), ("GET", "/lan/v1/replica/key"),
                             ("GET", "/lan/v1/replica/status"), ("POST", "/lan/v1/print/next"),
                             ("POST", "/lan/v1/print/recover"), ("GET", "/LAN/v1/ping"), ("GET", "//lan/v1/ping")):
            response, data = self.call(method, path, token=token, headers=dict(FROM_BROWSER),
                                       body=b"{}" if method == "POST" else None)
            self.assertEqual((response.status, data["code"]), (404, "not_found"), path)
        self.assertEqual(self.store.sending, {}, "the receipt stayed in the queue")
        self.assertEqual(self.store.recovered, [])
        self.assertEqual(self.app.calls, [], "and nothing under /lan reaches the app of the shop")
        # The agent of the station (monitor, print, copy puller) sends the header.
        response, data = self.call("POST", "/lan/v1/print/next", token=token, body=b"{}", headers=dict(BROWSER))
        self.assertEqual((response.status, bool(data["job"])), (200, True))

    def test_hello_proves_the_generation_only_to_who_asks_with_a_challenge(self):
        store = KeyedStore(generation=3)
        _app, server = serve(self, store)
        client = LanClient(HOST, server.port, server.fingerprint, None)
        self.addCleanup(client.close)
        plain = client.request("GET", "/lan/v1/hello")
        self.assertNotIn("proof", plain.json())
        challenge = os.urandom(16).hex()
        body = client.request("GET", "/lan/v1/hello?challenge=" + challenge).json()
        self.assertEqual(body["generation"], 3)
        self.assertEqual(body["proof"], lanclient.generation_proof(KEY, challenge, store.terminal_id, 3,
                                                                   server.fingerprint))
        self.assertNotIn(KEY, json.dumps(body))
        self.assertNotIn("proof", client.request("GET", "/lan/v1/hello?challenge=curto").json())


# ---------------------------------------------------------- banco danificado

class DamagedFromTheStartTests(unittest.TestCase):
    def setUp(self):
        self._env = {key: os.environ.get(key) for key in ("PDV_LAN_PORT", "PDV_DISCOVERY_PORT", "PDV_LAN_HOST")}
        os.environ.update(PDV_LAN_PORT="0", PDV_DISCOVERY_PORT="0", PDV_LAN_HOST=HOST)
        self.addCleanup(lambda: [os.environ.pop(key, None) if value is None else os.environ.__setitem__(key, value)
                                 for key, value in self._env.items()])
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def damaged_app(self):
        app = FakeApp(None, self.temp.name)
        app.extensions["terminal_damage"] = {"reason": "malformed"}
        return app

    def test_server_that_starts_damaged_keeps_answering_the_stations_it_had(self):
        known = lan.ensure_certificate(self.temp.name, "loja-1")["fingerprint"]  # the shop already had stations
        server = lan.start(self.damaged_app(), host=HOST, port=0)
        self.addCleanup(server.stop)
        self.assertTrue(server.running, server.error)
        self.assertEqual(server.fingerprint, known, "the same identity the stations pinned")
        client = LanClient(HOST, server.port, known, "t" * 43)
        self.addCleanup(client.close)
        response = client.request("GET", "/lan/v1/ping", headers=dict(BROWSER))
        self.assertEqual((response.status, response.headers["x-pdv-lan-error"]), (503, "server_damaged"))
        self.assertTrue(response.json()["damaged"])
        self.assertIsNone(server._discovery_info(), "nobody new is invited to a damaged server")

    def test_station_of_a_server_that_starts_damaged_does_not_offer_to_take_over(self):
        store = FakeStore()
        station_id, token = store.add_station("Caixa 2")
        lan.ensure_certificate(self.temp.name, store.shop_id)
        server = lan.start(self.damaged_app(), host=HOST, port=0)
        self.addCleanup(server.stop)
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        station.save_state(folder.name, {
            "v": 1, "station_id": station_id, "station_name": "Caixa 2", "token": token,
            "server": {"address": HOST, "port": server.port, "host": "PRINCIPAL", "fingerprint": server.fingerprint,
                       "shop_id": store.shop_id, "terminal_id": store.terminal_id, "generation": 1},
            "addresses": [HOST], "linked_at": 1})
        app = station.create_app({"TESTING": True, "DATA_DIR": folder.name, "DISCOVER": lambda shop_id=None: [],
                                  "PROMOTE_AFTER_SECONDS": 0, "LOCAL_ADDRESSES": lambda: ["192.168.0.20"]})
        self.addCleanup(station.stop_background, app, 2.0)
        bridge = app.extensions["station"]
        with patch.object(station, "promotion", FakePromotion()):
            self.assertEqual(bridge.check_once(), "damaged")
            status = app.test_client().get("/station/api/status", base_url=BASE).json
        self.assertEqual((status["state"], status["can_promote"], status["diagnosis"]), ("damaged", False, []))

    def test_a_damaged_computer_that_never_served_stations_opens_nothing(self):
        server = lan.start(self.damaged_app(), host=HOST, port=0)
        self.addCleanup(server.stop)
        self.assertFalse(server.running)
        self.assertFalse((Path(self.temp.name) / "lan" / "cert.pem").exists(), "no new identity is created")

    def test_disabled_installation_never_listens(self):
        store = FakeStore()
        with patch.dict(os.environ, {"PDV_LAN_DISABLED": "1"}):
            app = FakeApp(store, self.temp.name)
            server = lan.start(app, host=HOST, port=0)
            self.addCleanup(server.stop)
            self.assertFalse(server.running)
            self.assertFalse(lan.status(app)["supported"])
        self.assertTrue(lan.status(app)["supported"])

    def test_busy_port_is_explained_without_the_name_of_an_exception(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as taken:
            taken.bind((HOST, 0))
            taken.listen(1)
            with self.assertLogs("pdv2.lan", level="ERROR"):
                server = lan.start(FakeApp(FakeStore(), self.temp.name), host=HOST, port=taken.getsockname()[1])
            self.addCleanup(server.stop)
            self.assertFalse(server.running)
            self.assertEqual(server.error, f"Não foi possível abrir a rede entre caixas (a porta "
                                           f"{taken.getsockname()[1]} está em uso por outro programa)")
            self.assertNotIn("OSError", server.error)


# -------------------------------------------------- outro computador assumiu

class SupersededServerTests(LanCase):
    """The old main cashier is turned on again after an additional cashier took over."""

    def setUp(self):
        super().setUp()
        self.server.stop()
        self.old_store = KeyedStore(generation=1)
        self.old_app, self.old = serve(self, self.old_store)
        self.new_store = KeyedStore(generation=2, shop_id=self.old_store.shop_id,
                                    terminal_id=self.old_store.terminal_id)
        self.new_app, self.new = serve(self, self.new_store)

    def test_old_server_finds_the_newer_generation_and_stops(self):
        self.old.discover = lambda shop_id=None: [announce(self.old, self.old_store),
                                                  announce(self.new, self.new_store)]
        with self.assertLogs("pdv2.lan", level="ERROR"):
            self.assertTrue(self.old.check_superseded())
        self.assertEqual(self.old_store.superseded_by, (2, self.new.fingerprint))
        self.assertIsNone(self.old._discovery_info(), "it no longer invites stations")
        self.assertTrue(lan.status(self.old_app)["superseded"])
        # Stations (old tokens) and new links are refused; the hello still says who this is.
        _station_id, token = self.old_store.add_station("Caixa 3")
        client = LanClient(HOST, self.old.port, self.old.fingerprint, token)
        self.addCleanup(client.close)
        for method, path in (("GET", "/lan/v1/ping"), ("GET", "/api/session"), ("POST", "/lan/v1/enroll")):
            response = client.request(method, path, headers=dict(BROWSER), body=b"{}" if method == "POST" else None)
            self.assertEqual((response.status, response.headers["x-pdv-lan-error"]), (409, "server_superseded"), path)
            response.close()
        self.assertEqual(client.request("GET", "/lan/v1/hello").status, 200)
        self.assertEqual(self.old_app.calls, [])
        # The new one never stops because of the old one.
        self.new.discover = lambda shop_id=None: [announce(self.old, self.old_store)]
        self.assertFalse(self.new.check_superseded())
        self.assertIsNone(self.new_store.superseded_by)

    def test_an_answer_anyone_could_forge_stops_nobody(self):
        # Same terminal and a bigger generation, but without the key of the shop copy.
        forged_store = KeyedStore(generation=9, key="x" * 43 + "=", shop_id=self.old_store.shop_id,
                                  terminal_id=self.old_store.terminal_id)
        _app, forged = serve(self, forged_store)
        keyless_store = KeyedStore(generation=9, key=None, shop_id=self.old_store.shop_id,
                                   terminal_id=self.old_store.terminal_id)
        _app, keyless = serve(self, keyless_store)
        other_terminal = KeyedStore(generation=9, shop_id=self.old_store.shop_id)
        _app, other = serve(self, other_terminal)
        lying = dict(announce(self.new, self.new_store), generation=1)  # says 1, proves 2: still newer
        self.old.discover = lambda shop_id=None: [
            announce(forged, forged_store), announce(keyless, keyless_store), announce(other, other_terminal),
            dict(announce(self.new, self.new_store), fingerprint="0" * 64), lying, "lixo", None]
        self.assertFalse(self.old.check_superseded())
        self.assertIsNone(self.old_store.superseded_by)
        # A server restored from the cloud carries the superseded mark from its own database.
        self.old_store.superseded_by = (2, "ab" * 32)
        self.old.stop()
        again = lan.start(self.old_app, host=HOST, port=0)
        self.addCleanup(again.stop)
        self.assertTrue(again.superseded)


class SupersededStationTests(StationCase):
    """The station that saw the new main cashier never goes back to the old one by itself."""

    def promoted(self, generation=2, key=KEY):
        store = KeyedStore(generation=generation, key=key, shop_id=self.store.shop_id,
                           terminal_id=self.store.terminal_id)
        _app, server = serve(self, store)
        return store, server

    def test_old_server_back_does_not_take_the_station_back(self):
        announced = []
        app, bridge = self.station_app(DISCOVER=lambda shop_id=None: announced)
        bridge._copy_key = lambda: KEY  # this caixa keeps the copy of the shop
        client = app.test_client()
        self.assertEqual(bridge.check_once(), "up")
        port = self.port
        self.server.stop()
        new_store, promoted = self.promoted()
        announced.append(announce(promoted, new_store))
        bridge._last_discover = 0.0
        self.assertEqual(bridge.check_once(), "relink")
        saved = station.load_state(bridge.test_dir)
        self.assertEqual((saved["superseded"]["generation"], saved["superseded"]["fingerprint"]),
                         (2, promoted.fingerprint))
        self.assertEqual(saved["server"]["fingerprint"], self.fingerprint, "the link itself is untouched")
        # The old computer is turned on again and answers the ping.
        self.restart_server(port)
        self.assertEqual(bridge.check_once(), "relink")
        status = client.get("/station/api/status", base_url=BASE).json
        self.assertEqual((status["state"], status["reason"], status["can_promote"]),
                         ("relink", "server_superseded", False))
        self.assertEqual(status["relink"]["fingerprint"], promoted.fingerprint)
        self.assertIn("mudou de computador", status["reason_text"])
        before = len(self.app.calls)
        sale = client.post("/api/sales", json={"request_id": "r-1"}, base_url=BASE, headers={"Origin": BASE})
        self.assertEqual((sale.status_code, sale.json["code"]), (409, "server_replaced"))
        page = client.get("/", base_url=BASE, headers=PAGE)
        self.assertEqual((page.status_code, page.headers["Location"]), (302, "/station/"))
        self.assertEqual(len(self.app.calls), before, "nothing was sold through the old computer")
        self.assertEqual(client.get("/health", base_url=BASE).json["server"], "down")
        # It survives a restart of this caixa.
        again = station.create_app({"TESTING": True, "DATA_DIR": str(bridge.test_dir),
                                    "DISCOVER": lambda shop_id=None: []})
        self.addCleanup(station.stop_background, again, 2.0)
        restarted = again.extensions["station"]
        self.assertEqual((restarted.status, restarted.relink["fingerprint"]), ("relink", promoted.fingerprint))
        self.assertEqual(restarted.check_once(), "relink")
        # The 6-digit code of the new main cashier ends it.
        new_store.code = "654987"
        linked = client.post("/station/api/link", json={
            "address": HOST, "port": promoted.port, "fingerprint": promoted.fingerprint, "code": "654987"},
            base_url=BASE, headers=LOCAL)
        self.assertEqual(linked.status_code, 200, linked.json)
        self.assertNotIn("superseded", station.load_state(bridge.test_dir))
        self.assertEqual(bridge.check_once(), "up")
        self.assertEqual(client.get("/api/echo", base_url=BASE).status_code, 200)

    def test_without_proof_nothing_is_remembered(self):
        for label, key, copy in (("forged", "x" * 43 + "=", KEY), ("no copy here", KEY, None)):
            with self.subTest(label):
                announced = []
                app, bridge = self.station_app(name="Caixa " + label, DISCOVER=lambda shop_id=None: announced)
                bridge._copy_key = lambda: copy
                self.assertEqual(bridge.check_once(), "up")
                port = self.port
                self.server.stop()
                new_store, promoted = self.promoted(key=key)
                announced.append(announce(promoted, new_store))
                bridge._last_discover = 0.0
                self.assertEqual(bridge.check_once(), "relink")
                self.assertNotIn("superseded", station.load_state(bridge.test_dir))
                promoted.stop()
                self.restart_server(port)
                self.assertEqual(bridge.check_once(), "up", "as before: the original server cancels the hint")

    def test_server_that_knows_it_was_replaced_sends_the_station_to_look_for_the_new_one(self):
        announced = []
        app, bridge = self.station_app(DISCOVER=lambda shop_id=None: announced)
        client = app.test_client()
        self.assertEqual(bridge.check_once(), "up")
        self.server.superseded = True
        self.assertEqual(bridge.check_once(), "relink")
        status = client.get("/station/api/status", base_url=BASE).json
        self.assertEqual((status["state"], status["reason"], status["relink"]), ("relink", "server_superseded", None))
        page = client.get("/", base_url=BASE, headers=PAGE)
        self.assertEqual((page.status_code, page.headers["Location"]), (302, "/station/"))
        answer = client.get("/api/session", base_url=BASE)
        self.assertEqual((answer.status_code, answer.json["code"]), (409, "server_superseded"))
        # The next round finds the new main cashier and points the screen to it.
        new_store, promoted = self.promoted()
        announced.append(announce(promoted, new_store))
        bridge._last_discover = 0.0
        self.assertEqual(bridge.check_once(), "relink")
        self.assertEqual(bridge.relink["fingerprint"], promoted.fingerprint)


class OldServerOnWhileTheNewOneIsOffTests(StationCase):
    """B took over (generation 2) and was turned off; the next day A, the old one, is turned on first.
    The caixas that know B tell A, so A stops before the sales of the shop split in two."""

    def setUp(self):
        super().setUp()
        self.server.stop()
        self.old_store = KeyedStore(generation=1, shop_id=self.store.shop_id, terminal_id=self.store.terminal_id)
        self.old_app, self.old = serve(self, self.old_store)
        self.new_store = KeyedStore(generation=2, shop_id=self.store.shop_id, terminal_id=self.store.terminal_id)
        self.new_app, self.new = serve(self, self.new_store)

    def linked_to_the_new_one(self, copy, name="Caixa 2"):
        """A caixa linked to B (it typed the code of the new main cashier), with B now off."""
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        station_id, token = self.new_store.add_station(name)
        state = {"v": 1, "station_id": station_id, "station_name": name, "token": token,
                 "server": {"address": HOST, "port": self.new.port, "host": "CAIXA-NOVO",
                            "fingerprint": self.new.fingerprint, "shop_id": self.store.shop_id,
                            "terminal_id": self.store.terminal_id, "terminal_name": "Caixa 1", "generation": 2},
                 "addresses": [HOST], "linked_at": 1}
        station.save_state(folder.name, state)
        announced = [announce(self.old, self.old_store)]
        app = station.create_app({"TESTING": True, "DATA_DIR": folder.name,
                                  "DISCOVER": lambda shop_id=None: announced})
        self.addCleanup(station.stop_background, app, 2.0)
        bridge = app.extensions["station"]
        bridge._copy_key = lambda: copy
        return app, bridge

    def test_a_caixa_with_the_copy_stops_the_old_one_with_proof(self):
        self.assertEqual(lan.status(self.old_app)["superseded"], False)
        app, bridge = self.linked_to_the_new_one(KEY)
        self.new.stop()
        with self.assertLogs("pdv2.lan", level="ERROR"):
            self.assertEqual(bridge.check_once(), "down", "the old one is never offered as the place to link")
        self.assertEqual(self.old_store.superseded_by, (2, self.new.fingerprint))
        self.assertTrue(self.old.superseded)
        self.assertIsNone(self.old._discovery_info(), "it stopped inviting caixas")
        self.assertIsNone(bridge.relink)
        self.assertEqual(station.load_state(bridge.data_dir)["server"]["fingerprint"], self.new.fingerprint, "the link to B is untouched")

    def test_a_caixa_without_the_copy_only_warns(self):
        app, bridge = self.linked_to_the_new_one(None)
        self.new.stop()
        with self.assertLogs("pdv2.lan", level="WARNING"):
            self.assertEqual(bridge.check_once(), "down")
        self.assertIsNone(self.old_store.superseded_by, "without proof nobody is stopped")
        status = lan.status(self.old_app)
        self.assertFalse(status["superseded"])
        self.assertEqual(status["newer_reported"]["generation"], 2)

    def test_a_wrong_proof_or_an_old_generation_stops_nobody(self):
        client = LanClient(HOST, self.old.port, self.old.fingerprint, None)
        self.addCleanup(client.close)
        challenge = os.urandom(16).hex()
        notice = {"terminal_id": self.store.terminal_id, "generation": 2, "fingerprint": self.new.fingerprint,
                  "challenge": challenge}
        for label, extra in (("wrong key", {"proof": lanclient.generation_proof("x" * 43 + "=", challenge,
                                                                                 self.store.terminal_id, 2,
                                                                                 self.new.fingerprint)}),
                             ("same generation", {"generation": 1}),
                             ("other terminal", {"terminal_id": "outro"}),
                             ("its own fingerprint", {"fingerprint": self.old.fingerprint})):
            with self.subTest(label):
                response = client.request("POST", "/lan/v1/superseded", body={**notice, **extra})
                response.close()
                self.assertIn(response.status, (200, 400))
                self.assertIsNone(self.old_store.superseded_by)
                self.assertFalse(self.old.superseded)

    def test_a_caixa_still_linked_to_the_old_one_tells_it_when_it_answers(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        station_id, token = self.old_store.add_station("Caixa 3")
        station.save_state(folder.name, {
            "v": 1, "station_id": station_id, "station_name": "Caixa 3", "token": token,
            "server": {"address": HOST, "port": self.old.port, "host": "CAIXA-ANTIGO",
                       "fingerprint": self.old.fingerprint, "shop_id": self.store.shop_id,
                       "terminal_id": self.store.terminal_id, "generation": 1},
            "addresses": [HOST], "linked_at": 1,
            # It saw B (and B proved it) before B was turned off.
            "superseded": {"host": "CAIXA-NOVO", "address": HOST, "port": self.new.port,
                           "fingerprint": self.new.fingerprint, "generation": 2}})
        app = station.create_app({"TESTING": True, "DATA_DIR": folder.name, "DISCOVER": lambda shop_id=None: []})
        self.addCleanup(station.stop_background, app, 2.0)
        bridge = app.extensions["station"]
        bridge._copy_key = lambda: KEY
        self.new.stop()
        with self.assertLogs("pdv2.lan", level="ERROR"):
            self.assertEqual(bridge.check_once(), "relink")
        self.assertEqual(self.old_store.superseded_by, (2, self.new.fingerprint))
        # From now on the old one refuses everybody.
        self.assertEqual(bridge.check_once(), "relink")
        self.assertEqual(bridge.reason, "server_superseded")


# ------------------------------------------------------------- a ponte local

class BridgeBoundaryTests(StationCase):
    def test_browser_never_reaches_the_native_routes_through_the_bridge(self):
        app, bridge = self.station_app()
        client = app.test_client()
        self.assertEqual(bridge.check_once(), "up")
        job = self.store.queue(bridge.test_id, b"recibo")
        seen = len(self.store.auth_tokens)
        for method, path in (("GET", "/lan/v1/replica/key"), ("GET", "/lan/v1/replica/status"),
                             ("GET", "/lan/v1/replica/journal"), ("GET", "/lan/v1/ping"), ("GET", "/lan"),
                             ("POST", "/lan/v1/print/next"), ("POST", f"/lan/v1/print/{job}/done"),
                             ("GET", "/LAN/v1/replica/key"), ("GET", "/%6can/v1/replica/key"),
                             ("GET", "/lan//v1/replica/key")):
            for headers in ({}, {"Origin": "http://evil.example", "Content-Type": "text/plain"},
                            {"X-PDV-Station-Native": "1", "X-PDV-Station-Local": "1", "Origin": BASE}):
                response = client.open(path, method=method, base_url=BASE, headers=headers)
                self.assertIn(response.status_code, (404, 308), (path, headers))
                self.assertNotIn(b"key", response.data.lower().replace(b"key_id", b""), path)
        self.assertEqual(len(self.store.auth_tokens), seen, "the token never left this computer for those")
        self.assertEqual((self.store.sending, self.store.finished), ({}, []), "the receipt is still in the queue")
        # The agent of this caixa still prints it.
        bridge.send_bytes = lambda *args: None
        self.assertEqual(bridge.print_once(), 1)
        self.assertEqual(self.store.finished, [(bridge.test_id, job, None)])

    def test_a_forged_native_header_is_dropped_by_the_bridge(self):
        # Even if the local refusal were removed, the server would not answer: the bridge drops the header.
        app, bridge = self.station_app()
        response = app.test_client().get("/api/echo", base_url=BASE, headers={"X-PDV-Station-Native": "1"})
        self.assertNotIn("HTTP_X_PDV_STATION_NATIVE", response.json["headers"])

    def test_station_outside_the_plan_gets_a_screen_and_comes_back_alone(self):
        self.store.add_station("Caixa 2")
        app, bridge = self.station_app("Caixa 3", PROMOTE_AFTER_SECONDS=0, LOCAL_ADDRESSES=lambda: ["192.168.0.20"])
        client = app.test_client()
        self.assertEqual(bridge.check_once(), "up")
        self.store.limit = 1  # the plan shrank: the newest caixa waits
        page = client.get("/", base_url=BASE, headers=PAGE)
        self.assertEqual((page.status_code, page.headers["Location"]), (302, "/station/"),
                         "never the raw JSON of the refusal on the screen")
        with patch.object(station, "promotion", FakePromotion()):
            self.assertEqual(bridge.check_once(), "blocked")
            status = client.get("/station/api/status", base_url=BASE).json
        self.assertEqual((status["state"], status["reason"], status["can_promote"], status["diagnosis"]),
                         ("blocked", "station_limit", False, []))
        self.assertIn("O plano da loja não inclui mais este caixa adicional", status["reason_text"])
        self.assertIsNone(status["server"]["down_since"])
        self.assertEqual(client.get("/health", base_url=BASE).json["server"], "down")
        answer = client.get("/api/session", base_url=BASE)
        self.assertEqual((answer.status_code, answer.json["code"]), (403, "station_limit"))
        with patch.object(station, "promotion", FakePromotion()):
            refused = client.post("/station/api/promotion/start", json={"manager_pin": "654321", "confirm": True},
                                  base_url=BASE, headers=LOCAL)
        self.assertEqual(refused.status_code, 409)
        # The plan grows again: no new code, no restart.
        self.store.limit = 2
        self.assertEqual(bridge.check_once(), "up")
        self.assertEqual(client.get("/station/api/status", base_url=BASE).json["reason_text"], "")
        self.assertEqual(client.get("/", base_url=BASE, headers=PAGE).status_code, 200)

    def test_passing_refusal_of_the_network_layer_is_a_page_that_tries_again(self):
        app, bridge = self.station_app()
        client = app.test_client()
        self.assertEqual(bridge.check_once(), "up")
        with self.server._limits:
            self.server._blocked[HOST] = time.monotonic() + 60
        page = client.get("/", base_url=BASE, headers=PAGE)
        self.assertEqual((page.status_code, page.mimetype), (503, "text/html"))
        text = page.get_data(as_text=True)
        self.assertIn('http-equiv="refresh"', text)
        self.assertIn("Tentando de novo", text)
        self.assertNotIn("too_many_attempts", text)
        self.assertEqual(bridge.status, "up", "a passing refusal is not a state of this caixa")
        answer = client.get("/api/session", base_url=BASE)
        self.assertEqual((answer.status_code, answer.json["code"]), (429, "too_many_attempts"))

    def test_unlink_is_the_way_out_of_a_dead_server(self):
        app, bridge = self.station_app()
        client = app.test_client()
        self.server.stop()
        self.assertEqual(bridge.check_once(), "down")
        stuck = client.post("/station/api/mode/server", json={"confirm": True}, base_url=BASE, headers=LOCAL)
        self.assertEqual(stuck.status_code, 409)
        self.assertEqual(client.post("/station/api/unlink", json={"confirm": True}, base_url=BASE,
                                     headers=LOCAL).json, {"linked": False})
        with patch.object(station.runtime, "request_restart", return_value=True):
            free = client.post("/station/api/mode/server", json={"confirm": True}, base_url=BASE, headers=LOCAL)
        self.assertEqual((free.status_code, free.json["mode"]), (200, "server"))
        # The screen offers it wherever a linked caixa cannot be used.
        html = client.get("/station/", base_url=BASE).get_data(as_text=True)
        for view in ("unlinkDownBtn", "unlinkLinkBtn", "unlinkBlockedBtn"):
            self.assertIn(f'id="{view}"', html)
        script = (Path(station.__file__).resolve().parent / "static" / "station.js").read_text(encoding="utf-8")
        self.assertIn("$('unlinkLinkBtn').classList.toggle('hidden', status.state === 'unlinked')", script)


class PromotionGuardTests(StationCase):
    def setUp(self):
        super().setUp()
        self.promotion = FakePromotion()
        for target, name, value in ((station, "promotion", self.promotion),):
            patcher = patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        restart = patch.object(station.runtime, "request_restart", return_value=True)
        restart.start()
        self.addCleanup(restart.stop)

    def test_caixa_without_network_cannot_take_over(self):
        addresses = []
        app, bridge = self.station_app(PROMOTE_AFTER_SECONDS=0, LOCAL_ADDRESSES=lambda: addresses)
        client = app.test_client()
        self.assertEqual(bridge.check_once(), "up")
        self.server.stop()  # seen from here it is the same as a loose cable on this computer
        self.assertEqual(bridge.check_once(), "down")
        status = client.get("/station/api/status", base_url=BASE).json
        self.assertEqual((status["can_promote"], status["promote_block"]), (False, "no_network"))
        refused = client.post("/station/api/promotion/start", json={"manager_pin": "654321", "confirm": True},
                              base_url=BASE, headers=LOCAL)
        self.assertEqual((refused.status_code, refused.json["code"]), (409, "no_network"))
        self.assertIn("sem rede", refused.json["error"])
        self.assertEqual(self.promotion.calls, [])
        self.assertFalse(client.get("/station/api/promotion", base_url=BASE).json["can_promote"])
        # With the network back (and the server really out) the offer is there.
        addresses.append("192.168.0.20")
        status = client.get("/station/api/status", base_url=BASE).json
        self.assertEqual((status["can_promote"], status["promote_block"]), (True, None))

    def test_an_address_outside_the_network_of_the_server_is_no_network(self):
        # Cable out on a Windows with WSL or a VPN: the computer still has an address, but not on the
        # network of the main cashier. Taking over from there would make a second main cashier.
        reachable = []
        app, bridge = self.station_app(PROMOTE_AFTER_SECONDS=0, LOCAL_ADDRESSES=lambda: ["172.24.80.1"],
                                       SAME_NETWORK=lambda address: bool(reachable) and address == HOST)
        client = app.test_client()
        self.assertEqual(bridge.check_once(), "up")
        self.server.stop()
        self.assertEqual(bridge.check_once(), "down")
        status = client.get("/station/api/status", base_url=BASE).json
        self.assertEqual((status["can_promote"], status["promote_block"]), (False, "no_network"))
        refused = client.post("/station/api/promotion/start", json={"manager_pin": "654321", "confirm": True},
                              base_url=BASE, headers=LOCAL)
        self.assertEqual((refused.status_code, refused.json["code"]), (409, "no_network"))
        self.assertIn("fora da rede do caixa principal", refused.json["error"])
        self.assertEqual(self.promotion.calls, [])
        reachable.append(True)
        status = client.get("/station/api/status", base_url=BASE).json
        self.assertEqual((status["can_promote"], status["promote_block"]), (True, None))

    def test_clock_behind_the_server_is_told_before_the_server_breaks(self):
        app, bridge = self.station_app(LOCAL_ADDRESSES=lambda: ["192.168.0.20"])
        self.assertEqual(bridge.check_once(), "up")
        self.assertEqual(bridge.clock_behind, 0, "the ping of the server brings its clock; here they agree")
        bridge._note_clock(int(time.time()) + 200)
        self.assertEqual(bridge.clock_behind, 0, "inside the tolerance of the license")
        bridge._note_clock(int(time.time()) + 420)
        self.assertGreater(bridge.clock_behind, 300)
        self.server.stop()
        self.assertEqual(bridge.check_once(), "down")
        items = {item["id"]: item for item in bridge.diagnosis()}
        self.assertFalse(items["clock_behind"]["ok"])
        self.assertIn("Acerte a data e a hora", items["clock_behind"]["help"])


# ----------------------------------------------------------------- o cliente

class PinnedClientTests(LanCase):
    def test_closed_connection_never_becomes_a_plain_socket(self):
        _station_id, token = self.store.add_station("Caixa 2")
        client = self.client(token)
        first = client.request("GET", "/lan/v1/ping", headers=dict(BROWSER))
        first.read()
        first.close()
        connection = client._local.connection
        self.assertEqual(connection.auto_open, 0)
        client.close()  # what the copy puller used to do to the client of the bridge
        self.assertIsNone(connection.sock)
        opened = []
        with patch.object(socket, "create_connection", side_effect=lambda *a, **k: opened.append(a)):
            with self.assertRaises(LanError) as caught:
                client._send(connection, "POST", "/api/sales", b"{}", [("X-PDV-Station-Token", token)], 5)
        self.assertEqual((caught.exception.code, caught.exception.sent), ("unreachable", False))
        self.assertEqual(opened, [], "no socket without TLS was opened for the request")
        # The next request simply connects again, pinned.
        response = client.request("GET", "/lan/v1/ping", headers=dict(BROWSER))
        self.assertEqual(response.status, 200)
        response.close()


if __name__ == "__main__":
    unittest.main()
