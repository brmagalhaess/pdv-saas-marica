"""Réplica em segundo disco: cópia verificada do banco, diário cifrado e leitura para recuperação."""

import errno
import json
import os
import shutil
import sqlite3
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from cryptography.fernet import Fernet

from terminal import mirror
from terminal.store import Store, TerminalError, _json

TENANT = "11111111-1111-4111-8111-111111111111"
SHOP = "22222222-2222-4222-8222-222222222222"
TERMINAL = "33333333-3333-4333-8333-333333333333"
NEW_DEVICE = "99999999-9999-4999-8999-999999999999"
MARKER = "segredo-do-balcao"
TOKEN = "token-secreto-do-terminal"


def new_key(key_id="a1b2c3d4e5f60718"):
    return {"key_id": key_id, "key": Fernet.generate_key().decode()}


class MirrorCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.disk = root / "pendrive"
        self.disk.mkdir()
        self.key = new_key()
        self.store = self.make_store(root / "caixa" / "terminal.sqlite3", self.key)
        for registry in (mirror._SNAPSHOT_REQUESTS, mirror._SNAPSHOT_RETRY_AT, mirror._NEXT_DUE,
                         mirror._JOURNAL_CACHE):
            registry.clear()

    def make_store(self, path, key=None, tenant=TENANT, terminal=TERMINAL):
        store = Store(path)
        values = {"tenant_id": tenant, "shop_id": SHOP, "terminal_id": terminal, "token": TOKEN,
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

    def enable(self, store=None):
        result = mirror.configure(store or self.store, str(self.disk), allow_same_volume=True)
        self.assertEqual(result["state"], "ok", result)
        return result

    def replica(self, store=None):
        identity = (store or self.store).identity()
        return self.disk / mirror.ROOT_NAME / identity["terminal_id"] / identity["device_id"]

    def manifest(self, store=None):
        return json.loads((self.replica(store) / "manifest.json").read_text(encoding="utf-8"))

    def new_identity(self):
        return {"tenant_id": TENANT, "shop_id": SHOP, "terminal_id": TERMINAL, "device_id": NEW_DEVICE}

    def read(self, **kwargs):
        return mirror.read_pending(str(self.disk), self.key, self.new_identity(), **kwargs)

    def payloads(self, store=None):
        with (store or self.store).connection() as conn:
            return {row["id"]: row["payload"] for row in conn.execute("SELECT id,payload FROM outbox")}


class CopiaEDiarioTests(MirrorCase):
    def test_copia_e_diario_vao_e_voltam_com_a_chave(self):
        sent, kept = self.event(), self.event()
        self.store.ack_events([sent])
        self.enable()
        later = [self.event(), self.event()]
        status = mirror.tick(self.store)

        self.assertEqual(status["state"], "ok")
        self.assertEqual(status["message"], "Cópia de segurança em dia")
        self.assertEqual(status["journal_seq"], 4)
        self.assertEqual(status["pending_events"], 0)
        self.assertEqual(status["key_id"], self.key["key_id"])
        manifest = self.manifest()
        self.assertEqual(manifest["format"], "pdv2-mirror")
        self.assertEqual(manifest["key_id"], self.key["key_id"])
        self.assertEqual(manifest["snapshots"][0]["outbox_seq"], 2)
        self.assertEqual(manifest["journal_segments"], [{"name": "journal-000000000003.pdv2log", "first_seq": 3}])

        data = self.read()
        self.assertEqual(data["terminal_id"], TERMINAL)
        self.assertEqual(data["replica_id"], self.store.identity()["device_id"])
        self.assertEqual(data["snapshot"]["outbox_seq"], 2)
        self.assertEqual(data["journal_last_seq"], 4)
        self.assertEqual([item["event_id"] for item in data["events"]], [kept, *later])
        self.assertEqual([item["seq"] for item in data["events"]], [2, 3, 4])
        self.assertEqual(data["warnings"], [])
        original = self.payloads()
        for item in data["events"]:
            # O objeto volta idêntico: a nuvem compara o hash dele ao deduplicar.
            self.assertEqual(_json(item["event"]), original[item["event_id"]])
            self.assertEqual(item["event"]["event_id"], item["event_id"])

    def test_arquivos_no_disco_sao_ilegiveis_sem_a_chave(self):
        self.enable()
        self.event()
        mirror.tick(self.store)
        files = [path for path in self.replica().iterdir() if path.name != "manifest.json"]
        self.assertTrue(any(path.suffix == ".pdv2snap" for path in files))
        self.assertTrue(any(path.suffix == ".pdv2log" and path.stat().st_size for path in files))
        for path in self.replica().iterdir():
            content = path.read_bytes()
            self.assertNotIn(b"SQLite format 3", content)
            self.assertNotIn(MARKER.encode(), content)
            self.assertNotIn(self.key["key"].encode(), content)
            self.assertNotIn(TOKEN.encode(), content)
        state = (self.store.path.parent / "mirror.json").read_text(encoding="utf-8")
        self.assertNotIn(self.key["key"], state)

    def test_copia_do_banco_e_integra_e_nao_leva_token_nem_chave(self):
        self.event()
        self.enable()
        entry = self.manifest()["snapshots"][-1]
        plain = Path(self.tmp.name) / "aberta.sqlite3"
        with open(plain, "wb") as output:
            header, trailer = mirror._decrypt_snapshot(self.replica() / entry["name"],
                                                       Fernet(self.key["key"].encode()), output)
        self.assertEqual(header["chunk_bytes"], 4194304)
        self.assertEqual(header["key_id"], self.key["key_id"])
        self.assertEqual(trailer["sha256"], entry["sha256"])
        self.assertEqual(trailer["size"], plain.stat().st_size)
        self.assertNotIn(TOKEN.encode(), plain.read_bytes())
        self.assertNotIn(self.key["key"].encode(), plain.read_bytes())
        with closing(sqlite3.connect(plain)) as conn:
            self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            keys = {row[0] for row in conn.execute("SELECT key FROM meta")}
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0], 1)
        self.assertIn("terminal_id", keys)
        self.assertNotIn("token", keys)
        self.assertNotIn("mirror_key", keys)
        # O banco de verdade continua com o token.
        self.assertEqual(self.store.meta()["token"], TOKEN)
        self.assertEqual(list(self.store.path.parent.glob(".mirror-*")), [])

    def test_estado_local_e_gravado_com_permissao_restrita(self):
        self.enable()
        path = self.store.path.parent / "mirror.json"
        state = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(set(state), {"v", "directory", "enabled", "last_snapshot_at", "last_journal_at",
                                      "journal_seq", "last_error", "last_error_at", "device_id"})
        if os.name == "posix":
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(list(path.parent.glob("*.tmp")), [])

    def test_rotacao_mantem_tres_copias_e_apaga_diario_coberto(self):
        self.enable()
        names = []
        for _ in range(4):
            self.event()
            names.append(mirror.snapshot_now(self.store)["name"])
        manifest = self.manifest()
        self.assertEqual([item["name"] for item in manifest["snapshots"]], names[-3:])
        on_disk = sorted(path.name for path in self.replica().glob("snapshot-*"))
        self.assertEqual(on_disk, sorted(names[-3:]))
        oldest = manifest["snapshots"][0]["outbox_seq"]
        segments = sorted(path.name for path in self.replica().glob("journal-*"))
        self.assertTrue(segments)
        self.assertEqual(segments, [item["name"] for item in manifest["journal_segments"]
                                    if (self.replica() / item["name"]).exists()])
        self.assertTrue(all(int(name[8:20]) > oldest for name in segments), segments)
        self.assertEqual(list(self.replica().glob("*.tmp")), [])

    def test_cadencia_quinze_minutos_com_movimento_e_um_dia_sem(self):
        self.enable()
        start = self.manifest()["snapshots"][-1]["created_at"]
        minute = 60 * 1000
        self.event()
        mirror.tick(self.store, now=start + 10 * minute)
        self.assertEqual(len(self.manifest()["snapshots"]), 1)
        self.assertEqual(mirror.status(self.store)["journal_seq"], 1)
        mirror.tick(self.store, now=start + 16 * minute)
        self.assertEqual(len(self.manifest()["snapshots"]), 2)
        mirror.tick(self.store, now=start + 40 * minute)  # sem movimento: nada novo
        self.assertEqual(len(self.manifest()["snapshots"]), 2)
        mirror.tick(self.store, now=start + 16 * minute + 24 * 60 * minute)
        self.assertEqual(len(self.manifest()["snapshots"]), 3)

    def test_fechamento_do_caixa_pede_copia_imediata(self):
        self.enable()
        self.event()
        mirror.on_cash_closed(self.store)
        mirror.tick(self.store)
        manifest = self.manifest()
        self.assertEqual(len(manifest["snapshots"]), 2)
        self.assertEqual(manifest["snapshots"][-1]["outbox_seq"], 1)
        mirror.tick(self.store)  # o pedido foi atendido uma vez só
        self.assertEqual(len(self.manifest()["snapshots"]), 2)

    def test_snapshot_now_devolve_o_resumo(self):
        self.enable()
        self.event()
        result = mirror.snapshot_now(self.store)
        self.assertEqual(set(result), {"name", "created_at", "size_bytes", "outbox_seq"})
        self.assertEqual(result["outbox_seq"], 1)
        self.assertEqual(result["size_bytes"], (self.replica() / result["name"]).stat().st_size)

    def test_troca_de_chave_abre_geracao_nova(self):
        self.event()
        self.enable()
        old_files = {path.name for path in self.replica().glob("snapshot-*")}
        old_key, self.key = self.key, new_key("ffffffffffffffff")
        with self.store.transaction() as conn:
            Store._set_meta(conn, "mirror_key", json.dumps(self.key))
        self.event()
        status = mirror.tick(self.store)
        self.assertEqual(status["state"], "ok")
        self.assertEqual(status["key_id"], "ffffffffffffffff")
        manifest = self.manifest()
        self.assertEqual(manifest["key_id"], "ffffffffffffffff")
        self.assertEqual(len(manifest["snapshots"]), 1)
        self.assertFalse(old_files & {path.name for path in self.replica().iterdir()})
        self.assertEqual(len(self.read()["events"]), 2)
        with self.assertRaises(mirror.MirrorError):
            mirror.read_pending(str(self.disk), old_key, self.new_identity())


