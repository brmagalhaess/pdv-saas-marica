"""Caixa adicional: link, bridge, server out of reach, rediscovery, printing and promotion.

Real TLS listeners (terminal.lan) on ephemeral loopback ports, with the fake store and
the minimal WSGI app of test_lan. Nothing here needs the core of the terminal.
"""

import json
import logging
import os
import socket
import ssl
import stat
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from terminal import lan, paths, station
from terminal.lanclient import LanClient, LanError

try:
    from terminal.tests.test_lan import HOST, FakeApp, FakeError, FakeStore, LanCase, wait_for
except ImportError:  # discovered as a top-level module
    from test_lan import HOST, FakeApp, FakeError, FakeStore, LanCase, wait_for

BASE = "http://127.0.0.1:8010"
ORIGIN = {"Origin": BASE}
LOCAL = {"Origin": BASE, "X-PDV-Station-Local": "1"}
STATIC = Path(station.__file__).resolve().parent / "static"


def state_for(store, server, station_id, token, name="Caixa 2", address=HOST):
    return {"v": 1, "station_id": station_id, "station_name": name, "token": token,
            "server": {"address": address, "port": server.port, "host": "CAIXA-PRINCIPAL",
                       "fingerprint": server.fingerprint, "shop_id": store.shop_id, "shop_name": "Mercado X",
                       "tenant_name": "Empresa X", "terminal_id": store.terminal_id, "terminal_name": "Caixa 1",
                       "generation": 1},
            "addresses": [address], "linked_at": 1763300000000}


class SilentServer:
    """Presents the certificate of the shop, reads everything and never answers."""

    def __init__(self, data_dir, port=0):
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(str(Path(data_dir) / "lan" / "cert.pem"), str(Path(data_dir) / "lan" / "key.pem"))
        self.listener = socket.create_server((HOST, port), reuse_port=False)
        self.listener.settimeout(0.1)
        self.port = self.listener.getsockname()[1]
        self.received = []
        self._stop = threading.Event()
        self._threads = [threading.Thread(target=self._accept, args=(context,), daemon=True)]
        self._threads[0].start()

    def _accept(self, context):
        while not self._stop.is_set():
            try:
                raw, _address = self.listener.accept()
            except OSError:
                continue
            thread = threading.Thread(target=self._read, args=(context, raw), daemon=True)
            thread.start()
            self._threads.append(thread)

    def _read(self, context, raw):
        try:
            raw.settimeout(0.2)
            tls = context.wrap_socket(raw, server_side=True)
            while not self._stop.is_set():
                try:
                    data = tls.recv(65536)
                except socket.timeout:
                    continue
                if not data:
                    break
                self.received.append(data)
        except OSError:
            pass
        finally:
            raw.close()

    def data(self):
        return b"".join(self.received)

    def close(self):
        self._stop.set()
        for thread in self._threads:
            thread.join(timeout=3)
        self.listener.close()


class FakePromotion:
    def __init__(self):
        self.state = "idle"
        self.calls = []
        self.fail = None
        self.release = threading.Event()
        self.release.set()

    def status(self, _data_dir):
        return {"available": True, "reason": "", "state": self.state, "step": "", "error": "",
                "shop_name": "Mercado X", "terminal_name": "Caixa 1", "last_snapshot_at": 1763300000000,
                "journal_seq": 12, "last_contact_at": 1763300030000, "locked_until": None}

    def verify_manager(self, _data_dir, pin):
        if pin != "654321":
            raise FakeError("PIN de gerente inválido", 403)
        return {"operator_id": "m-1", "name": "Bruno", "role": "manager"}

    def promote(self, data_dir, pin, *, station_id, station_name):
        self.state = "running"
        self.release.wait(5)
        self.calls.append((Path(data_dir), pin, station_id, station_name))
        if self.fail:
            self.state = "failed"
            raise self.fail
        self.state = "done"
        return {"promoted": True, "applied": 3}

    def pending(self, _data_dir):
        return self.state == "done"


class StationCase(LanCase):
    def station_app(self, name="Caixa 2", link=True, **config):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        token = station_id = None
        if link:
            station_id, token = self.store.add_station(name)
            station.save_state(temp.name, state_for(self.store, self.server, station_id, token, name))
        options = {"TESTING": True, "DATA_DIR": temp.name, "DISCOVER": lambda shop_id=None: [], **config}
        app = station.create_app(options)
        self.addCleanup(station.stop_background, app, 2.0)
        bridge = app.extensions["station"]
        bridge.test_dir, bridge.test_token, bridge.test_id = Path(temp.name), token, station_id
        return app, bridge

    def restart_server(self, port=None, app=None):
        self.server = lan.start(app or self.app, host=HOST, port=self.port if port is None else port)
        self.assertTrue(self.server.running, self.server.error)
        return self.server

    def announce(self, server, store=None):
        store = store or self.store
        return {"address": HOST, "port": server.port, "fingerprint": server.fingerprint, "shop_id": store.shop_id,
                "shop_name": "Mercado X", "terminal_name": "Caixa 1", "host": "CAIXA-NOVO", "api": 1,
                "version": "2.1.0", "generation": 1}


