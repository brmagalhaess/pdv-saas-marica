"""Réplica pela rede: o puxador do caixa adicional ligado ao handle_request do principal por um
cliente falso em processo, com opção de derrubar a conexão no meio."""

import hashlib
import json
import os
import sqlite3
import tempfile
import threading
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs

from cryptography.fernet import Fernet

from terminal import mirror, replica_net
from terminal.store import Store

TENANT = "11111111-1111-4111-8111-111111111111"
SHOP = "22222222-2222-4222-8222-222222222222"
TERMINAL = "33333333-3333-4333-8333-333333333333"
MARKER = "segredo-do-balcao"
TOKEN = "token-secreto-do-terminal"
MINUTE = 60 * 1000


def new_key(key_id="a1b2c3d4e5f60718"):
    return {"key_id": key_id, "key": Fernet.generate_key().decode()}


class FakeResponse:
    """Resposta do cliente da rede local; ``cut`` derruba a conexão depois de tantos bytes."""

    def __init__(self, status, headers, body, cut=None):
        self.status = status
        self.headers = {str(name).lower(): str(value) for name, value in headers.items()}
        self._source = iter([body]) if isinstance(body, (bytes, bytearray)) else body
        self._buffer, self._served, self._cut = b"", 0, cut

    def read(self, amount=-1):
        if self._cut is not None and self._served >= self._cut:
            raise ConnectionResetError("cabo puxado")
        while amount < 0 or len(self._buffer) < amount:
            try:
                self._buffer += next(self._source)
            except StopIteration:
                break
        size = len(self._buffer) if amount < 0 else min(amount, len(self._buffer))
        if self._cut is not None:
            size = min(size, self._cut - self._served)
        block, self._buffer = self._buffer[:size], self._buffer[size:]
        self._served += len(block)
        return block

    def close(self):
        if hasattr(self._source, "close"):
            self._source.close()


class Lan:
    """A rede da loja de mentira: entrega os pedidos do puxador direto ao handle_request."""

    def __init__(self, store, station_id="caixa-2"):
        self.store = store
        self.station = {"id": station_id, "name": station_id}
        self.down = False
        self.calls = []
        self.cuts = {}        # trecho do caminho -> fração do corpo entregue antes de a conexão cair (uma vez)
        self.tamper = None    # função (caminho, status, cabeçalhos, corpo) -> (status, cabeçalhos, corpo)

    def client(self):
        return self

    def request(self, method, path, *, body=None, headers=None, timeout=None):
        self.calls.append((method, path, dict(headers or {})))
        if self.down:
            raise ConnectionRefusedError("caixa principal desligado")
        route, _, raw = path.partition("?")
        query = {name: values[-1] for name, values in parse_qs(raw).items()}
        # Como a escuta TLS chama: seis argumentos, sem o corpo.
        status, reply, payload = replica_net.handle_request(self.store, self.station, method, route, query,
                                                            headers or {})
        if self.tamper is not None:
            if not isinstance(payload, (bytes, bytearray)):
                payload = b"".join(payload)
            status, reply, payload = self.tamper(route, status, reply, payload)
        cut = None
        for fragment in list(self.cuts):
            if fragment in route:
                cut = int(int(reply["Content-Length"]) * self.cuts.pop(fragment))
                break
        return FakeResponse(status, reply, payload, cut)

    def close(self):
        pass

    def count(self, fragment):
        return sum(1 for _, path, _ in self.calls if fragment in path)


class ReplicaCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.key = new_key()
        self.store = self.make_store(self.root / "principal" / "terminal.sqlite3", self.key)
        self.lan = Lan(self.store)
        self.station_dir = self.root / "caixa-2"
        self.station_dir.mkdir()
        self.puller = replica_net.Puller(self.station_dir, self.lan.client, interval=2.0)
        self.addCleanup(replica_net.stop_all)
        for registry in (mirror._SNAPSHOT_REQUESTS, mirror._SNAPSHOT_RETRY_AT, mirror._NEXT_DUE,
                         mirror._JOURNAL_CACHE):
            registry.clear()

    def make_store(self, path, key=None, terminal=TERMINAL):
        store = Store(path)
        values = {"tenant_id": TENANT, "shop_id": SHOP, "terminal_id": terminal, "token": TOKEN,
                  "tenant_name": "Mercado Bom Preço", "shop_name": "Matriz", "terminal_name": "Caixa 01"}
        if key:
            values["mirror_key"] = json.dumps(key)
        with store.transaction() as conn:
            for name, value in values.items():
                Store._set_meta(conn, name, value)
        return store

    def event(self, store=None, note=MARKER):
        store = store or self.store
        with store.transaction() as conn:
            return store._event(conn, "sale.completed", "venda-" + os.urandom(4).hex(), {"note": note})

    def events(self, count, note=MARKER):
        return [self.event(note=note) for _ in range(count)]

    def call(self, method, path, query=None, headers=None, body=None, station=None):
        return replica_net.handle_request(self.store, station or self.lan.station, method, path, query or {},
                                          headers or {}, body)

    def server_status(self):
        status, _, body = self.call("GET", "/lan/v1/replica/status")
        self.assertEqual(status, 200)
        return json.loads(body)

    def wait(self, condition, seconds=10.0, message="a réplica não reagiu a tempo"):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if condition():
                return
            time.sleep(0.02)
        self.fail(message)

    def wait_snapshot(self, newer_than=None):
        """Pede o estado até a cópia do principal ficar pronta (ela é feita em outra thread)."""
        found = {}

        def ready():
            found.update(self.server_status())
            snapshot = found.get("snapshot")
            return bool(snapshot) and snapshot["name"] != newer_than

        self.wait(ready)
        return found["snapshot"]

    def sync(self, puller=None):
        """Passadas até o caixa adicional ficar com a cópia em dia."""
        puller = puller or self.puller
        self.wait_snapshot()
        for _ in range(20):
            if puller.cycle() and replica_net.puller_status(puller.data_dir)["state"] == "ok":
                return replica_net.puller_status(puller.data_dir)
        self.fail(f"a cópia não ficou em dia: {replica_net.puller_status(puller.data_dir)}")

    def replica(self, puller=None, store=None):
        identity = (store or self.store).identity()
        base = (puller or self.puller).base
        return base / mirror.ROOT_NAME / identity["terminal_id"] / identity["device_id"]

    def journal(self, puller=None):
        """Registros do diário do caixa adicional, na ordem em que estão no disco."""
        records = []
        for path in sorted(self.replica(puller).glob("journal-*.pdv2log")):
            for line in path.read_bytes().split(b"\n"):
                if line:
                    record = mirror.parse_journal_line(line, self.key)
                    self.assertIsNotNone(record, "linha ilegível no diário do caixa adicional")
                    records.append(record)
        return records

    def max_seq(self):
        return mirror.max_seq(self.store)

    def new_identity(self):
        return {"tenant_id": TENANT, "shop_id": SHOP, "terminal_id": TERMINAL,
                "device_id": "99999999-9999-4999-8999-999999999999"}