class ChaveTests(MirrorCase):
    def test_chave_errada_falha(self):
        self.event()
        self.enable()
        self.event()
        mirror.tick(self.store)
        for wrong in (new_key("0000000000000000"), new_key(self.key["key_id"])):
            with self.assertRaises(mirror.MirrorError) as raised:
                mirror.read_pending(str(self.disk), wrong, self.new_identity())
            self.assertEqual(raised.exception.status, 409)
            self.assertEqual(str(raised.exception), "Esta cópia de segurança foi feita com outra chave da empresa.")
            self.assertIsInstance(raised.exception, TerminalError)
            self.assertNotIn(wrong["key"], str(raised.exception))

    def test_sem_chave_valida_nao_le(self):
        self.enable()
        for bad in (None, {}, {"key_id": "abc", "key": "curta"}, "texto"):
            with self.assertRaises(mirror.MirrorError) as raised:
                mirror.read_pending(str(self.disk), bad, self.new_identity())
            self.assertEqual(raised.exception.status, 409)

    def test_sem_chave_no_terminal_fica_aguardando_e_nada_e_gravado(self):
        store = self.make_store(Path(self.tmp.name) / "semchave" / "terminal.sqlite3", key=None)
        self.event(store)
        status = mirror.configure(store, str(self.disk), allow_same_volume=True)
        self.assertEqual(status["state"], "no_key")
        self.assertTrue(status["enabled"])
        self.assertIsNone(status["key_id"])
        self.assertEqual(status["message"],
                         "Conecte este caixa à internet uma vez para liberar a cópia de segurança")
        self.assertFalse(self.replica(store).exists())
        with self.assertRaises(mirror.MirrorError):
            mirror.snapshot_now(store)
        with store.transaction() as conn:
            Store._set_meta(conn, "mirror_key", json.dumps(self.key))
        self.assertEqual(mirror.tick(store)["state"], "ok")
        self.assertEqual(len(self.manifest(store)["snapshots"]), 1)