class LocalRoutesTests(StationCase):
    def test_health_is_always_local(self):
        app, bridge = self.station_app(link=False)
        client = app.test_client()
        self.assertEqual(client.get("/health", base_url=BASE).json,
                         {"status": "ok", "service": "terminal-local", "mode": "station", "server": "unlinked"})
        app, bridge = self.station_app()
        client = app.test_client()
        self.assertEqual(client.get("/health", base_url=BASE).json["server"], "down", "not checked yet")
        self.assertEqual(bridge.check_once(), "up")
        self.assertEqual(client.get("/health", base_url=BASE).json["server"], "up")
        self.server.stop()
        bridge.check_once()
        health = client.get("/health", base_url=BASE)
        self.assertEqual((health.status_code, health.json["status"], health.json["server"]), (200, "ok", "down"))
        self.assertNotIn("/health", self.app.paths())

    def test_unlinked_everything_goes_to_the_station_page(self):
        app, bridge = self.station_app(link=False)
        client = app.test_client()
        for method, path in (("get", "/"), ("get", "/api/session"), ("post", "/api/login"), ("get", "/static/x.js")):
            response = getattr(client, method)(path, base_url=BASE)
            self.assertEqual((response.status_code, response.headers["Location"]), (302, "/station/"), path)
        page = client.get("/station/", base_url=BASE)
        self.assertEqual(page.status_code, 200)
        text = page.get_data(as_text=True)
        for needle in ("Este é um caixa adicional", "Procurando o caixa principal na rede da loja…",
                       "Código de 6 números mostrado no caixa principal (Sistema → Caixas da loja → Adicionar caixa)",
                       "Como este caixa vai se chamar? (ex.: Caixa 2)", "Conectar este caixa",
                       "Endereço do caixa principal (aparece na tela dele, ex.: 192.168.0.10)", "Procurar de novo",
                       "Este computador é o caixa principal? Voltar", "O caixa principal está fora do ar",
                       "Este computador não vai mais ser caixa adicional desta loja",
                       "Não é este computador? Digitar o endereço", "Este caixa não está liberado para vender",
                       "Este caixa não vende enquanto o principal não voltar. Ele tenta de novo sozinho a cada 2 "
                       "segundos.", "Assumir como caixa principal",
                       "Só faça isso se o computador principal não vai voltar agora (quebrou, queimou, foi "
                       "roubado). Depois de assumir, o computador antigo NÃO pode voltar a vender."):
            self.assertIn(needle, text)
        self.assertEqual(page.headers["Cache-Control"], "no-store")
        self.assertNotIn("Set-Cookie", page.headers)
        bare = client.get("/station", base_url=BASE)
        self.assertIn(bare.status_code, (302, 308))
        self.assertTrue(bare.headers["Location"].endswith("/station/"))
        self.assertEqual(self.app.calls, [])
        status = client.get("/station/api/status", base_url=BASE).json
        self.assertEqual((status["mode"], status["linked"], status["state"], status["station"], status["server"]),
                         ("station", False, "unlinked", None, None))
        self.assertEqual((status["can_promote"], status["diagnosis"]), (False, []))
        self.assertEqual(status["version"], paths.source_version())

    def test_static_files_of_the_page_are_local_and_listed(self):
        app, _bridge = self.station_app()
        client = app.test_client()
        for name in ("station.js", "station.css", "theme.js", "favicon.svg"):
            response = client.get("/station/static/" + name, base_url=BASE)
            self.assertEqual(response.status_code, 200, name)
            response.close()
        self.assertEqual(client.get("/station/static/terminal.js", base_url=BASE).status_code, 404)
        self.assertEqual(client.get("/station/static/..%2Fstation.py", base_url=BASE).status_code, 404)
        self.assertEqual(client.get("/station/qualquer", base_url=BASE).status_code, 404)
        self.assertEqual(client.get("/station/api/nada", base_url=BASE).status_code, 404)
        self.assertEqual(self.app.calls, [], "the reserved prefix is never forwarded")

    def test_screen_texts(self):
        script = (STATIC / "station.js").read_text(encoding="utf-8")
        for needle in ("Caixa principal do ' + shop + ' encontrado (", "Não encontrei o caixa principal sozinho.",
                       "O caixa principal agora é outro computador (",
                       "Peça ao gerente o código de 6 números em Sistema → Caixas da loja → Adicionar caixa, no novo "
                       "caixa principal, e digite aqui.",
                       "api('unlink', { confirm: true })", "status.state === 'blocked'",
                       "Sem rede não dá para saber se o caixa principal parou",
                       "Encontrei mais de um caixa principal nesta rede. Toque no desta loja:",
                       "Confira se o código foi gerado no computador ",
                       "Este caixa foi removido do caixa principal. Para voltar a usar, peça um novo código ao "
                       "gerente.", "Atualize este caixa: instale a versão nova do PDV2.",
                       "Atualize o caixa principal", "Cópia deste caixa: de ", "Abrindo a cópia…",
                       "Aplicando as últimas vendas…", "Preparando este computador…", "location.replace('/')",
                       "'X-PDV-Station-Local': '1'"):
            self.assertIn(needle, script)
        self.assertNotIn("innerHTML", script)
        self.assertIn("status.state === 'damaged'", script)
        self.assertNotIn("if (error.code === 'server_alive') { closeWizard(); return; }", script,
                         "the wizard never closes without saying why")
        self.assertIn("setText('downNote', error.message)", script)
        page = (STATIC.parent / "templates" / "station.html").read_text(encoding="utf-8")
        self.assertIn('id="viewDamaged"', page)
        self.assertIn("O caixa principal está com o banco de dados danificado. Chame o suporte.", page)

    def test_foreign_host_is_refused(self):
        app, _bridge = self.station_app()
        client = app.test_client()
        for path in ("/station/api/status", "/api/session", "/station/"):
            self.assertEqual(client.get(path, base_url="http://evil.example:8010").status_code, 403, path)
        self.assertEqual(self.app.calls, [])

    def test_local_post_needs_the_header_and_a_local_origin(self):
        app, bridge = self.station_app()
        client = app.test_client()
        body = {"confirm": True}
        refused = [client.post("/station/api/unlink", json=body, base_url=BASE),
                   client.post("/station/api/unlink", json=body, base_url=BASE, headers=ORIGIN),
                   client.post("/station/api/unlink", json=body, base_url=BASE,
                               headers={"Origin": "http://evil.example", "X-PDV-Station-Local": "1"}),
                   client.post("/station/api/unlink", json=body, base_url=BASE,
                               headers={"Origin": "null", "X-PDV-Station-Local": "1"}),
                   client.post("/station/api/discover", json={}, base_url=BASE, headers={"Origin": "http://localhost"})]
        self.assertEqual([response.status_code for response in refused], [403] * 5)
        self.assertTrue(bridge.linked)
        self.assertEqual(client.post("/station/api/unlink", json={}, base_url=BASE, headers=LOCAL).status_code, 400)
        response = client.post("/station/api/unlink", json=body, base_url=BASE, headers=LOCAL)
        self.assertEqual((response.status_code, response.json), (200, {"linked": False}))

    def test_unlink_keeps_the_previous_link(self):
        app, bridge = self.station_app()
        client = app.test_client()
        client.post("/station/api/unlink", json={"confirm": True}, base_url=BASE, headers=LOCAL)
        self.assertFalse((bridge.test_dir / "station.json").exists())
        previous = json.loads((bridge.test_dir / "station-anterior.json").read_text())
        self.assertEqual((previous["station_id"], previous["token"]), (bridge.test_id, bridge.test_token))
        self.assertIsNone(station.load_state(bridge.test_dir))
        self.assertEqual(client.get("/station/api/status", base_url=BASE).json["state"], "unlinked")
        self.assertEqual(client.get("/api/session", base_url=BASE).status_code, 302)

    def test_mode_server_only_without_a_link(self):
        app, bridge = self.station_app()
        client = app.test_client()
        with patch.object(station.runtime, "request_restart", return_value=True) as restart:
            response = client.post("/station/api/mode/server", json={"confirm": True}, base_url=BASE, headers=LOCAL)
            self.assertEqual(response.status_code, 409)
            self.assertFalse((bridge.test_dir / "mode.json").exists())
            client.post("/station/api/unlink", json={"confirm": True}, base_url=BASE, headers=LOCAL)
            self.assertEqual(client.post("/station/api/mode/server", json={}, base_url=BASE,
                                         headers=LOCAL).status_code, 400)
            restart.assert_not_called()
            response = client.post("/station/api/mode/server", json={"confirm": True}, base_url=BASE, headers=LOCAL)
            self.assertEqual((response.status_code, response.json), (200, {"mode": "server", "restart": True}))
            restart.assert_called_once_with("modo-servidor")
        self.assertEqual(paths.read_mode(bridge.test_dir, {}), "server")

    def test_state_file_is_private_and_validated(self):
        _app, bridge = self.station_app()
        path = bridge.test_dir / "station.json"
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        loaded = station.load_state(bridge.test_dir)
        self.assertEqual((loaded["v"], loaded["token"], loaded["server"]["port"]), (1, bridge.test_token, self.port))
        self.assertEqual(sorted(item.name for item in bridge.test_dir.iterdir()), ["station.json"])
        for broken in ("{", "[]", json.dumps({**loaded, "token": ""}),
                       json.dumps({**loaded, "server": {**loaded["server"], "fingerprint": "curto"}}),
                       json.dumps({**loaded, "server": {**loaded["server"], "port": "8011"}})):
            path.write_text(broken)
            self.assertIsNone(station.load_state(bridge.test_dir), broken[:30])
        with self.assertRaises(ValueError):
            station.save_state(bridge.test_dir, {"v": 1})
        self.assertFalse((bridge.test_dir / "terminal.sqlite3").exists())