class PrimeiraCopiaTests(ReplicaCase):
    def test_primeira_copia_chega_confere_e_o_diario_acompanha(self):
        self.events(3)
        status = self.sync()
        self.assertEqual(status["message"], "Cópia de segurança do caixa principal em dia")
        self.assertEqual((status["journal_seq"], status["server_seq"]), (3, 3))
        remote = self.server_status()["snapshot"]
        self.assertEqual(status["last_snapshot_at"], remote["created_at"])

        manifest = json.loads((self.replica() / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["format"], "pdv2-mirror")
        self.assertEqual([item["name"] for item in manifest["snapshots"]], [remote["name"]])
        self.assertEqual(manifest["journal_segments"], [{"name": "journal-000000000004.pdv2log", "first_seq": 4}])
        # O sha256 anunciado é o do arquivo em claro, e a cópia do caixa adicional bate com ele.
        plain = self.root / "aberta.sqlite3"
        with open(plain, "wb") as output:
            mirror._decrypt_snapshot(self.replica() / remote["name"], Fernet(self.key["key"].encode()), output)
        self.assertEqual(hashlib.sha256(plain.read_bytes()).hexdigest(), remote["sha256"])
        self.assertEqual((self.replica() / remote["name"]).stat().st_size, remote["size_bytes"])
        with closing(sqlite3.connect(plain)) as conn:
            self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            keys = {row[0] for row in conn.execute("SELECT key FROM meta")}
        self.assertNotIn("token", keys)
        self.assertNotIn("mirror_key", keys)

        later = self.events(2)
        self.assertTrue(self.puller.cycle())
        self.assertEqual([item["event_id"] for item in self.journal()], later)
        self.assertEqual(replica_net.puller_status(self.station_dir)["journal_seq"], 5)

    def test_arquivos_do_caixa_adicional_sao_ilegiveis_sem_a_chave(self):
        self.events(2)
        self.sync()
        self.events(2)
        self.puller.cycle()
        files = [path for path in self.puller.base.rglob("*") if path.is_file()]
        self.assertTrue(any(path.suffix == ".pdv2snap" for path in files))
        self.assertTrue(any(path.suffix == ".pdv2log" and path.stat().st_size for path in files))
        for path in files:
            content = path.read_bytes()
            self.assertNotIn(b"SQLite format 3", content)
            self.assertNotIn(MARKER.encode(), content)
            self.assertNotIn(TOKEN.encode(), content)
            if path.name != "state.json":  # o único lugar onde a chave fica
                self.assertNotIn(self.key["key"].encode(), content, path.name)
        state = self.puller.base / "state.json"
        self.assertIn(self.key["key"], state.read_text(encoding="utf-8"))
        if os.name == "posix":
            self.assertEqual(state.stat().st_mode & 0o777, 0o600)
        self.assertNotIn(self.key["key"], json.dumps(replica_net.puller_status(self.station_dir)))
        self.assertNotIn(self.key["key"], json.dumps(replica_net.status(self.store)))
        # A cópia de saída do principal também é cifrada e fica ao lado do banco dele.
        out = self.store.path.parent / "replica-out"
        self.assertEqual(len(list(out.glob("snapshot-*.pdv2snap"))), 1)
        for path in out.iterdir():
            self.assertNotIn(b"SQLite format 3", path.read_bytes())
            self.assertNotIn(self.key["key"].encode(), path.read_bytes())

    def test_leitores_da_fase_2_abrem_a_pasta_do_caixa_adicional(self):
        before = self.events(2)
        self.sync()
        after = self.events(2)
        self.puller.cycle()
        found = mirror.candidates(self.new_identity(), str(self.puller.base))
        self.assertEqual([(item["replica_id"], item["matches"], item["journal_seq"]) for item in found],
                         [(self.store.identity()["device_id"], True, 4)])
        data = mirror.read_pending(str(self.puller.base), self.key, self.new_identity())
        self.assertEqual([item["event_id"] for item in data["events"]], before + after)
        self.assertEqual((data["snapshot"]["outbox_seq"], data["journal_last_seq"], data["warnings"]), (2, 4, []))

    def test_thread_acompanha_cada_evento_em_segundos(self):
        self.wait_snapshot()
        with patch.object(replica_net.Puller, "LONG_POLL_SECONDS", 2.0):
            puller = replica_net.start_puller(self.station_dir, self.lan.client, interval=0.1)
            self.addCleanup(puller.stop)
            self.assertIs(replica_net.start_puller(self.station_dir, self.lan.client), puller)
            self.assertEqual((puller._thread.name, puller._thread.daemon), ("pdv-replica-pull", True))
            self.wait(lambda: replica_net.puller_status(self.station_dir)["state"] == "ok")
            for expected in (1, 2, 3):
                self.event()
                started = time.monotonic()
                self.wait(lambda: replica_net.puller_status(self.station_dir)["journal_seq"] == expected, 5.0)
                self.assertLess(time.monotonic() - started, 5.0)
            puller.stop()
            self.assertFalse(puller.is_alive())


class CaboPuxadoTests(ReplicaCase):
    def test_download_cortado_aos_40_por_cento_retoma_por_range(self):
        self.events(150, note=os.urandom(1500).hex())  # banco grande o bastante para vários blocos
        remote = self.wait_snapshot()
        self.lan.cuts["snapshot/"] = 0.4
        self.assertFalse(self.puller.cycle())
        part = self.replica() / (remote["name"] + ".part")
        self.assertEqual(part.stat().st_size, int(remote["size_bytes"] * 0.4))
        self.assertFalse((self.replica() / remote["name"]).exists())
        status = replica_net.puller_status(self.station_dir)
        self.assertEqual((status["state"], status["last_error"]), ("empty", "Sem contato com o caixa principal."))

        self.assertTrue(self.puller.cycle())
        ranges = [headers.get("Range") for _, path, headers in self.lan.calls if "snapshot/" in path]
        self.assertEqual(ranges, [None, f"bytes={int(remote['size_bytes'] * 0.4)}-"])
        self.assertFalse(part.exists())
        check = mirror.verify_snapshot(self.replica() / remote["name"], self.key)
        self.assertEqual((check["sha256"], check["header"]["outbox_seq"]), (remote["sha256"], 150))
        self.assertEqual(replica_net.puller_status(self.station_dir)["state"], "ok")

    def test_copia_que_nao_confere_e_descartada_e_baixada_de_novo(self):
        self.event()
        remote = self.wait_snapshot()

        def flip(route, status, headers, body):
            if "snapshot/" in route:
                body = body[:-40] + bytes([body[-40] ^ 1]) + body[-39:]
            return status, headers, body

        self.lan.tamper = flip
        self.assertFalse(self.puller.cycle())
        self.assertEqual(list(self.replica().glob("*.pdv2snap")) + list(self.replica().glob("*.part")), [])
        self.lan.tamper = None
        self.assertTrue(self.puller.cycle())
        self.assertTrue((self.replica() / remote["name"]).exists())

    def test_diario_cortado_no_meio_nao_duplica_nem_deixa_buraco(self):
        self.sync()
        sold = self.events(40)
        self.lan.cuts["journal"] = 0.5
        self.assertFalse(self.puller.cycle())
        partial = self.journal()
        self.assertTrue(0 < len(partial) < 40, len(partial))
        self.assertEqual([item["seq"] for item in partial], list(range(1, len(partial) + 1)))
        self.assertEqual(replica_net.puller_status(self.station_dir)["journal_seq"], len(partial))

        self.assertTrue(self.puller.cycle())
        records = self.journal()
        self.assertEqual([item["seq"] for item in records], list(range(1, 41)))
        self.assertEqual([item["event_id"] for item in records], sold)
        for path in self.replica().glob("journal-*.pdv2log"):
            self.assertTrue(path.read_bytes().endswith(b"\n"))
        # Uma passada a mais não traz nada de novo.
        self.assertTrue(self.puller.cycle())
        self.assertEqual(len(self.journal()), 40)


class LinhaRecusadaTests(ReplicaCase):
    def test_linha_adulterada_nao_e_gravada(self):
        self.sync()
        self.events(3)

        def flip(route, status, headers, body):
            if route.endswith("/journal") and body:
                lines = body.split(b"\n")
                lines[1] = lines[1][:-5] + (b"A" if lines[1][-5:-4] != b"A" else b"B") + lines[1][-4:]
                body = b"\n".join(lines)
            return status, headers, body

        self.lan.tamper = flip
        self.assertFalse(self.puller.cycle())
        self.assertEqual([item["seq"] for item in self.journal()], [1])  # só a que conferiu
        status = replica_net.puller_status(self.station_dir)
        self.assertEqual((status["state"], status["journal_seq"]), ("error", 1))
        self.assertIn("não conferem", status["last_error"])
        self.lan.tamper = None
        self.assertTrue(self.puller.cycle())
        self.assertEqual([item["seq"] for item in self.journal()], [1, 2, 3])
        self.assertEqual(replica_net.puller_status(self.station_dir)["state"], "ok")

    def test_linha_cifrada_com_outra_chave_ou_fora_de_ordem_e_recusada(self):
        self.sync()
        self.events(2)
        other = Fernet(Fernet.generate_key())

        def forge(route, status, headers, body):
            if route.endswith("/journal") and body:
                record = {"seq": 1, "event_id": "f" * 32, "created_at": 1,
                          "event": {"identity": {"terminal_id": TERMINAL}, "data": {}}}
                body = b"1\t" + b"f" * 32 + b"\t" + other.encrypt(json.dumps(record).encode()) + b"\n"
            return status, headers, body

        self.lan.tamper = forge
        self.assertFalse(self.puller.cycle())
        self.assertEqual(self.journal(), [])

        def replay(route, status, headers, body):
            if route.endswith("/journal") and body:
                first = body.split(b"\n")[0] + b"\n"
                body = first + first  # a mesma linha duas vezes: o seq não cresce
            return status, headers, body

        self.lan.tamper = replay
        self.assertFalse(self.puller.cycle())
        self.assertEqual([item["seq"] for item in self.journal()], [1])
        self.lan.tamper = None
        self.assertTrue(self.puller.cycle())
        self.assertEqual([item["seq"] for item in self.journal()], [1, 2])

    def test_chave_trocada_no_principal_e_buscada_de_novo(self):
        self.events(2)
        self.sync()
        self.assertEqual(self.lan.count("/replica/key"), 1)
        old_key, old_replica = self.key, self.replica()
        old_files = sorted(path.name for path in old_replica.iterdir())
        self.key = new_key("ffffffffffffffff")
        with self.store.transaction() as conn:
            Store._set_meta(conn, "mirror_key", json.dumps(self.key))
        self.event()
        info = self.server_status()
        self.assertEqual((info["key_id"], info["state"], info["snapshot"]), ("ffffffffffffffff", "preparing", None))
        status = self.sync()
        self.assertEqual(self.lan.count("/replica/key"), 2)
        self.assertEqual((status["state"], status["journal_seq"]), ("ok", 3))
        state = json.loads((self.puller.base / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["key"], self.key)
        # Os arquivos da chave antiga ficaram guardados, fora da geração nova.
        kept = list(self.puller.base.glob("anterior-*"))
        self.assertEqual(len(kept), 1)
        archived = kept[0] / mirror.ROOT_NAME / TERMINAL / self.store.identity()["device_id"]
        self.assertEqual(sorted(path.name for path in archived.iterdir()), old_files)
        self.assertEqual(len(mirror.read_pending(str(self.puller.base), self.key, self.new_identity())["events"]), 3)
        with self.assertRaises(mirror.MirrorError):
            mirror.read_pending(str(self.puller.base), old_key, self.new_identity())


class PrincipalInstavelTests(ReplicaCase):
    def test_principal_reiniciado_o_puxador_recua_e_volta(self):
        self.events(2)
        self.sync()
        downloads = self.lan.count("snapshot/")
        self.lan.down = True
        delays = []
        for _ in range(6):
            self.assertFalse(self.puller.cycle())
            delays.append(self.puller.next_delay())
        self.assertEqual(delays, [2.0, 4.0, 8.0, 16.0, 30.0, 30.0])
        self.assertEqual(replica_net.puller_status(self.station_dir)["last_error"],
                         "Sem contato com o caixa principal.")
        # Sem contato há mais de um minuto, a tela passa a dizer que a cópia está atrasada.
        late = replica_net._now_ms() + 2 * MINUTE
        with patch.object(replica_net, "_now_ms", return_value=late):
            self.assertEqual(replica_net.puller_status(self.station_dir)["state"], "late")

        replica_net.stop_all()  # o principal subiu de novo: nada do que estava em memória sobrou
        self.lan.down = False
        self.event()
        self.assertTrue(self.puller.cycle())
        self.assertEqual(self.puller.next_delay(), 2.0)
        self.assertEqual([item["seq"] for item in self.journal()], [3])
        self.assertEqual(self.lan.count("snapshot/"), downloads)  # a mesma cópia continua valendo
        self.assertEqual(replica_net.puller_status(self.station_dir)["state"], "ok")

    def test_principal_que_voltou_no_tempo_guarda_a_geracao_e_comeca_outra(self):
        self.events(3)
        self.sync()
        self.events(3)
        self.puller.cycle()
        old_files = sorted(path.name for path in self.replica().iterdir())
        with self.store.transaction() as conn:  # banco do principal restaurado de um backup antigo
            conn.execute("DELETE FROM outbox WHERE seq>2")
        status, _, body = self.call("GET", "/lan/v1/replica/journal", {"after": "6"})
        self.assertEqual((status, json.loads(body)["code"], json.loads(body)["outbox_seq"]), (409, "journal_reset", 2))

        self.sync()
        kept = list(self.puller.base.glob("anterior-*"))
        self.assertEqual(len(kept), 1)
        archived = kept[0] / mirror.ROOT_NAME / TERMINAL / self.store.identity()["device_id"]
        self.assertEqual(sorted(path.name for path in archived.iterdir()), old_files)
        manifest = json.loads((self.replica() / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual([item["outbox_seq"] for item in manifest["snapshots"]], [2])
        self.assertEqual(replica_net.puller_status(self.station_dir)["journal_seq"], 2)
        # A geração guardada não aparece para os leitores automáticos.
        self.assertEqual(len(mirror.candidates(self.new_identity(), str(self.puller.base))), 1)

    def test_journal_reset_no_meio_da_passada_tambem_arquiva(self):
        self.events(2)
        self.sync()

        def reset(route, status, headers, body):
            if route.endswith("/journal"):
                body = json.dumps({"error": "recomeçou", "code": "journal_reset", "outbox_seq": 0}).encode()
                return 409, {"Content-Length": str(len(body))}, body
            return status, headers, body

        self.lan.tamper = reset
        self.assertTrue(self.puller.cycle())  # recomeça já, sem contar como falha
        self.assertEqual(len(list(self.puller.base.glob("anterior-*"))), 1)
        self.assertEqual(replica_net.puller_status(self.station_dir)["state"], "empty")
        self.lan.tamper = None
        self.assertEqual(self.sync()["state"], "ok")

    def test_principal_com_outro_device_id_abre_outra_geracao(self):
        self.events(2)
        self.sync()
        old_device = self.store.identity()["device_id"]
        with self.store.transaction() as conn:
            Store._set_meta(conn, "device_id", "77777777-7777-4777-8777-777777777777")
        self.event()
        status = self.sync()
        self.assertEqual(status["state"], "ok")
        kept = list(self.puller.base.glob("anterior-*"))
        self.assertEqual(len(kept), 1)
        self.assertTrue((kept[0] / mirror.ROOT_NAME / TERMINAL / old_device / "manifest.json").exists())
        self.assertTrue((self.replica() / "manifest.json").exists())
        self.assertNotEqual(self.replica().name, old_device)


class PrincipalTrocadoPorPromocaoTests(ReplicaCase):
    """Outro caixa assumiu como principal: mesma identidade (terminal_id, device_id, chave), outra
    linha do tempo. O que este caixa recebeu do principal antigo não pode se misturar com o novo."""

    def promoted_store(self, sales, generation=None):
        """Banco de quem assumiu: cópia do principal como está agora, mais ``sales`` vendas próprias."""
        path = self.root / "promovido" / "terminal.sqlite3"
        path.parent.mkdir()
        with closing(sqlite3.connect(self.store.path)) as source, closing(sqlite3.connect(path)) as target:
            source.backup(target)
        store = Store(path)
        if generation is not None:
            with store.transaction() as conn:
                Store._set_meta(conn, "server_generation", str(generation))
        for _ in range(sales):
            self.event(store, note="venda do novo principal")
        return store

    def outbox_ids(self, store):
        with closing(sqlite3.connect(store.path)) as conn:
            return [row[0] for row in conn.execute("SELECT id FROM outbox ORDER BY seq")]

    def diverge(self, generation=None):
        """Este caixa tem 6,7,8 do principal antigo; o novo principal só tinha até 5 e vendeu 6'..9'."""
        self.events(5)
        self.sync()
        promoted = self.promoted_store(4, generation)
        old_tail = self.events(3)  # só este caixa recebeu
        self.assertTrue(self.puller.cycle())
        self.assertEqual([item["event_id"] for item in self.journal()][-3:], old_tail)
        self.assertEqual((mirror.max_seq(self.store), mirror.max_seq(promoted)), (8, 9))
        self.assertEqual(promoted.identity()["device_id"], self.store.identity()["device_id"])
        old_replica = self.replica()
        replica_net.stop_all()
        self.store = self.lan.store = promoted  # o caixa é religado ao novo principal
        self.lan.calls.clear()
        return promoted, old_tail, old_replica

    def check_separated(self, promoted, old_tail):
        kept = list(self.puller.base.glob("anterior-*"))
        self.assertEqual(len(kept), 1, "as vendas do principal antigo tinham de ficar guardadas")
        archived = kept[0] / mirror.ROOT_NAME / TERMINAL / promoted.identity()["device_id"]
        lines = b"".join(path.read_bytes() for path in sorted(archived.glob("journal-*.pdv2log"))).split(b"\n")
        self.assertEqual([mirror.parse_journal_line(line, self.key)["event_id"] for line in lines if line][-3:],
                         old_tail)
        # A cópia nova é só a linha do tempo do novo principal: nada do antigo, nada faltando.
        target = self.root / "conferencia.sqlite3"
        identity = dict(promoted.identity(), device_id=None)
        restored = mirror.restore_database(str(self.puller.base), self.key, identity, target,
                                           replica_id=promoted.identity()["device_id"])
        with closing(sqlite3.connect(target)) as conn:
            ids = [row[0] for row in conn.execute("SELECT id FROM outbox ORDER BY seq")]
        ids += [item["event_id"] for item in restored["events"]]
        self.assertEqual(ids, self.outbox_ids(promoted))
        self.assertFalse(set(ids) & set(old_tail))
        self.assertEqual(replica_net.status(promoted)["state"], "ok")

    def test_mesma_identidade_e_fila_divergente_guarda_a_geracao_antiga(self):
        promoted, old_tail, _ = self.diverge()
        self.assertEqual(self.sync()["state"], "ok")
        self.assertTrue(any("after=8&" in path and "after_id=" + old_tail[-1] in path
                            for _, path, _ in self.lan.calls))
        self.check_separated(promoted, old_tail)

    def test_geracao_diferente_arquiva_antes_de_pedir_o_diario(self):
        self.assertEqual(self.server_status()["generation"], 1)
        promoted, old_tail, _ = self.diverge(generation=2)
        self.assertEqual(json.loads((self.puller.base / "state.json").read_text(encoding="utf-8"))["generation"], 1)
        self.assertEqual(self.sync()["state"], "ok")
        self.assertFalse(any("after=8" in path for _, path, _ in self.lan.calls))
        self.assertEqual(json.loads((self.puller.base / "state.json").read_text(encoding="utf-8"))["generation"], 2)
        self.check_separated(promoted, old_tail)
        self.assertTrue(self.puller.cycle())  # mesma geração de novo: nada mais é arquivado
        self.assertEqual(len(list(self.puller.base.glob("anterior-*"))), 1)

    def test_rota_do_diario_confere_o_evento_da_posicao(self):
        ids = self.events(3)
        self.wait_snapshot()
        status, headers, body = self.call("GET", "/lan/v1/replica/journal", {"after": "2", "after_id": ids[1]})
        self.assertEqual((status, headers["X-PDV-Journal-Count"]), (200, "1"))
        status, _, body = self.call("GET", "/lan/v1/replica/journal", {"after": "2", "after_id": ids[0]})
        self.assertEqual((status, json.loads(body)["code"], json.loads(body)["outbox_seq"]), (409, "journal_reset", 3))
        status, _, _ = self.call("GET", "/lan/v1/replica/journal", {"after": "2", "after_id": "x y"})
        self.assertEqual(status, 400)
        status, _, _ = self.call("GET", "/lan/v1/replica/journal", {"after": "0", "after_id": ids[0]})
        self.assertEqual(status, 200)  # posição zero não tem evento para conferir


class SemChaveOuRestaurandoTests(ReplicaCase):
    def assert_nothing_copied(self, store):
        out = store.path.parent / "replica-out"
        self.assertEqual(list(out.glob("*")) if out.exists() else [], [])
        self.assertEqual([thread for thread in threading.enumerate() if thread.name == "pdv-replica-out"], [])

    def test_principal_sem_chave_fica_em_no_key(self):
        store = self.make_store(self.root / "sem-chave" / "terminal.sqlite3")
        self.event(store)
        lan = Lan(store)
        puller = replica_net.Puller(self.root / "caixa-3", lan.client)
        status, _, body = replica_net.handle_request(store, lan.station, "GET", "/lan/v1/replica/status", {}, {})
        info = json.loads(body)
        self.assertEqual((status, info["state"], info["snapshot"], info["key_id"]), (200, "no_key", None, None))
        status, headers, body = replica_net.handle_request(store, lan.station, "GET", "/lan/v1/replica/key", {}, {})
        self.assertEqual((status, json.loads(body)["code"]), (409, "no_key"))
        self.assertIn("Conecte o caixa principal à internet", json.loads(body)["error"])
        status, _, body = replica_net.handle_request(store, lan.station, "GET", "/lan/v1/replica/journal", {}, {})
        self.assertEqual((status, json.loads(body)["code"]), (409, "no_key"))
        self.assertTrue(puller.cycle())
        result = replica_net.puller_status(self.root / "caixa-3")
        self.assertEqual(result["state"], "no_key")
        self.assert_nothing_copied(store)
        # A chave chega da nuvem: a cópia começa sozinha.
        with store.transaction() as conn:
            Store._set_meta(conn, "mirror_key", json.dumps(self.key))
        self.store, self.lan = store, lan
        self.assertEqual(self.sync(puller)["state"], "ok")

    def test_principal_em_restauracao_nao_gera_copia(self):
        with self.store.transaction() as conn:
            Store._set_meta(conn, "restore_state", "restoring")
        self.assertEqual(self.server_status()["state"], "restoring")
        status, _, body = self.call("GET", "/lan/v1/replica/journal")
        self.assertEqual((status, json.loads(body)["code"]), (409, "restoring"))
        self.assertTrue(self.puller.cycle())
        self.assertEqual(replica_net.puller_status(self.station_dir)["state"], "empty")
        self.assert_nothing_copied(self.store)
        with self.store.transaction() as conn:
            Store._set_meta(conn, "restore_state", "done")
        self.assertEqual(self.sync()["state"], "ok")


class RotasTests(ReplicaCase):
    def test_estado_traz_identidade_e_copia(self):
        self.events(2)
        snapshot = self.wait_snapshot()
        info = self.server_status()
        self.assertEqual(set(info), {"v", "state", "terminal_id", "device_id", "tenant_id", "shop_id", "tenant_name",
                                     "shop_name", "terminal_name", "key_id", "app_version", "generation", "outbox_seq",
                                     "snapshot", "server_time_ms"})
        self.assertEqual((info["v"], info["state"], info["terminal_id"], info["shop_name"], info["outbox_seq"]),
                         (1, "ok", TERMINAL, "Matriz", 2))
        self.assertEqual(set(snapshot), {"name", "created_at", "outbox_seq", "size_bytes", "sha256"})
        self.assertNotIn(self.key["key"], json.dumps(info))

    def test_chave_so_sai_pela_rota_propria_e_sem_cache(self):
        status, headers, body = self.call("GET", "/lan/v1/replica/key")
        self.assertEqual((status, json.loads(body), headers["Cache-Control"]), (200, self.key, "no-store"))
        self.assertEqual(self.call("POST", "/lan/v1/replica/key")[0], 405)

    def test_copia_aceita_range_e_recusa_nome_estranho(self):
        self.event()
        snapshot = self.wait_snapshot()
        path = "/lan/v1/replica/snapshot/" + snapshot["name"]
        status, headers, body = self.call("GET", path)
        whole = b"".join(body)
        self.assertEqual((status, headers["Content-Length"], headers["Accept-Ranges"], headers["Content-Type"]),
                         (200, str(snapshot["size_bytes"]), "bytes", "application/octet-stream"))
        self.assertEqual(len(whole), snapshot["size_bytes"])
        status, headers, body = self.call("GET", path, headers={"Range": "bytes=100-"})
        self.assertEqual((status, headers["Content-Length"], headers["Content-Range"]),
                         (206, str(snapshot["size_bytes"] - 100),
                          f"bytes 100-{snapshot['size_bytes'] - 1}/{snapshot['size_bytes']}"))
        self.assertEqual(b"".join(body), whole[100:])
        self.assertEqual(self.call("GET", path, headers={"range": f"bytes={snapshot['size_bytes']}-"})[0], 416)
        self.assertEqual(self.call("GET", path, headers={"Range": "bytes=0-10"})[0], 416)
        for name in ("snapshot-1.pdv2snap", "../terminal.sqlite3", "index.json", ""):
            status, _, body = self.call("GET", "/lan/v1/replica/snapshot/" + name)
            self.assertEqual((status, json.loads(body)["code"]), (404, "snapshot_gone"), name)

    def test_diario_respeita_after_limit_e_espera_longa(self):
        sold = self.events(5)
        status, headers, body = self.call("GET", "/lan/v1/replica/journal", {"after": "1", "limit": "2"})
        lines = body.split(b"\n")[:-1]
        self.assertEqual((status, headers["X-PDV-Journal-Last"], headers["X-PDV-Journal-Count"]), (200, "5", "2"))
        self.assertEqual([mirror.parse_journal_line(line, self.key)["event_id"] for line in lines], sold[1:3])
        self.assertEqual([int(line.split(b"\t")[0]) for line in lines], [2, 3])
        for query in ({"after": "x"}, {"wait": "26"}, {"limit": "0"}, {"limit": "501"}, {"after": "-1"}):
            self.assertEqual(self.call("GET", "/lan/v1/replica/journal", query)[0], 400, query)
        # Espera longa: sem evento responde vazio no fim do prazo; com evento, responde na hora.
        started = time.monotonic()
        status, headers, body = self.call("GET", "/lan/v1/replica/journal", {"after": "5", "wait": "1"})
        self.assertEqual((status, body, headers["X-PDV-Journal-Count"]), (200, b"", "0"))
        self.assertGreaterEqual(time.monotonic() - started, 0.9)
        answer = {}
        waiter = threading.Thread(target=lambda: answer.update(
            reply=self.call("GET", "/lan/v1/replica/journal?after=5&wait=20")))
        started = time.monotonic()
        waiter.start()
        time.sleep(0.3)
        latest = self.event()
        waiter.join(10)
        self.assertLess(time.monotonic() - started, 5.0)
        line = answer["reply"][2].split(b"\n")[0]
        self.assertEqual(mirror.parse_journal_line(line, self.key)["event_id"], latest)

    def test_ack_pela_query_ou_pelo_corpo_e_progresso_por_caixa(self):
        self.events(4)
        self.assertEqual(replica_net.status(self.store),
                         {"supported": True, "state": "off", "stations": {},
                          "message": "Nenhum caixa adicional está guardando a cópia de segurança"})
        status, _, body = self.call("POST", "/lan/v1/replica/ack", {"journal_seq": "3", "snapshot_outbox_seq": "2"})
        self.assertEqual((status, json.loads(body)), (200, {"ok": True}))
        other = {"id": "caixa-3", "name": "Caixa 3"}
        body = json.dumps({"journal_seq": 4, "snapshot_outbox_seq": None}).encode()
        self.assertEqual(self.call("POST", "/lan/v1/replica/ack", body=body, station=other)[0], 200)
        self.assertEqual(self.call("POST", "/lan/v1/replica/ack")[0], 400)
        self.assertEqual(self.call("GET", "/lan/v1/replica/ack")[0], 405)
        result = replica_net.status(self.store)
        self.assertEqual(result["state"], "ok")
        self.assertEqual({key: (item["journal_seq"], item["behind"]) for key, item in result["stations"].items()},
                         {"caixa-2": (3, 1), "caixa-3": (4, 0)})
        self.assertEqual(self.call("GET", "/lan/v1/replica/nada")[0], 404)
        self.assertEqual(self.call("GET", "/lan/v1/outra")[0], 404)


class CadenciaTests(ReplicaCase):
    def at(self, moment):
        return patch.object(replica_net, "_now_ms", return_value=moment)

    def names(self):
        return sorted(path.name for path in (self.store.path.parent / "replica-out").glob("snapshot-*"))

    def settle(self):
        server = replica_net._server_for(self.store)
        self.wait(lambda: not server.building)

    def test_quinze_minutos_com_movimento_um_dia_sem_e_fechamento_de_caixa(self):
        self.event()
        first = self.wait_snapshot()
        start = first["created_at"]
        self.event()
        with self.at(start + 10 * MINUTE):
            self.server_status()
            self.settle()
        self.assertEqual(self.names(), [first["name"]])  # cedo demais
        with self.at(start + 16 * MINUTE):
            self.server_status()
            self.settle()
        self.assertEqual(len(self.names()), 2)
        second = self.server_status()["snapshot"]
        self.assertEqual(second["outbox_seq"], 2)
        with self.at(second["created_at"] + 60 * MINUTE):  # sem movimento: nada novo
            self.server_status()
            self.settle()
        self.assertEqual(len(self.names()), 2)
        with self.at(second["created_at"] + 24 * 60 * MINUTE):
            self.server_status()
            self.settle()
        names = self.names()
        self.assertEqual(len(names), 2)  # só as duas mais novas ficam
        self.assertNotIn(first["name"], names)
        # Fechamento de caixa: a próxima consulta já pede cópia nova.
        self.event()
        mirror.on_cash_closed(self.store)
        self.server_status()
        self.settle()
        self.assertEqual(self.server_status()["snapshot"]["outbox_seq"], 3)
        self.assertEqual(len(self.names()), 2)
        index = json.loads((self.store.path.parent / "replica-out" / "index.json").read_text(encoding="utf-8"))
        self.assertEqual([item["name"] for item in index["entries"]], self.names())
        self.assertNotIn(self.key["key"], json.dumps(index))

    def test_falha_ao_copiar_nao_derruba_nada_e_tenta_de_novo_depois(self):
        self.event()
        with patch.object(mirror, "make_snapshot", side_effect=OSError("disco cheio")):
            self.assertEqual(self.server_status()["state"], "preparing")
            self.settle()
            self.assertEqual(self.server_status()["state"], "preparing")
        self.event()  # a venda continua
        server = replica_net._server_for(self.store)
        self.assertGreater(server.retry_at, 0)
        later = replica_net._now_ms() + 2 * MINUTE
        with self.at(later):
            self.server_status()
        self.settle()
        self.assertEqual(self.server_status()["state"], "ok")


class TresCaixasTests(ReplicaCase):
    def test_tres_caixas_puxando_juntos_usam_a_mesma_copia(self):
        self.events(5)
        made = []
        real = mirror.make_snapshot

        def counting(*args, **kwargs):
            made.append(1)
            time.sleep(0.2)  # tempo para os três pedirem enquanto a cópia é feita
            return real(*args, **kwargs)

        lans = [Lan(self.store, f"caixa-{number}") for number in (2, 3, 4)]
        pullers = [replica_net.Puller(self.root / lan.station["id"], lan.client) for lan in lans]
        for puller in pullers:
            puller.data_dir.mkdir(exist_ok=True)
        errors = []

        def run(puller):
            try:
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    puller.cycle()
                    if replica_net.puller_status(puller.data_dir)["state"] == "ok":
                        return
                    time.sleep(0.02)
                errors.append("não ficou em dia")
            except Exception as exc:  # pragma: no cover
                errors.append(repr(exc))

        with patch.object(mirror, "make_snapshot", counting):
            threads = [threading.Thread(target=run, args=(puller,)) for puller in pullers]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(20)
        self.assertEqual(errors, [])
        self.assertEqual(len(made), 1)  # uma só geração da cópia para os três
        out = self.store.path.parent / "replica-out"
        self.assertEqual(len(list(out.glob("snapshot-*.pdv2snap"))), 1)
        digests = {hashlib.sha256(next(self.replica(puller).glob("snapshot-*.pdv2snap")).read_bytes()).hexdigest()
                   for puller in pullers}
        self.assertEqual(len(digests), 1)

        self.events(2)
        for puller in pullers[:2]:
            self.assertTrue(puller.cycle())
        result = replica_net.status(self.store)
        self.assertEqual((result["state"], result["message"]), ("ok", "Cópia de segurança nos outros caixas em dia"))
        self.assertEqual({key: (item["journal_seq"], item["behind"]) for key, item in result["stations"].items()},
                         {"caixa-2": (7, 0), "caixa-3": (7, 0), "caixa-4": (5, 2)})
        # Sem contato de nenhum caixa por mais de um minuto, o principal avisa que a cópia atrasou.
        with patch.object(replica_net, "_now_ms", return_value=replica_net._now_ms() + 2 * MINUTE):
            self.assertEqual(replica_net.status(self.store)["state"], "late")


class GanchosTests(ReplicaCase):
    def test_ganchos_nao_fazem_es_nem_levantam_com_a_replica_ligada(self):
        self.event()
        self.sync()

        def forbidden(*args, **kwargs):
            raise AssertionError("gancho fez E/S")

        def broken(kind, store):
            raise RuntimeError("ouvinte com defeito")

        mirror.add_listener(broken)
        self.addCleanup(mirror.remove_listener, broken)
        with patch("builtins.open", forbidden), patch.object(os, "open", forbidden), \
                patch.object(os, "stat", forbidden), patch.object(sqlite3, "connect", forbidden), \
                patch.object(mirror, "make_snapshot", forbidden), patch.object(threading.Thread, "start", forbidden):
            mirror.on_event(self.store)
            mirror.on_cash_closed(self.store)
            mirror.on_event(None)
            mirror.on_cash_closed(object())
        self.assertTrue(replica_net._server_for(self.store).cash_closed)
        # Uma venda de verdade com o ouvinte quebrado continua entrando na fila.
        sold = self.event()
        self.assertTrue(self.puller.cycle())
        self.assertEqual(self.journal()[-1]["event_id"], sold)

    def test_erro_inesperado_na_rota_vira_resposta_e_nao_vaza_conteudo(self):
        with patch.object(self.store, "meta", side_effect=RuntimeError(MARKER)):
            status, _, body = self.call("GET", "/lan/v1/replica/status")
        self.assertEqual((status, json.loads(body)["code"]), (500, "replica_error"))
        self.assertNotIn(MARKER, body.decode())


if __name__ == "__main__":
    unittest.main()