class DiscoTests(MirrorCase):
    def test_pasta_do_mesmo_volume_e_recusada(self):
        with self.assertRaises(mirror.MirrorError) as raised:
            mirror.configure(self.store, str(self.disk))
        self.assertEqual(raised.exception.status, 409)
        self.assertEqual(str(raised.exception),
                         "Escolha outro disco ou um pendrive: esta pasta fica no mesmo disco do caixa.")
        self.assertFalse(mirror.is_enabled(self.store))
        self.assertEqual(mirror.status(self.store)["state"], "off")

    def test_pasta_inexistente_ou_relativa_e_recusada(self):
        with self.assertRaises(mirror.MirrorError) as raised:
            mirror.configure(self.store, str(self.disk / "nao-existe"), allow_same_volume=True)
        self.assertEqual(raised.exception.status, 404)
        for bad in ("", "pasta/relativa", 12):
            with self.assertRaises(mirror.MirrorError):
                mirror.configure(self.store, bad, allow_same_volume=True)
        self.assertFalse(mirror.is_enabled(self.store))

    def test_desligar(self):
        self.enable()
        self.assertTrue(mirror.is_enabled(self.store))
        status = mirror.configure(self.store, None)
        self.assertEqual((status["state"], status["enabled"], status["directory"]), ("off", False, None))
        self.assertFalse(mirror.is_enabled(self.store))
        before = len(self.manifest()["snapshots"])
        self.event()
        mirror.tick(self.store, force_snapshot=True)
        self.assertEqual(len(self.manifest()["snapshots"]), before)
        with self.assertRaises(mirror.MirrorError):
            mirror.snapshot_now(self.store)

    def test_disco_removido_nao_derruba_a_venda_e_volta_sozinho(self):
        self.enable()
        self.event()
        mirror.tick(self.store)
        hidden = self.disk.with_name("pendrive-fora")
        self.disk.rename(hidden)

        sold = self.event()  # a venda continua entrando na fila
        mirror.on_cash_closed(self.store)
        status = mirror.tick(self.store)
        self.assertEqual(status["state"], "error")
        self.assertIn("não foi encontrado", status["last_error"])
        self.assertEqual(status["pending_events"], 1)
        self.assertTrue(status["enabled"])
        self.assertFalse(self.disk.exists())  # não recria a pasta em um disco que não está lá
        with self.assertRaises(mirror.MirrorError):
            mirror.snapshot_now(self.store)

        hidden.rename(self.disk)
        status = mirror.tick(self.store)
        self.assertEqual(status["state"], "ok")
        self.assertEqual(status["last_error"], "")
        self.assertEqual(status["pending_events"], 0)
        self.assertIn(sold, [item["event_id"] for item in self.read()["events"]])

    def test_disco_trocado_por_um_vazio_recomeca_com_copia_completa(self):
        self.enable()
        first = self.event()
        mirror.tick(self.store)
        shutil.rmtree(self.disk)
        self.disk.mkdir()
        second = self.event()
        status = mirror.tick(self.store)
        self.assertEqual(status["state"], "ok")
        self.assertEqual([item["event_id"] for item in self.read()["events"]], [first, second])

    def test_disco_cheio_vira_estado_de_erro_e_nao_deixa_resto(self):
        self.enable()
        self.event()
        with patch.object(mirror, "_write_snapshot", side_effect=OSError(errno.ENOSPC, "No space left")):
            status = mirror.tick(self.store, force_snapshot=True)
        self.assertEqual(status["state"], "error")
        self.assertIn("cheio", status["last_error"])
        self.assertEqual(status["message"],
                         "Não foi possível gravar a cópia de segurança. Confira se o disco ou pendrive está conectado.")
        self.assertEqual(list(self.store.path.parent.glob(".mirror-*")), [])
        self.assertEqual(len(self.manifest()["snapshots"]), 1)
        # O diário não depende da cópia do banco.
        self.assertEqual(status["journal_seq"], 1)
        self.assertEqual(len(self.read()["events"]), 1)
        # Nova tentativa pedida pelo gerente funciona e limpa o erro.
        mirror.snapshot_now(self.store)
        self.assertEqual(mirror.status(self.store)["state"], "ok")

    def test_falha_ao_gravar_o_diario_vira_estado_de_erro(self):
        self.enable()
        self.event()
        with patch.object(mirror, "_append_journal", side_effect=PermissionError(errno.EACCES, "negado")):
            status = mirror.tick(self.store)
        self.assertEqual(status["state"], "error")
        self.assertIn("permissão", status["last_error"])
        self.assertEqual(mirror.tick(self.store)["state"], "ok")

    def test_erro_inesperado_nao_vaza_conteudo(self):
        self.enable()
        self.event()
        with patch.object(mirror, "_outbox_after", side_effect=RuntimeError(self.key["key"])):
            status = mirror.tick(self.store)
        self.assertEqual(status["state"], "error")
        self.assertNotIn(self.key["key"], json.dumps(status))
        self.assertNotIn(self.key["key"], (self.store.path.parent / "mirror.json").read_text(encoding="utf-8"))

    def test_status_nao_toca_o_disco_de_destino(self):
        self.enable()
        shutil.rmtree(self.disk)
        with patch.object(mirror, "_load_manifest", side_effect=AssertionError("leu o destino")):
            status = mirror.status(self.store)
        self.assertEqual(status["state"], "ok")
        self.assertEqual(set(status), {"supported", "enabled", "directory", "state", "message", "last_snapshot_at",
                                       "last_journal_at", "journal_seq", "pending_events", "last_error", "key_id"})

    def test_atraso_aparece_quando_o_diario_fica_para_tras(self):
        self.enable()
        self.event()
        with self.store.transaction() as conn:
            conn.execute("UPDATE outbox SET created_at=created_at-120000")
        status = mirror.status(self.store)
        self.assertEqual((status["state"], status["message"], status["pending_events"]),
                         ("late", "Cópia de segurança atrasada", 1))

    def test_banco_restaurado_de_copia_antiga_guarda_a_geracao_anterior(self):
        self.enable()
        self.event()
        self.event()
        mirror.tick(self.store)
        with self.store.transaction() as conn:
            conn.execute("DELETE FROM outbox")  # equivale a restaurar um banco mais velho
        fresh = self.event()
        self.assertEqual(mirror.tick(self.store)["state"], "ok")
        archived = [path for path in self.replica().iterdir() if path.name.startswith("anterior-")]
        self.assertEqual(len(archived), 1)
        self.assertTrue(list(archived[0].glob("journal-*")))
        self.assertEqual([item["event_id"] for item in self.read()["events"]], [fresh])

    def test_drives_lista_unidades_sem_levantar(self):
        with patch.object(mirror, "_drive_roots",
                          return_value=[(str(self.disk), "E:", "pendrive", True), ("/nao/existe", "Z:", "rede", False)]):
            drives = mirror.drives(self.store)
        self.assertEqual(len(drives), 1)
        self.assertEqual(set(drives[0]), {"directory", "label", "free_bytes", "removable", "same_volume"})
        self.assertTrue(drives[0]["label"].startswith("E: · pendrive · "))
        self.assertTrue(drives[0]["label"].endswith(" livres"))
        self.assertIs(drives[0]["same_volume"], True)
        self.assertIsInstance(mirror.drives(self.store), list)