class LinkTests(StationCase):
    def link(self, client, code="483920", **changes):
        body = {"address": HOST, "port": self.port, "fingerprint": self.fingerprint, "code": code,
                "name": "Caixa 2", **changes}
        return client.post("/station/api/link", json=body, base_url=BASE, headers=LOCAL)

    def test_probe_describes_the_server_without_a_token(self):
        app, _bridge = self.station_app(link=False)
        client = app.test_client()
        response = client.post("/station/api/probe", json={"address": HOST, "port": self.port}, base_url=BASE,
                               headers=LOCAL)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json, {
            "address": HOST, "port": self.port, "host": socket.gethostname(), "shop_id": self.store.shop_id,
            "shop_name": "Mercado X", "terminal_name": "Caixa 1", "fingerprint": self.fingerprint, "api": 1,
            "version": paths.source_version(), "generation": 1, "same_shop": False, "known": False})
        self.assertEqual(self.store.auth_tokens, [])
        with socket.socket() as probe:
            probe.bind((HOST, 0))
            free = probe.getsockname()[1]
        for body in ({"address": HOST, "port": free}, {"address": "não é endereço"}, {"address": HOST, "port": "x"},
                     {}):
            response = client.post("/station/api/probe", json=body, base_url=BASE, headers=LOCAL)
            self.assertEqual(response.status_code, 404, body)
            self.assertEqual(response.json["error"], "Não encontrei o caixa principal neste endereço. Confira o "
                                                     "número e se o computador principal está ligado.")

    def test_discover_lists_servers(self):
        found = [self.announce(self.server)]
        app, _bridge = self.station_app(link=False, DISCOVER=lambda shop_id=None: found)
        response = app.test_client().post("/station/api/discover", json={}, base_url=BASE, headers=LOCAL)
        self.assertEqual(response.json, {"servers": [{
            "address": HOST, "port": self.port, "host": "CAIXA-NOVO", "shop_id": self.store.shop_id,
            "shop_name": "Mercado X", "terminal_name": "Caixa 1", "fingerprint": self.fingerprint, "api": 1,
            "version": "2.1.0", "generation": 1, "same_shop": False, "known": False}]})
        app, _bridge = self.station_app(DISCOVER=lambda shop_id=None: found)
        item = app.test_client().post("/station/api/discover", json={}, base_url=BASE, headers=LOCAL).json["servers"][0]
        self.assertEqual((item["same_shop"], item["known"]), (True, True))
        app, _bridge = self.station_app(link=False, DISCOVER=MagicMock(side_effect=OSError("sem rede")))
        with self.assertLogs("pdv2.station", level="WARNING"):
            self.assertEqual(app.test_client().post("/station/api/discover", json={}, base_url=BASE,
                                                    headers=LOCAL).json, {"servers": []})

    def test_right_code_links_and_saves_the_state(self):
        app, bridge = self.station_app(link=False)
        client = app.test_client()
        self.store.code = "483920"
        with self.assertLogs("pdv2", level="DEBUG") as logs:
            logging.getLogger("pdv2.station").debug("início")
            response = self.link(client, code="483 920")
        self.assertEqual(response.status_code, 200, response.json)
        saved = station.load_state(bridge.test_dir)
        self.assertEqual(response.json["linked"], True)
        self.assertEqual(response.json["station"], {"id": saved["station_id"], "name": "Caixa 2"})
        self.assertNotIn("token", json.dumps(response.json))
        self.assertEqual(saved["token"], self.store.rows[saved["station_id"]]["token"])
        self.assertEqual(saved["server"], {
            "address": HOST, "port": self.port, "host": socket.gethostname(), "fingerprint": self.fingerprint,
            "shop_id": self.store.shop_id, "shop_name": "Mercado X", "tenant_name": "Empresa X",
            "terminal_id": self.store.terminal_id, "terminal_name": "Caixa 1", "generation": 1})
        self.assertEqual((saved["v"], saved["addresses"]), (1, [HOST]))
        self.assertLess(abs(saved["linked_at"] - time.time() * 1000), 5000)
        self.assertEqual(self.store.enrolls[0]["code"], "483920")
        self.assertEqual((self.store.enrolls[0]["hostname"], self.store.enrolls[0]["station_id"]),
                         (socket.gethostname()[:64], None))
        self.assertNotIn(saved["token"], "\n".join(logs.output))
        status = client.get("/station/api/status", base_url=BASE).json
        self.assertEqual((status["linked"], status["state"], status["station"]["name"]), (True, "up", "Caixa 2"))
        self.assertNotIn(saved["token"], json.dumps(status))
        echo = client.get("/api/echo", base_url=BASE).json
        self.assertEqual(echo["station"], {"id": saved["station_id"], "name": "Caixa 2",
                                           "version": station.paths.source_version()})

    def test_server_refusals_are_passed_on(self):
        app, bridge = self.station_app(link=False)
        client = app.test_client()
        self.store.code = "483920"
        response = self.link(client, code="111111")
        self.assertEqual((response.status_code, response.json["code"]), (401, "invalid_code"))
        self.assertIn("Código inválido ou vencido", response.json["error"])
        self.assertEqual(self.link(client, code="12").status_code, 400)
        self.assertEqual(self.link(client, fingerprint="xyz").status_code, 400)
        self.assertEqual(len(self.store.enrolls), 1, "an incomplete code never leaves this computer")
        self.store.add_station("A")
        self.store.add_station("B")
        response = self.link(client)
        self.assertEqual((response.status_code, response.json["code"]), (409, "station_limit"))
        for _attempt in range(8):
            self.link(client, code="000000")
        response = self.link(client)
        self.assertEqual((response.status_code, response.json["code"]), (429, "too_many_attempts"))
        self.assertIsNone(station.load_state(bridge.test_dir))
        self.assertFalse(bridge.linked)

    def test_wrong_identity_and_unreachable_server(self):
        app, bridge = self.station_app(link=False)
        client = app.test_client()
        self.store.code = "483920"
        response = self.link(client, fingerprint="0" * 64)
        self.assertEqual((response.status_code, response.json), (409, {
            "error": "O caixa principal respondeu com outra identidade. Procure de novo.",
            "code": "fingerprint_mismatch"}))
        self.assertEqual(self.store.enrolls, [], "the code is never sent to another identity")
        self.server.stop()
        response = self.link(client)
        self.assertEqual((response.status_code, response.json), (502, {
            "error": "Não consegui falar com o caixa principal. Confira a rede e tente de novo.",
            "code": "server_unreachable"}))
        self.assertFalse(bridge.linked)

    def test_relink_sends_the_previous_station_id_for_the_same_shop(self):
        app, bridge = self.station_app()
        client = app.test_client()
        previous = bridge.test_id
        client.post("/station/api/unlink", json={"confirm": True}, base_url=BASE, headers=LOCAL)
        self.store.code = "483920"
        response = self.link(client)
        self.assertEqual((response.status_code, response.json["station"]["id"]), (200, previous))
        self.assertEqual(self.store.enrolls[-1]["station_id"], previous)
        self.assertNotEqual(station.load_state(bridge.test_dir)["token"], bridge.test_token)
        # Another shop: the old identity is not offered.
        other_store = FakeStore()
        other_temp = tempfile.TemporaryDirectory()
        self.addCleanup(other_temp.cleanup)
        other = lan.start(FakeApp(other_store, other_temp.name), host=HOST, port=0)
        self.addCleanup(other.stop)
        other_store.code = "111222"
        response = client.post("/station/api/link", json={
            "address": HOST, "port": other.port, "fingerprint": other.fingerprint, "code": "111222"},
            base_url=BASE, headers=LOCAL)
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(other_store.enrolls[0]["station_id"])


class BridgeTests(StationCase):
    def test_request_reaches_the_server_untouched(self):
        app, bridge = self.station_app()
        client = app.test_client()
        client.set_cookie("pdv2_session", "sessao-1", domain="127.0.0.1")
        response = client.post("/api/echo?a=1&b=%C3%A7", data=b'{"x":1}', base_url=BASE, headers={
            **ORIGIN, "Content-Type": "application/json", "X-CSRF-Token": "csrf-1",
            "X-PDV-Station-Token": "forjado-" + "f" * 40, "X-PDV-Station-Id": "server",
            "X-Forwarded-For": "10.0.0.1", "Forwarded": "for=10.0.0.1", "X-PDV-Lan-Error": "x",
            "Connection": "keep-alive, X-Segredo", "X-Segredo": "hop", "X-Outro": "fica"})
        self.assertEqual(response.status_code, 200)
        data = response.json
        self.assertEqual(data["station"], {"id": bridge.test_id, "name": "Caixa 2",
                                           "version": station.paths.source_version()},
                         "the token of station.json wins over anything the browser sends")
        self.assertEqual((data["host"], data["origin"]), ("127.0.0.1:8010", BASE))
        self.assertEqual((data["cookie"], data["csrf"]), ("pdv2_session=sessao-1", "csrf-1"))
        self.assertEqual((data["method"], data["path"], data["query"], data["length"]),
                         ("POST", "/api/echo", "a=1&b=%C3%A7", 7))
        self.assertNotIn("HTTP_X_SEGREDO", data["headers"])
        self.assertIn("HTTP_X_OUTRO", data["headers"])
        environ = self.app.calls[0]["environ"]
        self.assertEqual(environ["CONTENT_TYPE"], "application/json")
        self.assertFalse(any("STATION" in key or "FORWARDED" in key for key in environ if key.startswith("HTTP_")))
        self.assertEqual(self.store.touched[0][0], bridge.test_id)
        self.assertEqual(self.store.touched[0][2], paths.source_version())

    def test_set_cookie_is_literal_and_lan_headers_are_removed(self):
        app, _bridge = self.station_app()
        client = app.test_client()
        response = client.post("/api/login", json={}, base_url=BASE, headers=ORIGIN)
        cookies = response.headers.getlist("Set-Cookie")
        self.assertEqual(len(cookies), 2)
        self.assertRegex(cookies[0], r"^pdv2_session=[0-9a-f]+\.[0-9a-f]{8}; HttpOnly; Path=/; SameSite=Strict$")
        self.assertEqual(cookies[1], "pdv2_hint=1; Path=/")
        self.assertNotIn("X-PDV-Lan-Error", response.headers)
        self.assertEqual(response.headers["Content-Type"], "application/json")
        self.assertEqual(int(response.headers["Content-Length"]), len(response.data))
        # The browser keeps the cookie and sends it back through the bridge.
        session = response.json["session"]
        self.assertEqual(client.get("/api/echo", base_url=BASE).json["cookie"],
                         f"pdv2_session={session}; pdv2_hint=1")

    def test_statuses_and_documents_pass_intact(self):
        app, _bridge = self.station_app()
        client = app.test_client()
        response = client.get("/api/denied", base_url=BASE)
        self.assertEqual((response.status_code, response.json), (401, {"error": "Faça login com PIN"}))
        page = client.get("/", base_url=BASE, headers={"Accept": "text/html"})
        self.assertEqual((page.status_code, page.headers["Content-Type"]), (200, "text/html; charset=utf-8"))
        self.assertIn(b"<title>Caixa</title>", page.data)
        response = client.post("/api/pair", json={}, base_url=BASE, headers=ORIGIN)
        self.assertEqual((response.status_code, response.json["code"]), (403, "server_only"))
        self.assertEqual(client.get("/station/api/status", base_url=BASE).json["state"], "up")
        big = client.post("/api/echo", data=b"x" * (300 * 1024), base_url=BASE, headers=ORIGIN)
        self.assertEqual((big.status_code, big.json["code"]), (413, "too_large"))
        self.assertEqual(client.post("/api/echo", data=b"x" * (256 * 1024), base_url=BASE,
                                     headers=ORIGIN).json["length"], 256 * 1024)

    def test_large_response_is_streamed_in_blocks(self):
        app, _bridge = self.station_app()
        payload = os.urandom(200 * 1024)

        def big(environ, start_response):
            start_response("200 OK", [("Content-Type", "application/octet-stream")])
            return [payload]

        self.app.__class__ = type("Big", (FakeApp,), {"__call__": staticmethod(big)})
        self.addCleanup(setattr, self.app, "__class__", FakeApp)
        response = app.test_client().get("/static/grande.bin", base_url=BASE, buffered=False)
        chunks = list(response.response)
        self.assertEqual(b"".join(chunks), payload)
        self.assertEqual(max(len(chunk) for chunk in chunks), station.BLOCK)
        self.assertEqual(response.headers["Content-Length"], str(len(payload)))

    def test_read_timeouts(self):
        seen = []

        class Recorder:
            def __init__(self, *_args, **_options):
                pass

            def request(self, method, path, *, body=None, headers=None, timeout=None):
                seen.append((method, path, timeout))
                response = MagicMock(status=200, headers={}, header_list=[("Content-Type", "application/json")])
                response.read.side_effect = [b"{}", b""]
                return response

            def close(self):
                pass

        app, _bridge = self.station_app(CLIENT_FACTORY=Recorder)
        client = app.test_client()
        client.get("/api/session", base_url=BASE)
        client.post("/api/sales", json={}, base_url=BASE, headers=ORIGIN)
        client.post("/api/sync/now", json={}, base_url=BASE, headers=ORIGIN)
        client.post("/api/license/renew", json={}, base_url=BASE, headers=ORIGIN)
        client.get("/api/sync/now", base_url=BASE)
        self.assertEqual(seen, [("GET", "/api/session", 30.0), ("POST", "/api/sales", 30.0),
                                ("POST", "/api/sync/now", 75.0), ("POST", "/api/license/renew", 75.0),
                                ("GET", "/api/sync/now", 30.0)])
        self.assertEqual(station.CONNECT_SECONDS, 3.0)

    def test_print_discover_lists_the_printers_of_this_computer(self):
        app, bridge = self.station_app()
        client = app.test_client()
        with patch.object(station.printers, "cups_queues", return_value=["fila-deste-caixa"]):
            response = client.get("/api/print/discover", base_url=BASE)
        self.assertEqual(response.json, {"driver": "cups", "queues": ["fila-deste-caixa"]})
        self.assertEqual(self.app.paths(), ["/api/print/discover"], "the server still checks the login")

        def denied(environ, start_response):
            start_response("401 UNAUTHORIZED", [("Content-Type", "application/json")])
            return [json.dumps({"error": "Faça login com PIN"}).encode()]

        self.app.__class__ = type("Denied", (FakeApp,), {"__call__": staticmethod(denied)})
        self.addCleanup(setattr, self.app, "__class__", FakeApp)
        with patch.object(station.printers, "cups_queues", return_value=["fila-deste-caixa"]):
            response = client.get("/api/print/discover", base_url=BASE)
        self.assertEqual((response.status_code, response.json), (401, {"error": "Faça login com PIN"}))

    def test_sales_and_print_routes_wake_the_agent(self):
        app, bridge = self.station_app()
        client = app.test_client()
        for path, expected in (("/api/sales", True), ("/api/customers/receive", True), ("/api/print/flush", True),
                               ("/api/sales/abc/reprint", True), ("/api/cart/quote", False), ("/api/denied", False)):
            bridge._print_wake.clear()
            client.post(path, json={"request_id": path}, base_url=BASE, headers=ORIGIN)
            self.assertEqual(bridge._print_wake.is_set(), expected, path)
        bridge._print_wake.clear()
        client.get("/api/sales", base_url=BASE)
        self.assertFalse(bridge._print_wake.is_set())

    def test_three_stations_at_once_never_mix_station_or_session(self):
        bridges = [self.station_app(name) for name in ("Caixa 2", "Caixa 3")]
        self.store.limit = 3
        bridges.append(self.station_app("Caixa 4"))
        errors, sessions = [], {}

        def cashier(app, bridge, rounds=25):
            try:
                client = app.test_client()
                login = client.post("/api/login", json={}, base_url=BASE, headers=ORIGIN)
                session = login.json["session"]
                sessions[bridge.test_id] = session
                if not session.startswith(bridge.test_id + "."):
                    errors.append("login de outro caixa")
                for index in range(rounds):
                    data = client.get(f"/api/echo?n={index}", base_url=BASE).json
                    if data["station"]["id"] != bridge.test_id:
                        errors.append(f"estação trocada: {data['station']}")
                    if data["cookie"] != f"pdv2_session={session}; pdv2_hint=1":
                        errors.append(f"cookie trocado: {data['cookie']}")
                    sale = client.post("/api/sales", json={"request_id": f"{bridge.test_id}-{index}"},
                                       base_url=BASE, headers=ORIGIN)
                    if sale.status_code != 201 or sale.json["station"]["id"] != bridge.test_id:
                        errors.append("venda no caixa errado")
            except Exception as exc:  # reported by the main thread
                errors.append(repr(exc))

        for count in (2, 3):
            self.app.calls.clear()
            threads = [threading.Thread(target=cashier, args=pair) for pair in bridges[:count]]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(30)
            self.assertEqual(errors, [])
            seen = {call["station"]["id"] for call in self.app.calls}
            self.assertEqual(seen, {bridge.test_id for _app, bridge in bridges[:count]})
        self.assertEqual(len(set(sessions.values())), 3)
        self.assertEqual(self.app.sale_runs, 3 * 25, "the second round repeated the same request ids")
        for call in self.app.calls:
            cookie = call["environ"].get("HTTP_COOKIE", "")
            if cookie:
                self.assertIn(call["station"]["id"], cookie)