class LeituraTests(MirrorCase):
    def test_linha_truncada_no_fim_do_diario_e_ignorada(self):
        self.enable()
        ids = [self.event(), self.event()]
        mirror.tick(self.store)
        journal = self.replica() / self.manifest()["journal_segments"][-1]["name"]
        whole = journal.read_bytes()
        last_line = whole.rstrip(b"\n").split(b"\n")[-1]
        with open(journal, "ab") as handle:
            handle.write(b"3\t" + b"f" * 32 + b"\t" + last_line.split(b"\t")[2][:40])  # queda de energia no meio

        data = self.read()
        self.assertEqual([item["event_id"] for item in data["events"]], ids)
        self.assertEqual(data["warnings"], ["A última linha do diário estava incompleta e foi ignorada."])

        # O escritor continua depois da sobra, sem colar a linha nova nela.
        mirror._JOURNAL_CACHE.clear()
        third = self.event()
        self.assertEqual(mirror.tick(self.store)["state"], "ok")
        data = self.read()
        self.assertEqual([item["event_id"] for item in data["events"]], [*ids, third])
        self.assertEqual(data["warnings"], ["Uma linha do diário estava ilegível e foi ignorada."])

    def test_ultima_linha_cortada_pela_metade_perde_so_ela(self):
        self.enable()
        ids = [self.event(), self.event()]
        mirror.tick(self.store)
        journal = self.replica() / self.manifest()["journal_segments"][-1]["name"]
        whole = journal.read_bytes()
        journal.write_bytes(whole[:-25])
        data = self.read()
        self.assertEqual([item["event_id"] for item in data["events"]], ids[:1])
        self.assertEqual(len(data["warnings"]), 1)
        # Na próxima passada o próprio caixa regrava o que faltou.
        mirror._JOURNAL_CACHE.clear()
        mirror._NEXT_DUE.clear()
        self.event()
        mirror.tick(self.store)
        self.assertEqual([item["seq"] for item in self.read()["events"]], [1, 2, 3])

    def test_prefixo_adulterado_nao_passa(self):
        self.enable()
        ids = [self.event(), self.event()]
        mirror.tick(self.store)
        journal = self.replica() / self.manifest()["journal_segments"][-1]["name"]
        lines = journal.read_bytes().split(b"\n")
        lines[1] = b"7" + lines[1][1:]  # seq do prefixo não bate com o conteúdo cifrado
        journal.write_bytes(b"\n".join(lines))
        data = self.read()
        self.assertEqual([item["event_id"] for item in data["events"]], ids[:1])
        self.assertEqual(data["warnings"], ["Uma linha do diário estava ilegível e foi ignorada."])

    def test_copia_corrompida_cai_para_a_anterior(self):
        first = self.event()
        self.enable()
        second = self.event()
        newest = mirror.snapshot_now(self.store)
        third = self.event()
        mirror.tick(self.store)
        path = self.replica() / newest["name"]
        content = bytearray(path.read_bytes())
        middle = len(content) // 2
        content[middle:middle + 8] = b"XXXXXXXX"
        path.write_bytes(bytes(content))

        data = self.read()
        self.assertEqual(data["snapshot"]["name"], self.manifest()["snapshots"][0]["name"])
        self.assertEqual(data["snapshot"]["outbox_seq"], 1)
        self.assertEqual([item["event_id"] for item in data["events"]], [first, second, third])
        self.assertEqual(data["warnings"], ["A cópia mais recente do banco estava danificada; foi usada a anterior."])

    def test_copia_cortada_no_fim_e_tratada_como_danificada(self):
        first = self.event()
        self.enable()
        second = self.event()
        mirror.tick(self.store)
        path = self.replica() / self.manifest()["snapshots"][0]["name"]
        path.write_bytes(path.read_bytes()[:-60])
        data = self.read()
        self.assertIsNone(data["snapshot"])
        # Sem cópia inteira sobra o diário: o que só estava no banco copiado não volta.
        self.assertEqual([item["event_id"] for item in data["events"]], [second])
        self.assertNotIn(first, [item["event_id"] for item in data["events"]])
        self.assertEqual(data["warnings"],
                         ["Nenhuma cópia do banco pôde ser lida; foram usados só os registros do diário."])

    def test_devolve_nao_confirmados_mais_posteriores_sem_repeticao(self):
        acked, pending = self.event(), self.event()
        self.store.ack_events([acked])
        self.enable()
        after = [self.event(), self.event()]
        mirror.tick(self.store)
        self.store.ack_events([after[0]])  # confirmado depois da cópia: volta mesmo assim, a nuvem deduplica
        mirror.snapshot_now(self.store)
        last = self.event()
        mirror.tick(self.store)

        data = self.read()
        ids = [item["event_id"] for item in data["events"]]
        self.assertEqual(ids, [pending, after[1], last])
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual([item["seq"] for item in data["events"]], sorted(item["seq"] for item in data["events"]))
        self.assertEqual(self.read()["events"], data["events"])

    def test_importacao_preserva_event_id_e_nao_duplica(self):
        self.event()
        self.enable()
        self.event()
        self.event()
        mirror.tick(self.store)
        original = self.payloads()
        new_store = self.make_store(Path(self.tmp.name) / "novo" / "terminal.sqlite3", self.key)

        def import_events(events):
            # Mesma regra de store.import_outbox_from_mirror (frente B): id e objeto intactos.
            with new_store.transaction() as conn:
                for item in events:
                    conn.execute("""INSERT OR IGNORE INTO outbox(id,event_type,aggregate_id,payload,created_at,acked_at,seq)
                                    VALUES (?,?,?,?,?,NULL,(SELECT COALESCE(MAX(seq),0)+1 FROM outbox))""",
                                 (item["event_id"], item["event"]["type"], item["event"]["aggregate_id"],
                                  _json(item["event"]), item["created_at"]))

        identity = new_store.identity()
        for _ in range(2):
            import_events(mirror.read_pending(str(self.disk), self.key, identity)["events"])
        self.assertEqual(self.payloads(new_store), original)
        self.assertEqual([event["event_id"] for event in new_store.pending_events()],
                         [event["event_id"] for event in self.store.pending_events()])

    def test_escolhe_a_replica_do_computador_antigo_e_nao_a_propria(self):
        self.event()
        self.enable()
        own = self.store.identity()
        with self.assertRaises(mirror.MirrorError) as raised:
            mirror.read_pending(str(self.disk), self.key, own)
        self.assertEqual(raised.exception.status, 404)
        self.assertEqual(str(raised.exception),
                         "Nenhuma cópia de segurança deste caixa foi encontrada nesta pasta.")
        # Pedida pelo nome, a própria réplica pode ser lida.
        self.assertEqual(len(mirror.read_pending(str(self.disk), self.key, own, own["device_id"])["events"]), 1)
        # A pasta escolhida pode ser qualquer nível do layout.
        for folder in (self.disk / mirror.ROOT_NAME, self.replica().parent, self.replica()):
            self.assertEqual(len(mirror.read_pending(str(folder), self.key, self.new_identity())["events"]), 1)

    def test_pasta_vazia_outra_loja_e_outro_caixa(self):
        empty = Path(self.tmp.name) / "vazio"
        empty.mkdir()
        for folder in (empty, Path(self.tmp.name) / "sumiu"):
            with self.assertRaises(mirror.MirrorError) as raised:
                mirror.read_pending(str(folder), self.key, self.new_identity())
            self.assertEqual(raised.exception.status, 404)

        self.enable()
        other_tenant = dict(self.new_identity(), tenant_id="44444444-4444-4444-8444-444444444444")
        for replica_id in (None, self.store.identity()["device_id"]):
            with self.assertRaises(mirror.MirrorError) as raised:
                mirror.read_pending(str(self.disk), self.key, other_tenant, replica_id)
            self.assertEqual((raised.exception.status, str(raised.exception)), (409, "Esta cópia é de outra loja."))

        other_terminal = dict(self.new_identity(), terminal_id="55555555-5555-4555-8555-555555555555")
        with self.assertRaises(mirror.MirrorError) as raised:
            mirror.read_pending(str(self.disk), self.key, other_terminal)
        self.assertEqual(raised.exception.status, 404)
        with self.assertRaises(mirror.MirrorError) as raised:
            mirror.read_pending(str(self.disk), self.key, other_terminal, self.store.identity()["device_id"])
        self.assertEqual(raised.exception.status, 409)

    def test_candidatas_trazem_as_deste_caixa_primeiro(self):
        self.enable()
        other = self.make_store(Path(self.tmp.name) / "outro" / "terminal.sqlite3", self.key,
                                terminal="66666666-6666-4666-8666-666666666666")
        self.event(other)
        self.enable(other)

        found = mirror.candidates(self.new_identity(), str(self.disk))
        self.assertEqual(len(found), 2)
        self.assertEqual(set(found[0]), {"directory", "replica_id", "terminal_id", "terminal_name", "shop_name",
                                         "updated_at", "journal_seq", "last_snapshot_at", "matches",
                                         "current_device"})
        self.assertEqual([item["matches"] for item in found], [True, False])
        self.assertEqual(found[0]["replica_id"], self.store.identity()["device_id"])
        self.assertEqual(found[0]["terminal_name"], "Caixa 01")
        self.assertEqual(found[0]["shop_name"], "Matriz")
        self.assertEqual(found[0]["directory"], str(self.disk))
        self.assertFalse(found[0]["current_device"])
        self.assertIsInstance(found[0]["last_snapshot_at"], int)
        self.assertTrue(mirror.candidates(self.store.identity(), str(self.disk))[0]["current_device"])

        with patch.object(mirror, "_drive_roots", return_value=[(str(self.disk), "E:", "pendrive", True)]):
            self.assertEqual(len(mirror.candidates(self.new_identity())), 2)
        self.assertEqual(mirror.candidates(self.new_identity(), str(self.disk / "nada")), [])
        self.assertEqual(mirror.candidates(None, None if os.name == "nt" else "/nao/existe"), [])