class ServerDownTests(StationCase):
    def sale(self, client, request_id):
        return client.post("/api/sales", json={"request_id": request_id, "items": [1, 2]}, base_url=BASE,
                           headers=ORIGIN)

    def test_cable_pulled_in_the_middle_of_a_sale(self):
        app, bridge = self.station_app()
        client = app.test_client()
        self.assertEqual(self.sale(client, "r-1").status_code, 201)
        port = self.port
        # 1) Server off: connection refused, nothing was sent.
        self.server.stop()
        response = self.sale(client, "r-2")
        self.assertEqual((response.status_code, response.json), (503, {
            "error": "O caixa principal está fora do ar. Este caixa volta sozinho quando a conexão voltar.",
            "code": "server_unreachable"}))
        self.assertEqual(client.get("/station/api/status", base_url=BASE).json["state"], "down")
        page = client.get("/", base_url=BASE, headers={"Accept": "text/html"})
        self.assertEqual((page.status_code, page.headers["Location"]), (302, "/station/"))
        asset = client.get("/static/terminal.js", base_url=BASE)
        self.assertEqual((asset.status_code, asset.json["code"]), (503, "server_unreachable"))
        # 2) Cable pulled after the request left: accepted, no answer.
        silent = SilentServer(self.temp.name, port)
        try:
            with patch.object(station, "READ_SECONDS", 0.5):
                response = self.sale(client, "r-2")
            self.assertEqual((response.status_code, response.json), (504, {
                "error": "O caixa principal não respondeu a tempo. Confira na lista de vendas se a operação foi "
                         "concluída antes de repetir.", "code": "server_timeout"}))
            wait_for(lambda: b"POST /api/sales" in silent.data(), message="request at the silent server")
            time.sleep(0.3)
            self.assertEqual(silent.data().count(b"POST /api/sales"), 1, "the bridge never repeats a POST")
            self.assertEqual(self.app.sale_runs, 1)
        finally:
            silent.close()
        # 3) Server back: the screen repeats the same body and the sale happens once.
        self.restart_server(port)
        first = self.sale(client, "r-2")
        self.assertEqual(first.status_code, 201)
        again = self.sale(client, "r-2")
        self.assertEqual((again.status_code, again.json["sale_id"]), (201, first.json["sale_id"]))
        self.assertEqual(self.app.sale_runs, 2)
        self.assertEqual(client.get("/station/api/status", base_url=BASE).json["state"], "up")

    def test_get_is_retried_once_on_a_dropped_connection_and_post_is_not(self):
        app, bridge = self.station_app()
        client = app.test_client()
        self.assertEqual(client.get("/api/echo", base_url=BASE).status_code, 200)
        port = self.port
        self.server.stop()
        self.restart_server(port)
        # The pooled connection died with the old listener: both verbs still work, on a new one.
        self.assertEqual(client.get("/api/echo", base_url=BASE).status_code, 200)
        self.server.stop()
        self.restart_server(port)
        self.assertEqual(self.sale(client, "r-9").status_code, 201)
        self.assertEqual(self.app.sale_runs, 1)

    def test_server_restart_reconnects_alone_with_the_same_token(self):
        app, bridge = self.station_app(PING_UP_SECONDS=0.05, PING_DOWN_SECONDS=0.05, PRINT_SECONDS=0.05)
        client = app.test_client()
        station.start_background(app)
        wait_for(lambda: bridge.status == "up", message="station up")
        health, stop = [], threading.Event()

        def probe():
            checker = app.test_client()
            while not stop.is_set():
                health.append(checker.get("/health", base_url=BASE).status_code)
                time.sleep(0.01)

        thread = threading.Thread(target=probe)
        thread.start()
        try:
            port, fingerprint = self.port, self.fingerprint
            self.server.stop()
            wait_for(lambda: bridge.status == "down", message="station down")
            self.assertEqual(client.get("/api/session", base_url=BASE).status_code, 503)
            status = client.get("/station/api/status", base_url=BASE).json
            self.assertEqual(status["state"], "down")
            self.assertIsNotNone(status["server"]["down_since"])
            self.assertIsNotNone(status["server"]["last_ok_at"])
            self.assertEqual([item["id"] for item in status["diagnosis"]][:4],
                             ["network", "server_off", "service_off", "clock_behind"])
            self.restart_server(port)
            self.assertEqual(self.server.fingerprint, fingerprint, "same certificate after the restart")
            wait_for(lambda: bridge.status == "up", message="station up again")
        finally:
            stop.set()
            thread.join(5)
        self.assertEqual(client.get("/api/echo", base_url=BASE).json["station"]["id"], bridge.test_id)
        self.assertEqual(station.load_state(bridge.test_dir)["token"], bridge.test_token)
        self.assertGreater(len(health), 5)
        self.assertEqual(set(health), {200}, "/health answered 200 the whole time")

    def test_diagnosis_texts(self):
        app, bridge = self.station_app()
        self.server.stop()
        with patch.object(station.discovery, "local_addresses", return_value=["192.168.0.20"]), \
                patch.object(station.discovery, "network_profile", return_value="public"):
            bridge.check_once()
            items = {item["id"]: item for item in bridge.diagnosis()}
        self.assertEqual((items["network"]["ok"], items["network"]["title"], items["network"]["help"]),
                         (True, "Este computador está sem rede", "Confira o cabo de rede ou o Wi-Fi."))
        # The firewall rule covers every profile: a "Public" network is not a cause to show.
        self.assertNotIn("public_profile", items)
        # The connection was REFUSED: the computer is on and the PDV2 is not listening on it.
        self.assertEqual(bridge.error_kind, "refused")
        self.assertEqual((items["service_off"]["ok"], items["service_off"]["title"], items["service_off"]["help"]),
                         (False, "O computador principal está ligado, mas o PDV2 não está aberto nele",
                          "Abra o PDV2 no computador principal ou reinicie-o."))
        self.assertTrue(items["server_off"]["ok"])
        self.assertTrue(items["clock_behind"]["ok"])
        # No answer at all (timeout) is what a computer that is OFF looks like.
        bridge.error_kind = "timeout"
        with patch.object(station.discovery, "local_addresses", return_value=["192.168.0.20"]):
            items = {item["id"]: item for item in bridge.diagnosis()}
        self.assertEqual((items["server_off"]["ok"], items["server_off"]["title"]),
                         (False, "O computador principal está desligado ou fora da rede"))
        self.assertIn("Ligue o computador principal e espere um minuto.", items["server_off"]["help"])
        self.assertIn("firewall", items["server_off"]["help"])
        self.assertTrue(items["service_off"]["ok"])
        with patch.object(station.discovery, "local_addresses", return_value=[]):
            items = {item["id"]: item for item in bridge.diagnosis()}
        self.assertEqual((items["network"]["ok"], items["server_off"]["ok"], items["service_off"]["ok"]),
                         (False, True, True), "without network only the network is blamed")
        bridge.clock_behind = 420
        with patch.object(station.discovery, "local_addresses", return_value=["192.168.0.20"]):
            items = {item["id"]: item for item in bridge.diagnosis()}
        self.assertEqual((items["clock_behind"]["ok"], items["clock_behind"]["title"]),
                         (False, "O relógio deste computador está atrasado em relação ao do caixa principal"))

    def test_revoked_token_during_use(self):
        app, bridge = self.station_app()
        client = app.test_client()
        self.assertEqual(client.get("/api/echo", base_url=BASE).status_code, 200)
        self.store.rows[bridge.test_id]["revoked"] = True
        response = client.post("/api/sales", json={"request_id": "r-1"}, base_url=BASE, headers=ORIGIN)
        self.assertEqual((response.status_code, response.json), (403, {
            "error": "Este caixa foi removido do caixa principal.", "code": "station_revoked"}))
        status = client.get("/station/api/status", base_url=BASE).json
        self.assertEqual((status["state"], status["reason"], status["linked"]), ("revoked", "station_revoked", True))
        calls = len(self.store.auth_tokens)
        response = client.get("/api/session", base_url=BASE)
        self.assertEqual((response.status_code, response.json["code"]), (403, "station_revoked"))
        self.assertEqual(len(self.store.auth_tokens), calls, "a revoked station stops knocking")
        page = client.get("/", base_url=BASE, headers={"Accept": "text/html"})
        self.assertEqual((page.status_code, page.headers["Location"]), (302, "/station/"))
        self.assertEqual(client.get("/health", base_url=BASE).json["server"], "down")
        self.assertEqual(bridge.check_once(), "revoked")
        self.assertEqual(bridge.print_once(), 0)
        self.assertIn("Este caixa foi removido do caixa principal. Para voltar a usar, peça um novo código ao "
                      "gerente.", (STATIC / "station.js").read_text(encoding="utf-8"))
        # A new code brings the same station back.
        self.store.code = "483920"
        response = client.post("/station/api/link", json={
            "address": HOST, "port": self.port, "fingerprint": self.fingerprint, "code": "483920"},
            base_url=BASE, headers=LOCAL)
        self.assertEqual((response.status_code, response.json["station"]["id"]), (200, bridge.test_id))
        self.assertEqual(client.get("/api/echo", base_url=BASE).json["station"]["id"], bridge.test_id)

    def test_unknown_token_is_reported_as_not_linked(self):
        app, bridge = self.station_app()
        self.store.rows[bridge.test_id]["token"] = "outro-" + "t" * 40
        response = app.test_client().get("/api/session", base_url=BASE)
        self.assertEqual((response.status_code, response.json), (403, {
            "error": "Este caixa não está vinculado ao caixa principal.", "code": "station_unauthorized"}))
        self.assertEqual(bridge.status, "revoked")

    def test_incompatible_versions(self):
        app, bridge = self.station_app()
        client = app.test_client()
        with patch.object(lan, "LAN_API", 0), patch.object(lan, "MIN_API", 0):
            response = client.get("/api/session", base_url=BASE)
            self.assertEqual((response.status_code, response.json["code"]), (426, "server_outdated"))
            status = client.get("/station/api/status", base_url=BASE).json
            self.assertEqual((status["state"], status["reason"], status["can_promote"]),
                             ("incompatible", "server_outdated", False))
            page = client.get("/", base_url=BASE, headers={"Accept": "text/html"})
            self.assertEqual(page.headers["Location"], "/station/")
        self.assertEqual(bridge.check_once(), "up")

    def test_station_outside_the_plan_gets_the_message_of_the_server(self):
        self.store.add_station("Caixa 2")
        app, bridge = self.station_app("Caixa 3")
        self.store.limit = 1
        response = app.test_client().get("/api/session", base_url=BASE)
        self.assertEqual((response.status_code, response.json["code"]), (403, "station_limit"))
        self.assertIn("O plano da loja não inclui mais este caixa adicional", response.json["error"])
        self.assertNotIn("X-PDV-Lan-Error", response.headers)


class RediscoveryTests(StationCase):
    def test_address_changed_is_fixed_and_reported(self):
        found = []
        app, bridge = self.station_app(DISCOVER=lambda shop_id=None: found.append(shop_id) or announced)
        announced = []
        self.assertEqual(bridge.check_once(), "up")
        old_port = self.port
        self.server.stop()
        self.assertEqual(bridge.check_once(), "down")
        moved = self.restart_server(0)
        self.assertNotEqual(moved.port, old_port)
        self.assertEqual(moved.fingerprint, self.fingerprint, "same computer, same certificate")
        announced.append(self.announce(moved))
        bridge._last_discover = 0.0
        self.assertEqual(bridge.check_once(), "up")
        saved = station.load_state(bridge.test_dir)
        self.assertEqual((saved["server"]["address"], saved["server"]["port"]), (HOST, moved.port))
        self.assertEqual((saved["token"], saved["addresses"]), (bridge.test_token, [HOST]))
        self.assertEqual(found[-1], self.store.shop_id, "discovery asks only for this shop")
        change = [item for item in bridge.diagnosis() if item["id"] == "ip_changed"]
        self.assertEqual(len(change), 1)
        self.assertEqual((change[0]["ok"], change[0]["title"]), (
            True, f"O endereço do caixa principal mudou (era {HOST}, agora {HOST}). Já foi corrigido."))
        self.assertEqual(app.test_client().get("/api/echo", base_url=BASE).json["station"]["id"], bridge.test_id)

    def test_known_addresses_are_tried_before_discovery(self):
        discover = MagicMock(return_value=[])
        app, bridge = self.station_app(DISCOVER=discover)
        # The last address stopped answering; the server is at another address this station already knew.
        with bridge.lock:
            bridge.state["server"]["address"] = "192.0.2.1"
            bridge.state["addresses"] = ["192.0.2.1", HOST]
        bridge._last_discover = time.monotonic()
        bridge._client = LanClient("192.0.2.1", self.port, self.fingerprint, bridge.test_token,
                                   timeout=0.3, connect_timeout=0.3)
        self.assertEqual(bridge.check_once(), "up")
        discover.assert_not_called()
        self.assertEqual(station.load_state(bridge.test_dir)["addresses"], [HOST, "192.0.2.1"])
        self.assertEqual(bridge.diagnosis()[-1]["title"],
                         f"O endereço do caixa principal mudou (era 192.0.2.1, agora {HOST}). Já foi corrigido.")

    def test_same_shop_with_another_certificate_asks_for_a_new_link(self):
        created = []

        def factory(address, port, fingerprint, token=None, **options):
            created.append((port, token))
            return LanClient(address, port, fingerprint, token, **options)

        announced = []
        app, bridge = self.station_app(DISCOVER=lambda shop_id=None: announced, CLIENT_FACTORY=factory)
        client = app.test_client()
        self.assertEqual(bridge.check_once(), "up")
        self.server.stop()
        # The promoted computer: same shop and terminal, new certificate, tokens gone.
        new_store = FakeStore(shop_id=self.store.shop_id, terminal_id=self.store.terminal_id)
        new_temp = tempfile.TemporaryDirectory()
        self.addCleanup(new_temp.cleanup)
        new_app = FakeApp(new_store, new_temp.name)
        promoted = lan.start(new_app, host=HOST, port=0)
        self.addCleanup(promoted.stop)
        self.assertNotEqual(promoted.fingerprint, self.fingerprint)
        announced.append(self.announce(promoted, new_store))
        bridge._last_discover = 0.0
        created.clear()
        self.assertEqual(bridge.check_once(), "relink")
        self.assertEqual(new_store.auth_tokens, [], "the token is never offered to another identity")
        self.assertEqual([token for port, token in created if port == promoted.port], [None])
        self.assertEqual(new_app.calls, [])
        status = client.get("/station/api/status", base_url=BASE).json
        self.assertEqual((status["state"], status["can_promote"]), ("relink", False))
        self.assertEqual(status["relink"], {"host": socket.gethostname(), "address": HOST, "port": promoted.port,
                                            "fingerprint": promoted.fingerprint, "shop_name": "Mercado X",
                                            "terminal_name": "Caixa 1"})
        self.assertEqual(status["diagnosis"][-1], {
            "id": "identity_changed", "ok": False, "title": "O caixa principal mudou de computador",
            "help": "Peça ao gerente o código de 6 números em Sistema → Caixas da loja → Adicionar caixa e "
                    "digite aqui."})
        saved = station.load_state(bridge.test_dir)
        self.assertEqual((saved["server"]["fingerprint"], saved["server"]["port"]), (self.fingerprint, self.port),
                         "nothing is saved before the manager gives a new code")
        self.assertEqual(client.get("/api/session", base_url=BASE).status_code, 503)
        # The 6-digit code of the new server points this station to it, with the same identity.
        new_store.rows[bridge.test_id] = {"name": "Caixa 2", "token": "x" * 43, "revoked": False, "created": 0}
        new_store.code = "654987"
        response = client.post("/station/api/link", json={
            "address": HOST, "port": promoted.port, "fingerprint": promoted.fingerprint, "code": "654987"},
            base_url=BASE, headers=LOCAL)
        self.assertEqual((response.status_code, response.json["station"]["id"]), (200, bridge.test_id))
        self.assertEqual(new_store.enrolls[0]["station_id"], bridge.test_id)
        self.assertEqual(client.get("/api/echo", base_url=BASE).json["station"]["id"], bridge.test_id)
        self.assertEqual(client.get("/station/api/status", base_url=BASE).json["state"], "up")

    def test_another_terminal_of_the_same_shop_is_ignored(self):
        # Terminal 1 (the former hub, still listening) must not capture the caixas of Terminal 2
        # when Terminal 2 breaks, nor hide "Assumir como caixa principal".
        announced = []
        app, bridge = self.station_app(DISCOVER=lambda shop_id=None: announced, PROMOTE_AFTER_SECONDS=0)
        self.assertEqual(bridge.check_once(), "up")
        self.server.stop()
        other_store = FakeStore(shop_id=self.store.shop_id)
        self.assertNotEqual(other_store.terminal_id, self.store.terminal_id)
        other_temp = tempfile.TemporaryDirectory()
        self.addCleanup(other_temp.cleanup)
        other = lan.start(FakeApp(other_store, other_temp.name), host=HOST, port=0)
        self.addCleanup(other.stop)
        announced.append(self.announce(other, other_store))
        bridge._last_discover = 0.0
        self.assertEqual(bridge.check_once(), "down")
        self.assertIsNone(bridge.rediscover(force=True))
        self.assertIsNone(bridge.relink)
        self.assertEqual(other_store.auth_tokens, [])
        with patch.object(station, "promotion", FakePromotion()):
            status = app.test_client().get("/station/api/status", base_url=BASE).json
        self.assertEqual((status["state"], status["can_promote"]), ("down", True))

    def test_another_shop_is_ignored(self):
        announced = []
        app, bridge = self.station_app(DISCOVER=lambda shop_id=None: announced)
        self.server.stop()
        other_store = FakeStore()
        other_temp = tempfile.TemporaryDirectory()
        self.addCleanup(other_temp.cleanup)
        other = lan.start(FakeApp(other_store, other_temp.name), host=HOST, port=0)
        self.addCleanup(other.stop)
        announced.append(self.announce(other, other_store))
        self.assertEqual(bridge.check_once(), "down")
        self.assertIsNone(bridge.relink)
        self.assertEqual(other_store.auth_tokens, [])

    def test_original_server_back_cancels_the_relink(self):
        app, bridge = self.station_app()
        self.assertEqual(bridge.check_once(), "up")
        with bridge.lock:
            bridge.status, bridge.relink = "relink", {"host": "X"}
        self.assertEqual(bridge.check_once(), "up")
        self.assertIsNone(bridge.relink)