class GanchosTests(MirrorCase):
    def test_ganchos_nao_fazem_es_nem_levantam(self):
        self.enable()

        def forbidden(*args, **kwargs):
            raise AssertionError("gancho fez E/S")

        with patch("builtins.open", forbidden), patch.object(os, "open", forbidden), \
                patch.object(os, "stat", forbidden), patch.object(sqlite3, "connect", forbidden), \
                patch.object(mirror, "tick", forbidden):
            mirror.on_event(self.store)
            mirror.on_cash_closed(self.store)
            mirror.on_event(None)
            mirror.on_cash_closed(object())

    def test_thread_grava_o_diario_ao_ser_acordada(self):
        self.enable()
        worker = mirror.start(self.store)
        self.addCleanup(worker.stop)
        self.assertIs(mirror.start(self.store), worker)
        self.assertEqual(worker._thread.name, "pdv-mirror")
        self.assertTrue(worker._thread.daemon)

        sold = self.event()
        mirror.on_event(self.store)
        self.wait(lambda: mirror.status(self.store)["journal_seq"] == 1)
        self.assertIn(sold, [item["event_id"] for item in self.read()["events"]])

        self.event()
        mirror.on_cash_closed(self.store)
        self.wait(lambda: len(self.manifest()["snapshots"]) == 2)
        self.assertEqual(self.manifest()["snapshots"][-1]["outbox_seq"], 2)

        worker.stop()
        self.assertFalse(worker.is_alive())
        again = mirror.start(self.store)
        self.addCleanup(again.stop)
        self.assertIsNot(again, worker)
        again.stop()
        self.assertFalse(again.is_alive())

    def test_thread_sobrevive_a_disco_ausente(self):
        self.enable()
        shutil.rmtree(self.disk)
        worker = mirror.start(self.store)
        self.addCleanup(worker.stop)
        self.event()
        mirror.on_event(self.store)
        self.wait(lambda: mirror.status(self.store)["state"] == "error")
        self.assertTrue(worker.is_alive())
        self.disk.mkdir()
        mirror.on_event(self.store)
        self.wait(lambda: mirror.status(self.store)["state"] == "ok")
        self.assertEqual(len(self.read()["events"]), 1)

    def wait(self, condition, seconds=8.0):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            try:
                if condition():
                    return
            except (OSError, ValueError):
                pass
            time.sleep(0.02)
        self.fail("a thread da réplica não reagiu a tempo")