class PrintAgentTests(StationCase):
    def agent(self, **config):
        sent = []

        def send(driver, target, payload, job_id):
            sent.append((driver, target, payload, job_id))
            if payload == b"quebra":
                raise OSError("impressora sem papel")

        app, bridge = self.station_app(SEND_BYTES=send, **config)
        self.assertEqual(bridge.check_once(), "up")
        return app, bridge, sent

    def test_job_is_fetched_printed_here_and_confirmed(self):
        _app, bridge, sent = self.agent()
        self.store.queue("server", b"do principal")
        job = self.store.queue(bridge.test_id, b"\x1b@recibo", driver="cups", target="Termica")
        self.assertEqual(bridge.print_once(), 1)
        self.assertEqual(sent, [("cups", "Termica", b"\x1b@recibo", job)])
        self.assertEqual(self.store.finished, [(bridge.test_id, job, None)])
        self.assertEqual(self.store.recovered, [bridge.test_id], "recover runs once, at the start")
        self.assertEqual(bridge.print_once(), 0)
        self.assertEqual(self.store.recovered, [bridge.test_id])
        self.assertEqual(len(self.store.jobs["server"]), 1)
        self.assertFalse((bridge.test_dir / station.PRINT_PENDING_FILE).exists())

    def test_lost_answer_of_print_next_asks_for_recover_again(self):
        lose = []

        class Lossy(LanClient):
            def request(self, method, path, **options):
                response = super().request(method, path, **options)
                if path == "/lan/v1/print/next" and lose:
                    lose.pop()
                    response.close()  # the server already took the job; the answer never arrives
                    raise LanError("timeout", "sem resposta", sent=True, kind="timeout")
                return response

        _app, bridge, sent = self.agent(CLIENT_FACTORY=Lossy)
        self.assertEqual(bridge.print_once(), 0)
        self.assertEqual(self.store.recovered, [bridge.test_id])
        job = self.store.queue(bridge.test_id, b"recibo")
        lose.append(True)
        with self.assertRaises(LanError):
            bridge.print_once()
        self.assertEqual((sent, self.store.sending), ([], {job: bridge.test_id}))
        self.assertEqual(bridge.check_once(), "up")
        self.assertEqual(bridge.print_once(), 0)
        self.assertEqual(self.store.recovered, [bridge.test_id, bridge.test_id],
                         "the server is told to release the job that never arrived")
        self.assertEqual(bridge.print_once(), 0)
        self.assertEqual(len(self.store.recovered), 2, "and only once per lost contact")

    def test_default_transport_is_the_local_printer_driver(self):
        app, bridge = self.station_app()
        bridge.check_once()
        job = self.store.queue(bridge.test_id, b"recibo")
        with patch.object(station.printers, "send_bytes") as send:
            self.assertEqual(bridge.print_once(), 1)
        send.assert_called_once_with("tcp", "127.0.0.1:9100", b"recibo", job)

    def test_print_error_comes_back_as_error(self):
        _app, bridge, _sent = self.agent()
        bad = self.store.queue(bridge.test_id, b"quebra")
        good = self.store.queue(bridge.test_id, b"recibo")
        self.assertEqual(bridge.print_once(), 2)
        self.assertEqual(self.store.finished, [(bridge.test_id, bad, "impressora sem papel"),
                                               (bridge.test_id, good, None)])

    def test_failed_confirmation_stays_on_disk_and_is_sent_again(self):
        _app, bridge, sent = self.agent()
        job = self.store.queue(bridge.test_id, b"recibo")
        port = self.port
        bridge.send_bytes = lambda *args: (sent.append(args), self.server.stop())  # cable pulled while printing
        self.assertEqual(bridge.print_once(), 1)
        self.assertEqual(bridge.print_once(), 0, "nothing new is fetched while a result waits")
        pending = bridge.test_dir / station.PRINT_PENDING_FILE
        self.assertEqual(json.loads(pending.read_text()), [{"id": job, "error": None}])
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(pending.stat().st_mode), 0o600)
        self.assertEqual((len(sent), self.store.finished), (1, []))
        self.restart_server(port)
        self.assertEqual(bridge.print_once(), 0)
        self.assertEqual(self.store.finished, [(bridge.test_id, job, None)], "sent again, not printed again")
        self.assertEqual(len(sent), 1)
        self.assertFalse(pending.exists())

    def test_pending_result_survives_a_restart_of_the_station(self):
        app, bridge, _sent = self.agent()
        job = self.store.queue(bridge.test_id, b"recibo")
        self.store.fail_finish = True
        with self.assertLogs("pdv2.lan", level="ERROR"):
            self.assertEqual(bridge.print_once(), 1)
        self.assertEqual(bridge.pending_results(), [{"id": job, "error": None}])
        self.store.fail_finish = False
        again = station.create_app({"TESTING": True, "DATA_DIR": str(bridge.test_dir),
                                    "SEND_BYTES": MagicMock(side_effect=AssertionError("printed twice"))})
        self.addCleanup(station.stop_background, again)
        restarted = again.extensions["station"]
        restarted.check_once()
        self.assertEqual(restarted.print_once(), 0)
        self.assertEqual(self.store.recovered, [bridge.test_id, bridge.test_id], "recover on every start")
        self.assertEqual(self.store.finished, [(bridge.test_id, job, None)])
        self.assertEqual(restarted.pending_results(), [])

    def test_background_agent_prints_after_a_sale(self):
        app, bridge, sent = self.agent(PING_UP_SECONDS=0.05, PING_DOWN_SECONDS=0.05, PRINT_SECONDS=30)
        station.start_background(app)
        wait_for(lambda: self.store.recovered, message="recover at start")
        job = self.store.queue(bridge.test_id, b"recibo")
        time.sleep(0.2)
        self.assertEqual(sent, [], "the agent sleeps until something wakes it")
        app.test_client().post("/api/sales", json={"request_id": "r-1"}, base_url=BASE, headers=ORIGIN)
        wait_for(lambda: self.store.finished, message="receipt printed")
        self.assertEqual(self.store.finished, [(bridge.test_id, job, None)])


class PromotionTests(StationCase):
    def setUp(self):
        super().setUp()
        self.promotion = FakePromotion()
        patcher = patch.object(station, "promotion", self.promotion)
        patcher.start()
        self.addCleanup(patcher.stop)
        restart = patch.object(station.runtime, "request_restart", return_value=True)
        self.restart = restart.start()
        self.addCleanup(restart.stop)

    def down(self, bridge, seconds):
        self.assertEqual(bridge.check_once(), "down")
        with bridge.lock:
            bridge._down_mono = time.monotonic() - seconds

    def start(self, client, pin="654321", **changes):
        return client.post("/station/api/promotion/start", json={"manager_pin": pin, "confirm": True, **changes},
                           base_url=BASE, headers=LOCAL)

    def test_offer_only_after_sixty_seconds_out(self):
        app, bridge = self.station_app()
        client = app.test_client()
        self.assertEqual(bridge.check_once(), "up")
        self.assertFalse(client.get("/station/api/status", base_url=BASE).json["can_promote"])
        self.server.stop()
        self.down(bridge, 59)
        status = client.get("/station/api/status", base_url=BASE).json
        self.assertEqual((status["state"], status["can_promote"]), ("down", False))
        self.assertEqual(status["promotion"]["journal_seq"], 12)
        response = self.start(client)
        self.assertEqual((response.status_code, response.json["code"]), (409, "too_early"))
        self.down(bridge, 61)
        self.assertTrue(client.get("/station/api/status", base_url=BASE).json["can_promote"])
        payload = client.get("/station/api/promotion", base_url=BASE).json
        self.assertEqual((payload["available"], payload["can_promote"], payload["state"], payload["finished"]),
                         (True, True, "idle", False))
        self.assertGreaterEqual(payload["down_seconds"], 61)
        self.assertEqual(self.promotion.calls, [])

    def test_verify_checks_the_manager_pin(self):
        app, bridge = self.station_app()
        client = app.test_client()
        ok = client.post("/station/api/promotion/verify", json={"manager_pin": "654321"}, base_url=BASE,
                         headers=LOCAL)
        self.assertEqual((ok.status_code, ok.json), (200, {"ok": True, "manager": "Bruno"}))
        bad = client.post("/station/api/promotion/verify", json={"manager_pin": "000000"}, base_url=BASE,
                          headers=LOCAL)
        self.assertEqual((bad.status_code, bad.json), (403, {"error": "PIN de gerente inválido"}))
        self.assertEqual(client.post("/station/api/promotion/verify", json={"manager_pin": "654321"},
                                     base_url=BASE).status_code, 403, "needs the local header")
        with patch.object(self.promotion, "verify_manager", side_effect=RuntimeError("/caminho/secreto")), \
                self.assertLogs("pdv2.station", level="ERROR") as logs:
            broken = client.post("/station/api/promotion/verify", json={"manager_pin": "654321"}, base_url=BASE,
                                 headers=LOCAL)
        self.assertEqual(broken.status_code, 500)
        self.assertNotIn("secreto", broken.get_data(as_text=True) + "".join(logs.output))

    def test_start_with_the_server_alive_is_refused(self):
        app, bridge = self.station_app()
        client = app.test_client()
        port = self.port
        self.server.stop()
        self.down(bridge, 61)
        self.restart_server(port)  # it came back and the monitor has not noticed yet
        response = self.start(client)
        self.assertEqual((response.status_code, response.json), (409, {
            "error": "O caixa principal voltou a responder. Não é preciso assumir.", "code": "server_alive"}))
        self.assertEqual(self.promotion.calls, [])
        self.assertEqual(bridge.check_once(), "up")
        response = self.start(client)
        self.assertEqual((response.status_code, response.json["code"]), (409, "server_alive"))
        self.assertFalse((bridge.test_dir / "mode.json").exists())
        self.restart.assert_not_called()

    def test_damaged_database_of_the_server_is_not_out_of_reach(self):
        app, bridge = self.station_app(PROMOTE_AFTER_SECONDS=0)
        client = app.test_client()
        self.assertEqual(bridge.check_once(), "up")
        self.app.extensions["terminal_store"] = None
        self.assertEqual(bridge.check_once(), "damaged")
        status = client.get("/station/api/status", base_url=BASE).json
        self.assertEqual((status["state"], status["reason"], status["can_promote"], status["diagnosis"]),
                         ("damaged", "server_damaged", False, []))
        self.assertIsNone(status["server"]["down_since"])
        self.assertEqual(client.get("/health", base_url=BASE).json["server"], "down")
        page = client.get("/", base_url=BASE)
        self.assertEqual((page.status_code, page.headers["Location"]), (302, "/station/"))
        answer = client.get("/api/session", base_url=BASE)
        self.assertEqual((answer.status_code, answer.json["damaged"]), (503, True))
        self.assertIn("banco de dados danificado", answer.json["error"])
        response = self.start(client)
        self.assertEqual((response.status_code, response.json), (409, {
            "error": "O caixa principal está com o banco de dados danificado. Chame o suporte.",
            "code": "server_damaged"}))
        self.assertEqual(self.promotion.calls, [])
        # Repaired: the caixa comes back alone.
        self.app.extensions["terminal_store"] = self.store
        self.assertEqual(bridge.check_once(), "up")
        # And a server that is really off after that is "down" again, with the usual diagnosis.
        self.app.extensions["terminal_store"] = None
        self.assertEqual(bridge.check_once(), "damaged")
        self.server.stop()
        self.assertEqual(bridge.check_once(), "down")

    def test_start_needs_confirmation_and_pin(self):
        app, bridge = self.station_app()
        client = app.test_client()
        self.server.stop()
        self.down(bridge, 61)
        self.assertEqual(self.start(client, confirm=False).status_code, 400)
        self.assertEqual(self.start(client, pin="").status_code, 400)
        self.assertEqual(self.promotion.calls, [])

    def test_finished_promotion_writes_the_mode_and_asks_for_a_restart(self):
        app, bridge = self.station_app(PING_UP_SECONDS=0.05, PING_DOWN_SECONDS=0.05)
        client = app.test_client()
        station.start_background(app)
        wait_for(lambda: bridge.status == "up", message="station up")
        self.server.stop()
        wait_for(lambda: bridge.status == "down", message="station down")
        with bridge.lock:
            bridge._down_mono = time.monotonic() - 61
        self.promotion.release.clear()
        response = self.start(client)
        self.assertEqual((response.status_code, response.json), (202, {"state": "running"}))
        self.assertEqual(self.start(client).json["code"], "running")
        self.assertEqual(client.get("/station/api/promotion", base_url=BASE).json["state"], "running")
        self.assertFalse((bridge.test_dir / "mode.json").exists(), "nothing changes before the promotion ends")
        self.promotion.release.set()
        wait_for(lambda: self.restart.called, message="restart requested")
        self.restart.assert_called_once_with("promocao")
        self.assertEqual(self.promotion.calls, [(bridge.test_dir, "654321", bridge.test_id, "Caixa 2")])
        self.assertEqual(paths.read_mode(bridge.test_dir, {}), "server")
        self.assertFalse((bridge.test_dir / "station.json").exists())
        self.assertEqual(json.loads((bridge.test_dir / "station-anterior.json").read_text())["station_id"],
                         bridge.test_id)
        payload = client.get("/station/api/promotion", base_url=BASE).json
        self.assertEqual((payload["finished"], payload["state"], payload["can_promote"]), (True, "done", False))
        wait_for(lambda: "pdv-station-monitor" not in [thread.name for thread in threading.enumerate()],
                 message="background stopped")
        health = client.get("/health", base_url=BASE)
        self.assertEqual((health.status_code, health.json["mode"]), (200, "station"),
                         "still the station process until the restart")

    def test_failed_promotion_keeps_the_station(self):
        app, bridge = self.station_app()
        client = app.test_client()
        self.server.stop()
        self.down(bridge, 61)
        self.promotion.fail = FakeError("PIN de gerente inválido", 403)
        self.assertEqual(self.start(client, pin="000000").status_code, 202)
        wait_for(lambda: self.promotion.calls and not bridge.promoting, message="promotion failed")
        payload = client.get("/station/api/promotion", base_url=BASE).json
        self.assertEqual((payload["state"], payload["finished"]), ("failed", False))
        self.assertEqual(paths.read_mode(bridge.test_dir, {}), "server", "no mode file was written")
        self.assertFalse((bridge.test_dir / "mode.json").exists())
        self.assertTrue((bridge.test_dir / "station.json").exists())
        self.restart.assert_not_called()
        self.assertTrue(bridge.linked)
        # It may be tried again.
        self.promotion.fail = None
        self.assertEqual(self.start(client).status_code, 202)
        wait_for(lambda: self.restart.called, message="second attempt")

    def test_without_the_promotion_module_nothing_is_offered(self):
        with patch.object(station, "promotion", None):
            app, bridge = self.station_app()
            client = app.test_client()
            self.server.stop()
            self.down(bridge, 600)
            status = client.get("/station/api/status", base_url=BASE).json
            self.assertEqual((status["can_promote"], status["promotion"]), (False, None))
            payload = client.get("/station/api/promotion", base_url=BASE).json
            self.assertEqual((payload["available"], payload["can_promote"]), (False, False))
            self.assertEqual(self.start(client).status_code, 409)
            self.assertEqual(client.post("/station/api/promotion/verify", json={"manager_pin": "1"}, base_url=BASE,
                                         headers=LOCAL).status_code, 409)


class ReplicaPullerTests(StationCase):
    def test_puller_gets_a_client_of_the_current_server(self):
        started = []

        class Replica:
            @staticmethod
            def start_puller(data_dir, client_factory):
                puller = MagicMock()
                started.append((Path(data_dir), client_factory, puller))
                return puller

            @staticmethod
            def puller_status(_data_dir):
                return {"state": "ok", "message": "Cópia de segurança do caixa principal em dia", "journal_seq": 3}

        with patch.object(station, "replica_net", Replica):
            app, bridge = self.station_app(PING_UP_SECONDS=30, PING_DOWN_SECONDS=30)
            station.start_background(app)
            self.assertEqual(started[0][0], bridge.test_dir)
            own = started[0][1]()
            self.addCleanup(own.close)
            self.assertIsNot(own, bridge.client(), "the puller closes its client: never the one of the bridge")
            response = own.request("GET", "/lan/v1/ping", headers=dict([station.lanclient.NATIVE_HEADER]))
            self.assertEqual(response.status, 200)
            response.close()
            status = app.test_client().get("/station/api/status", base_url=BASE).json
            self.assertEqual(status["replica"]["state"], "ok")
            station.stop_background(app)
            started[0][2].stop.assert_called_once()
            bridge.unlink()
            with self.assertRaises(OSError):
                started[0][1]()


if __name__ == "__main__":
    unittest.main()