class RelogioCorrigidoTests(MirrorCase):
    HOUR = 60 * 60 * 1000
    MINUTE = 60 * 1000

    def test_relogio_corrigido_para_tras_faz_uma_unica_copia_de_reancoragem(self):
        self.enable()
        start = self.manifest()["snapshots"][-1]["created_at"]
        self.event()
        mirror.tick(self.store, now=start + 3 * self.HOUR)  # computador com a hora adiantada
        self.assertEqual(len(self.manifest()["snapshots"]), 2)
        copies = []
        with patch.object(mirror, "_write_snapshot", side_effect=lambda *args: copies.append(1)
                          or self.real_write(*args)):
            for index in range(5):  # o Windows acertou a hora: cinco vendas em seguida
                self.event()
                status = mirror.tick(self.store, now=start + self.MINUTE + index)
                self.assertEqual(status["state"], "ok", status)
        self.assertEqual(len(copies), 1)
        now = start + self.MINUTE + 4
        snapshots = self.manifest()["snapshots"]
        self.assertEqual(len(snapshots), 3)
        stamps = [item["created_at"] for item in snapshots]
        self.assertEqual(stamps, sorted(set(stamps)))
        self.assertLessEqual(stamps[-1], now)
        self.assertLessEqual(mirror.status(self.store)["last_snapshot_at"], now)
        self.assertEqual(sorted(path.name for path in self.replica().glob("snapshot-*")),
                         sorted(item["name"] for item in snapshots))
        # A cadência normal volta a valer a partir da hora certa.
        mirror.tick(self.store, now=stamps[-1] + 10 * self.MINUTE)
        self.assertEqual(self.manifest()["snapshots"][-1]["created_at"], stamps[-1])
        mirror.tick(self.store, now=stamps[-1] + 16 * self.MINUTE)
        self.assertGreater(self.manifest()["snapshots"][-1]["created_at"], stamps[-1])
        # A leitura usa a cópia mais adiantada na fila, não a de carimbo mais alto.
        data = self.read()
        self.assertEqual(data["snapshot"]["outbox_seq"], 6)
        self.assertEqual([item["seq"] for item in data["events"]], [1, 2, 3, 4, 5, 6])

    def real_write(self, *args):
        return REAL_WRITE_SNAPSHOT(*args)

    def test_leitura_prefere_a_copia_mais_adiantada_mesmo_com_carimbo_antigo(self):
        self.enable()
        start = self.manifest()["snapshots"][-1]["created_at"]
        first = self.event()
        mirror.tick(self.store, now=start + 3 * self.HOUR)
        self.store.ack_events([first])
        second = self.event()
        mirror.tick(self.store, now=start + self.MINUTE)
        data = self.read()
        self.assertEqual(data["snapshot"]["outbox_seq"], 2)
        self.assertEqual([item["event_id"] for item in data["events"]], [second])

    def test_espera_de_nova_tentativa_nao_fica_presa_no_carimbo_futuro(self):
        self.enable()
        start = self.manifest()["snapshots"][-1]["created_at"]
        self.event()
        with patch.object(mirror, "_write_snapshot", side_effect=OSError(errno.EIO, "falha")):
            status = mirror.tick(self.store, now=start + 3 * self.HOUR)
        self.assertEqual(status["state"], "error")
        self.event()
        status = mirror.tick(self.store, now=start + 20 * self.MINUTE)  # hora acertada, disco bom
        self.assertEqual(status["state"], "ok", status)
        self.assertEqual(status["last_error"], "")
        self.assertEqual(len(self.manifest()["snapshots"]), 2)

    def test_proximo_vencimento_no_futuro_distante_nao_segura_a_copia_diaria(self):
        self.enable()
        start = self.manifest()["snapshots"][-1]["created_at"]
        mirror.tick(self.store, now=start + 30 * 24 * self.HOUR)  # data errada por um mês, sem venda
        self.assertEqual(len(self.manifest()["snapshots"]), 2)
        mirror.tick(self.store, now=start + self.MINUTE)
        mirror.tick(self.store, now=start + 2 * self.MINUTE)
        self.assertEqual(len(self.manifest()["snapshots"]), 3)
        newest = self.manifest()["snapshots"][-1]["created_at"]
        self.assertEqual(newest, start + self.MINUTE)
        mirror.tick(self.store, now=newest + 25 * self.HOUR)
        self.assertEqual(self.manifest()["snapshots"][-1]["created_at"], newest + 25 * self.HOUR)


class ManifestoPerdidoTests(MirrorCase):
    def prepare_replica(self):
        sent, kept = self.event(), self.event()
        self.store.ack_events([sent])
        self.enable()
        later = [self.event(), self.event()]
        mirror.tick(self.store)
        return [kept, *later]

    def assert_recovered(self, expected):
        data = self.read()
        self.assertEqual([item["event_id"] for item in data["events"]], expected)
        self.assertEqual(data["terminal_id"], TERMINAL)
        self.assertEqual(data["replica_id"], self.store.identity()["device_id"])
        return data

    def test_sem_manifesto_a_replica_e_lida_pelos_arquivos(self):
        expected = self.prepare_replica()
        (self.replica() / "manifest.json").unlink()
        data = self.assert_recovered(expected)
        self.assertEqual(data["snapshot"]["outbox_seq"], 2)
        self.assertIn(mirror.MSG_REBUILT, data["warnings"])
        found = mirror.candidates(self.new_identity(), str(self.disk))
        self.assertEqual(len(found), 1)
        self.assertTrue(found[0]["matches"])
        self.assertEqual(found[0]["replica_id"], self.store.identity()["device_id"])
        self.assertEqual(found[0]["terminal_id"], TERMINAL)
        self.assertEqual(found[0]["journal_seq"], 4)
        self.assertIsInstance(found[0]["last_snapshot_at"], int)
        self.assertEqual(self.read(replica_id=self.store.identity()["device_id"])["events"], data["events"])

    def test_manifesto_cortado_usa_o_arquivo_temporario_da_troca(self):
        expected = self.prepare_replica()
        path = self.replica() / "manifest.json"
        content = path.read_bytes()
        (self.replica() / "manifest.json.tmp").write_bytes(content)
        path.write_bytes(content[:len(content) // 2])  # troca interrompida em pendrive FAT
        data = self.assert_recovered(expected)
        self.assertIn(mirror.MSG_REBUILT, data["warnings"])
        found = mirror.candidates(self.new_identity(), str(self.disk))
        self.assertEqual([item["shop_name"] for item in found], ["Matriz"])
        self.assertTrue(found[0]["matches"])

    def test_manifesto_e_temporario_cortados_reconstroem_pelos_cabecalhos(self):
        expected = self.prepare_replica()
        path = self.replica() / "manifest.json"
        content = path.read_bytes()
        path.write_bytes(b"")
        (self.replica() / "manifest.json.tmp").write_bytes(content[:40])
        self.assert_recovered(expected)

    def test_so_o_diario_ainda_devolve_os_eventos_pelo_caminho_da_pasta(self):
        self.enable()
        events = [self.event(), self.event()]
        mirror.tick(self.store)
        (self.replica() / "manifest.json").unlink()
        for path in self.replica().glob("snapshot-*"):
            path.unlink()
        data = self.assert_recovered(events)
        self.assertIsNone(data["snapshot"])

    def test_replica_reconstruida_com_outra_chave_e_recusada(self):
        self.prepare_replica()
        (self.replica() / "manifest.json").unlink()
        with self.assertRaises(mirror.MirrorError) as raised:
            mirror.read_pending(str(self.disk), new_key("ffffffffffffffff"), self.new_identity())
        self.assertEqual(str(raised.exception), mirror.MSG_WRONG_KEY)

    def test_replica_reconstruida_de_outro_caixa_nao_e_usada(self):
        self.prepare_replica()
        (self.replica() / "manifest.json").unlink()
        other = dict(self.new_identity(), terminal_id="55555555-5555-4555-8555-555555555555")
        with self.assertRaises(mirror.MirrorError) as raised:
            mirror.read_pending(str(self.disk), self.key, other)
        self.assertEqual(raised.exception.status, 404)
        self.assertFalse(mirror.candidates(other, str(self.disk))[0]["matches"])

    def test_diario_nao_regrava_o_manifesto_a_cada_venda(self):
        self.enable()
        path = self.replica() / "manifest.json"
        before = path.read_bytes()
        with patch.object(mirror, "_save_manifest", side_effect=AssertionError("manifesto regravado")):
            for _ in range(3):
                self.event()
                self.assertEqual(mirror.tick(self.store)["state"], "ok")
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(mirror.status(self.store)["journal_seq"], 3)
        found = mirror.candidates(self.new_identity(), str(self.disk))
        self.assertEqual(found[0]["journal_seq"], 3)  # lido do próprio diário
        self.assertEqual(len(self.read()["events"]), 3)

    def test_caixa_antigo_refaz_o_manifesto_perdido_sem_perder_a_fila(self):
        expected = self.prepare_replica()
        (self.replica() / "manifest.json").unlink()
        expected.append(self.event())
        self.assertEqual(mirror.tick(self.store)["state"], "ok")
        self.assertEqual(self.manifest()["key_id"], self.key["key_id"])
        data = self.read()
        self.assertEqual([item["event_id"] for item in data["events"]], expected)
        self.assertEqual(data["warnings"], [])


class RestauracaoDoBancoTests(MirrorCase):
    """restore_database: o banco inteiro refeito da réplica (promoção de estação, fase 3)."""

    def restore(self, **kwargs):
        target = Path(self.tmp.name) / "promovido" / "terminal.sqlite3.promoting"
        target.parent.mkdir(exist_ok=True)
        return target, mirror.restore_database(str(self.disk), self.key, self.new_identity(), target, **kwargs)

    def test_banco_restaurado_e_integro_sem_token_nem_chave_e_so_traz_o_diario_posterior(self):
        before = [self.event(), self.event()]
        self.store.ack_events(before[:1])
        self.enable()
        after = [self.event(), self.event(), self.event()]
        mirror.tick(self.store)

        target, data = self.restore()
        self.assertEqual(set(data), {"snapshot", "events", "journal_last_seq", "app_version", "warnings"})
        self.assertEqual(set(data["snapshot"]), {"name", "created_at", "outbox_seq"})
        self.assertEqual((data["snapshot"]["outbox_seq"], data["journal_last_seq"], data["warnings"]), (2, 5, []))
        self.assertEqual(data["app_version"], self.manifest()["app_version"])
        # Só o que veio depois da cópia: o que já está no banco não volta como evento.
        self.assertEqual([(item["seq"], item["event_id"]) for item in data["events"]], list(zip((3, 4, 5), after)))
        original = self.payloads()
        for item in data["events"]:
            self.assertEqual(_json(item["event"]), original[item["event_id"]])
            self.assertEqual(set(item), {"seq", "event_id", "created_at", "event"})
        self.assertNotIn(TOKEN.encode(), target.read_bytes())
        self.assertNotIn(self.key["key"].encode(), target.read_bytes())
        with closing(sqlite3.connect(target)) as conn:
            self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            meta = dict(conn.execute("SELECT key,value FROM meta"))
            rows = conn.execute("SELECT id,acked_at IS NULL FROM outbox ORDER BY seq").fetchall()
        self.assertNotIn("token", meta)
        self.assertNotIn("mirror_key", meta)
        self.assertEqual((meta["terminal_id"], meta["device_id"]), (TERMINAL, self.store.identity()["device_id"]))
        self.assertEqual(rows, [(before[0], 0), (before[1], 1)])
        self.assertEqual(sorted(path.name for path in target.parent.iterdir()), [target.name])
        if os.name == "posix":
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)
        # O banco restaurado abre como um caixa de verdade.
        self.assertEqual(Store(target).identity()["terminal_id"], TERMINAL)

    def test_copia_mais_recente_danificada_cai_para_a_anterior_com_aviso(self):
        first = self.event()
        self.enable()
        second = self.event()
        newest = mirror.snapshot_now(self.store)
        third = self.event()
        mirror.tick(self.store)
        path = self.replica() / newest["name"]
        content = bytearray(path.read_bytes())
        middle = len(content) // 2
        content[middle:middle + 8] = b"XXXXXXXX"
        path.write_bytes(bytes(content))

        target, data = self.restore(replica_id=self.store.identity()["device_id"])
        self.assertEqual(data["snapshot"]["name"], self.manifest()["snapshots"][0]["name"])
        self.assertEqual(data["snapshot"]["outbox_seq"], 1)
        self.assertEqual([item["event_id"] for item in data["events"]], [second, third])
        self.assertEqual(data["warnings"], ["A cópia mais recente do banco estava danificada; foi usada a anterior."])
        with closing(sqlite3.connect(target)) as conn:
            self.assertEqual([row[0] for row in conn.execute("SELECT id FROM outbox")], [first])

    def test_sem_copia_legivel_recusa_e_nao_deixa_arquivo(self):
        self.event()
        self.enable()
        for path in self.replica().glob("snapshot-*"):
            path.write_bytes(path.read_bytes()[:200])
        target = Path(self.tmp.name) / "terminal.sqlite3.promoting"
        with self.assertRaises(mirror.MirrorError) as caught:
            mirror.restore_database(str(self.disk), self.key, self.new_identity(), target)
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(list(Path(self.tmp.name).glob("terminal.sqlite3.promoting*")), [])

    def test_chave_errada_outra_loja_e_pasta_vazia(self):
        self.event()
        self.enable()
        target = Path(self.tmp.name) / "x.sqlite3"
        with self.assertRaises(mirror.MirrorError) as caught:
            mirror.restore_database(str(self.disk), new_key("0000000000000000"), self.new_identity(), target)
        self.assertEqual((caught.exception.status, str(caught.exception)), (409, mirror.MSG_WRONG_KEY))
        with self.assertRaises(mirror.MirrorError) as caught:
            mirror.restore_database(str(self.disk), new_key(self.key["key_id"]), self.new_identity(), target)
        self.assertEqual(caught.exception.status, 409)  # mesmo key_id, outro segredo: nenhuma cópia abre
        with self.assertRaises(mirror.MirrorError) as caught:
            mirror.restore_database(str(self.disk), self.key, dict(self.new_identity(), shop_id="outra"), target)
        self.assertEqual(str(caught.exception), mirror.MSG_OTHER_SHOP)
        with self.assertRaises(mirror.MirrorError) as caught:
            mirror.restore_database(self.tmp.name + "/vazio", self.key, self.new_identity(), target)
        self.assertEqual(caught.exception.status, 404)
        with self.assertRaises(mirror.MirrorError):
            mirror.restore_database(str(self.disk), None, self.new_identity(), target)
        self.assertFalse(target.exists())

    def test_buraco_no_diario_vira_aviso(self):
        self.enable()
        self.event(), self.event(), self.event()
        mirror.tick(self.store)
        segment = next(self.replica().glob("journal-*"))
        lines = segment.read_bytes().split(b"\n")
        segment.write_bytes(b"\n".join([lines[0], lines[2], b""]))
        _, data = self.restore()
        self.assertEqual([item["seq"] for item in data["events"]], [1, 3])
        self.assertEqual(data["warnings"], [mirror.MSG_GAP])


class InterfaceDaFase3Tests(MirrorCase):
    def test_chave_copia_e_linhas_do_diario_pelas_funcoes_publicas(self):
        self.assertEqual(mirror.key_from_meta(self.store.meta()), self.key)
        self.assertIsNone(mirror.key_from_meta({}))
        self.assertIsNone(mirror.key_from_meta({"mirror_key": "isto não é json"}))
        self.assertIsNone(mirror.key_from_meta({"mirror_key": json.dumps({"key_id": "a", "key": "curta"})}))
        sold = [self.event(), self.event(), self.event()]
        out = Path(self.tmp.name) / "saida"
        entry = mirror.make_snapshot(self.store, out)
        self.assertEqual(set(entry), {"name", "created_at", "outbox_seq", "size_bytes", "sha256"})
        self.assertEqual(entry["outbox_seq"], 3)
        self.assertEqual(sorted(path.name for path in out.iterdir()), [entry["name"]])
        self.assertEqual(list(self.store.path.parent.glob(".replica-*")), [])
        check = mirror.verify_snapshot(out / entry["name"], self.key)
        self.assertEqual((check["sha256"], check["header"]["outbox_seq"], check["header"]["key_id"]),
                         (entry["sha256"], 3, self.key["key_id"]))
        self.assertIsNone(mirror.verify_snapshot(out / entry["name"], new_key()))
        self.assertIsNone(mirror.verify_snapshot(out / "nao-existe.pdv2snap", self.key))
        (out / "cortada.pdv2snap").write_bytes((out / entry["name"]).read_bytes()[:-30])
        self.assertIsNone(mirror.verify_snapshot(out / "cortada.pdv2snap", self.key))

        lines = mirror.journal_lines(self.store, 1)
        self.assertEqual(len(mirror.journal_lines(self.store, 0, limit=2)), 2)
        self.assertTrue(all(line.endswith(b"\n") and MARKER.encode() not in line for line in lines))
        records = [mirror.parse_journal_line(line, self.key) for line in lines]
        self.assertEqual([(item["seq"], item["event_id"]) for item in records], [(2, sold[1]), (3, sold[2])])
        self.assertIsNone(mirror.parse_journal_line(lines[0], new_key()))
        self.assertIsNone(mirror.parse_journal_line(lines[0].replace(b"2\t", b"9\t", 1), self.key))
        self.assertIsNone(mirror.parse_journal_line("texto", self.key))
        self.assertEqual(mirror.max_seq(self.store), 3)

    def test_copia_avulsa_nao_e_feita_sem_chave_nem_em_restauracao(self):
        plain = self.make_store(Path(self.tmp.name) / "sem-chave" / "terminal.sqlite3")
        out = Path(self.tmp.name) / "saida"
        with self.assertRaises(mirror.MirrorError):
            mirror.make_snapshot(plain, out)
        self.assertEqual(mirror.journal_lines(plain, 0), [])
        with self.store.transaction() as conn:
            Store._set_meta(conn, "restore_state", "restoring")
        with self.assertRaises(mirror.MirrorError):
            mirror.make_snapshot(self.store, out)
        self.assertFalse(out.exists() and list(out.iterdir()))

    def test_copia_avulsa_e_copia_do_segundo_disco_nao_apagam_a_sobra_uma_da_outra(self):
        parent = self.store.path.parent
        (parent / ".mirror-1-abc.tmp").write_bytes(b"x")
        (parent / ".replica-1-abc.tmp").write_bytes(b"x")
        mirror.make_snapshot(self.store, Path(self.tmp.name) / "saida")
        self.assertTrue((parent / ".mirror-1-abc.tmp").exists())
        self.assertFalse((parent / ".replica-1-abc.tmp").exists())

    def test_ouvintes_sao_chamados_pelos_ganchos_e_erro_deles_nao_sobe(self):
        seen = []

        def listener(kind, store):
            seen.append((kind, store))

        def broken(kind, store):
            raise RuntimeError("ouvinte com defeito")

        for callback in (broken, listener, listener):  # registrar duas vezes não duplica
            mirror.add_listener(callback)
            self.addCleanup(mirror.remove_listener, callback)

        def forbidden(*args, **kwargs):
            raise AssertionError("gancho fez E/S")

        with patch("builtins.open", forbidden), patch.object(os, "open", forbidden), \
                patch.object(os, "stat", forbidden), patch.object(sqlite3, "connect", forbidden):
            mirror.on_event(self.store)
            mirror.on_cash_closed(self.store)
        self.assertEqual(seen, [("event", self.store), ("cash_closed", self.store)])
        # Pela venda de verdade: o commit acontece e o ouvinte quebrado não derruba nada.
        sold = self.event()
        self.assertIn(sold, self.payloads())
        self.assertEqual(seen[-1], ("event", self.store))
        mirror.remove_listener(listener)
        self.event()
        self.assertEqual(len(seen), 3)


REAL_WRITE_SNAPSHOT = mirror._write_snapshot


if __name__ == "__main__":
    unittest.main()
