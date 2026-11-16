"""Réplica do caixa em um segundo disco (pendrive, HD externo ou pasta de rede).

Dois artefatos, ambos cifrados com a chave da cópia de segurança da empresa:

* cópia verificada do banco SQLite (a cada 15 min se houve movimento, a cada 24 h,
  em todo fechamento de caixa e a pedido), com as 3 últimas mantidas;
* diário com cada evento da fila (``outbox``), acrescentado em segundos.

Layout no destino::

    <pasta>/PDV2-copia-de-seguranca/<terminal_id>/<device_id>/
        manifest.json
        snapshot-<created_ms>.pdv2snap
        journal-<primeiro_seq com 12 dígitos>.pdv2log

Regras que este módulo nunca quebra:

* uma falha do disco de destino jamais derruba a venda: vira ``last_error`` e
  ``state="error"`` em ``status()``;
* o diário é alimentado pela tabela ``outbox`` em ordem de ``seq``, nunca por dado em
  memória: evento desfeito não entra, evento confirmado não se perde;
* a chave nunca vai para log, mensagem de erro, manifesto ou estado local.

Só depende da biblioteca padrão, de ``cryptography`` e de ``TerminalError``.
"""

import errno
import hashlib
import json
import os
import secrets
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
from contextlib import closing
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from .store import TerminalError

ROOT_NAME = "PDV2-copia-de-seguranca"
STATE_NAME = "mirror.json"
MANIFEST_NAME = "manifest.json"
SNAPSHOT_SUFFIX = ".pdv2snap"
JOURNAL_SUFFIX = ".pdv2log"
CHUNK_BYTES = 4 * 1024 * 1024
KEEP_SNAPSHOTS = 3
JOURNAL_INTERVAL_SECONDS = 5.0
JOURNAL_BATCH = 500
SNAPSHOT_INTERVAL_MS = 15 * 60 * 1000
SNAPSHOT_MAX_AGE_MS = 24 * 60 * 60 * 1000
SNAPSHOT_RETRY_MS = 60 * 1000
LATE_JOURNAL_MS = 60 * 1000
LATE_SNAPSHOT_MS = SNAPSHOT_MAX_AGE_MS + 2 * 60 * 60 * 1000

MSG_OK = "Cópia de segurança em dia"
MSG_LATE = "Cópia de segurança atrasada"
MSG_NO_KEY = "Conecte este caixa à internet uma vez para liberar a cópia de segurança"
MSG_OFF = "Cópia de segurança em outro disco desligada"
MSG_ERROR = "Não foi possível gravar a cópia de segurança. Confira se o disco ou pendrive está conectado."
MSG_NOT_FOUND = "Nenhuma cópia de segurança deste caixa foi encontrada nesta pasta."
MSG_WRONG_KEY = "Esta cópia de segurança foi feita com outra chave da empresa."
MSG_OTHER_SHOP = "Esta cópia é de outra loja."
MSG_OTHER_TERMINAL = "Esta cópia é de outro caixa."
MSG_SAME_VOLUME = "Escolha outro disco ou um pendrive: esta pasta fica no mesmo disco do caixa."
MSG_DAMAGED_SNAPSHOT = "A cópia mais recente do banco estava danificada; foi usada a anterior."
MSG_NO_SNAPSHOT = "Nenhuma cópia do banco pôde ser lida; foram usados só os registros do diário."
MSG_TRUNCATED = "A última linha do diário estava incompleta e foi ignorada."
MSG_BAD_LINE = "Uma linha do diário estava ilegível e foi ignorada."


class MirrorError(TerminalError):
    """Erro da réplica com mensagem pronta para a tela."""


class _Damaged(Exception):
    """Arquivo da réplica incompleto, alterado ou cifrado com outra chave."""


_REGISTRY_LOCK = threading.Lock()
_LOCKS = {}
_WORKERS = {}
_SNAPSHOT_REQUESTS = set()
_SNAPSHOT_RETRY_AT = {}
_JOURNAL_CACHE = {}
_NEXT_DUE = {}
# No Windows, os.open sem O_BINARY traduz quebras de linha.
_BIN = getattr(os, "O_BINARY", 0)


def _now_ms():
    return time.time_ns() // 1_000_000


def _canon(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _store_id(store):
    return str(store.path)


def _lock_for(store):
    key = _store_id(store)
    with _REGISTRY_LOCK:
        lock = _LOCKS.get(key)
        if lock is None:
            lock = _LOCKS[key] = threading.RLock()
        return lock


# ---------------------------------------------------------------- arquivos

def _fsync_dir(directory):
    if not hasattr(os, "O_DIRECTORY"):
        return
    try:
        descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def _atomic_write(path, data):
    """tmp + fsync + replace: o leitor vê o arquivo antigo inteiro ou o novo inteiro."""
    path = Path(path)
    staging = path.with_name(path.name + ".tmp")
    descriptor = os.open(staging, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | _BIN, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(staging, path)
    except BaseException:
        try:
            staging.unlink()
        except OSError:
            pass
        raise
    _fsync_dir(path.parent)


def _read_json(path):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _root(directory):
    directory = Path(directory)
    return directory if directory.name == ROOT_NAME else directory / ROOT_NAME


# ---------------------------------------------------------------- estado local

def _state_path(store):
    return Path(store.path).parent / STATE_NAME


def _default_state():
    return {"v": 1, "directory": None, "enabled": False, "last_snapshot_at": None,
            "last_journal_at": None, "journal_seq": 0, "last_error": "", "last_error_at": None}


def _load_state(store):
    state = _default_state()
    stored = _read_json(_state_path(store))
    if stored and stored.get("v") == 1:
        for name in state:
            if name in stored:
                state[name] = stored[name]
    if not isinstance(state["directory"], str) or not state["directory"]:
        state["directory"], state["enabled"] = None, False
    state["enabled"] = state["enabled"] is True
    if type(state["journal_seq"]) is not int or state["journal_seq"] < 0:
        state["journal_seq"] = 0
    for name in ("last_snapshot_at", "last_journal_at", "last_error_at"):
        if type(state[name]) is not int:
            state[name] = None
    state["last_error"] = state["last_error"] if isinstance(state["last_error"], str) else ""
    return state


def _save_state(store, state):
    try:
        _atomic_write(_state_path(store), _canon(state).encode("utf-8"))
    except OSError:
        pass  # disco do próprio caixa com problema: a venda decide o que fazer, não a réplica


def _failure_text(exc):
    if isinstance(exc, MirrorError):
        return str(exc)
    if isinstance(exc, OSError):
        if exc.errno in (errno.ENOSPC, getattr(errno, "EDQUOT", -1)):
            return "O disco da cópia de segurança está cheio."
        if isinstance(exc, (FileNotFoundError, NotADirectoryError)) or exc.errno in (errno.ENODEV, errno.ENXIO, errno.EIO):
            return "O disco da cópia de segurança não foi encontrado."
        if isinstance(exc, PermissionError) or exc.errno == errno.EROFS:
            return "Sem permissão para gravar no disco da cópia de segurança."
        return f"Falha ao gravar no disco da cópia de segurança ({type(exc).__name__})."
    # Nunca o texto da exceção: pode trazer trecho de dado.
    return f"Falha na cópia de segurança ({type(exc).__name__})."


def _record_failure(store, state, exc, now):
    state["last_error"] = _failure_text(exc)[:300]
    state["last_error_at"] = now
    _save_state(store, state)


# ---------------------------------------------------------------- chave e banco

def _fernet(key):
    """(key_id, Fernet) de ``{"key_id","key"}``, ou None se não serve."""
    if not isinstance(key, dict):
        return None
    key_id, secret = key.get("key_id"), key.get("key")
    if not isinstance(key_id, str) or not key_id or not isinstance(secret, str) or len(secret) != 44:
        return None
    try:
        return key_id, Fernet(secret.encode("ascii"))
    except (ValueError, UnicodeError):
        return None


def _meta_key(meta):
    try:
        return _fernet(json.loads(meta.get("mirror_key") or "null"))
    except ValueError:
        return None


def _open_readonly(path):
    conn = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=10)
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


def _max_seq(store):
    with closing(_open_readonly(store.path)) as conn:
        return int(conn.execute("SELECT COALESCE(MAX(seq),0) FROM outbox").fetchone()[0])


def _outbox_after(store, after, upto=None):
    query = "SELECT seq,id,payload,created_at FROM outbox WHERE seq>?"
    params = [after]
    if upto is not None:
        query += " AND seq<=?"
        params.append(upto)
    with closing(_open_readonly(store.path)) as conn:
        return conn.execute(query + " ORDER BY seq LIMIT ?", (*params, JOURNAL_BATCH)).fetchall()


def _remove_local_copy(path):
    for suffix in ("", "-journal", "-wal", "-shm"):
        try:
            os.unlink(str(path) + suffix)
        except OSError:
            pass


def _local_copy(store):
    """Cópia consistente e conferida do banco, em arquivo temporário ao lado dele.

    O token do terminal e a própria chave da réplica não viajam na cópia.
    """
    parent = Path(store.path).parent
    for stale in parent.glob(".mirror-*.tmp*"):
        try:
            stale.unlink()
        except OSError:
            pass
    staging = parent / f".mirror-{_now_ms()}-{secrets.token_hex(3)}.tmp"
    os.close(os.open(staging, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
    try:
        with closing(_open_readonly(store.path)) as source, closing(sqlite3.connect(staging)) as target:
            source.backup(target)
            target.execute("PRAGMA journal_mode=DELETE")
            target.execute("PRAGMA secure_delete=ON")
            target.execute("DELETE FROM meta WHERE key IN ('token','mirror_key')")
            target.commit()
            if target.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise MirrorError("A cópia do banco não passou na verificação.", 409)
            outbox_seq = int(target.execute("SELECT COALESCE(MAX(seq),0) FROM outbox").fetchone()[0])
        return staging, outbox_seq
    except BaseException:
        _remove_local_copy(staging)
        raise


# ---------------------------------------------------------------- cópia cifrada

def _write_snapshot(path, plain_path, header, fernet):
    """Grava a cópia cifrada em blocos. Devolve (size, sha256) do arquivo em claro."""
    staging = path.with_name(path.name + ".tmp")
    digest, size, chunks = hashlib.sha256(), 0, 0
    descriptor = os.open(staging, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | _BIN, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as target, open(plain_path, "rb") as source:
            target.write(_canon(header).encode("ascii") + b"\n")
            while True:
                block = source.read(CHUNK_BYTES)
                if not block:
                    break
                digest.update(block)
                size += len(block)
                chunks += 1
                target.write(fernet.encrypt(block) + b"\n")
            trailer = {"chunks": chunks, "size": size, "sha256": digest.hexdigest()}
            target.write(b"END " + fernet.encrypt(_canon(trailer).encode("ascii")) + b"\n")
            target.flush()
            os.fsync(target.fileno())
        os.replace(staging, path)
    except BaseException:
        try:
            staging.unlink()
        except OSError:
            pass
        raise
    _fsync_dir(path.parent)
    return size, digest.hexdigest()


def _snapshot_header(path):
    try:
        with open(path, "rb") as source:
            header = json.loads(source.readline(65536).decode("ascii"))
    except (OSError, ValueError):
        return None
    if not isinstance(header, dict) or header.get("format") != "pdv2-mirror-snapshot" or header.get("v") != 1:
        return None
    return header


def _decrypt_snapshot(path, fernet, output=None):
    """Decifra e confere a cópia. Devolve (cabeçalho, rodapé); levanta _Damaged."""
    digest, size, chunks, trailer = hashlib.sha256(), 0, 0, None
    try:
        with open(path, "rb") as source:
            try:
                header = json.loads(source.readline(65536).decode("ascii"))
            except ValueError:
                raise _Damaged("cabeçalho")
            if not isinstance(header, dict) or header.get("format") != "pdv2-mirror-snapshot" or header.get("v") != 1:
                raise _Damaged("cabeçalho")
            for line in source:
                if trailer is not None or not line.endswith(b"\n"):
                    raise _Damaged("fim")
                line = line[:-1]
                try:
                    if line.startswith(b"END "):
                        trailer = json.loads(fernet.decrypt(line[4:]).decode("ascii"))
                        continue
                    block = fernet.decrypt(line)
                except (InvalidToken, ValueError):
                    raise _Damaged("bloco")
                digest.update(block)
                size += len(block)
                chunks += 1
                if output is not None:
                    output.write(block)
    except OSError:
        raise _Damaged("leitura")
    if (not isinstance(trailer, dict) or trailer.get("chunks") != chunks or trailer.get("size") != size
            or trailer.get("sha256") != digest.hexdigest()):
        raise _Damaged("conferência")
    return header, trailer


# ---------------------------------------------------------------- diário

def _segment_name(first_seq):
    return f"journal-{first_seq:012d}{JOURNAL_SUFFIX}"


def _journal_line(row, fernet):
    record = {"seq": row[0], "event_id": row[1], "created_at": row[3], "event": json.loads(row[2])}
    return b"%d\t%s\t%s\n" % (row[0], str(row[1]).encode("ascii"), fernet.encrypt(_canon(record).encode("utf-8")))


def _parse_journal_line(line, fernet):
    """Registro de uma linha do diário, ou None se ilegível ou se o prefixo não bate."""
    parts = line.rstrip(b"\r\n").split(b"\t")
    if len(parts) != 3:
        return None
    try:
        record = json.loads(fernet.decrypt(parts[2]).decode("utf-8"))
        seq = int(parts[0].decode("ascii"))
        event_id = parts[1].decode("ascii")
    except (InvalidToken, ValueError, TypeError):
        return None
    if (not isinstance(record, dict) or type(record.get("seq")) is not int or record["seq"] != seq
            or record.get("event_id") != event_id or not isinstance(record.get("event"), dict)):
        return None
    return record


def _segment_last_seq(path, fernet):
    """Maior ``seq`` legível do segmento (0 se vazio ou ausente), com cache por tamanho."""
    try:
        size = os.stat(path).st_size
    except OSError:
        _JOURNAL_CACHE.pop(str(path), None)
        return 0
    cached = _JOURNAL_CACHE.get(str(path))
    if cached and cached[0] == size:
        return cached[1]
    last = 0
    with open(path, "rb") as source:
        for line in source:
            record = _parse_journal_line(line, fernet)
            if record:
                last = max(last, record["seq"])
    _JOURNAL_CACHE[str(path)] = (size, last)
    return last


def _append_journal(path, rows, fernet):
    """Acrescenta as linhas com um único fsync. Garante quebra de linha antes de escrever."""
    data = b"".join(_journal_line(row, fernet) for row in rows)
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | _BIN, 0o600)
    with os.fdopen(descriptor, "r+b") as target:
        size = target.seek(0, os.SEEK_END)
        if size:
            target.seek(size - 1)
            if target.read(1) != b"\n":
                data = b"\n" + data  # sobra de uma gravação interrompida fica isolada na linha dela
            target.seek(0, os.SEEK_END)
        target.write(data)
        target.flush()
        os.fsync(target.fileno())
        size = target.tell()
    _JOURNAL_CACHE[str(path)] = (size, rows[-1][0])


# ---------------------------------------------------------------- manifesto

def _manifest_ok(manifest):
    return (isinstance(manifest, dict) and manifest.get("format") == "pdv2-mirror" and manifest.get("v") == 1
            and isinstance(manifest.get("snapshots"), list) and isinstance(manifest.get("journal_segments"), list)
            and all(isinstance(item, dict) and isinstance(item.get("name"), str)
                    and type(item.get("outbox_seq")) is int and type(item.get("created_at")) is int
                    for item in manifest["snapshots"])
            and all(isinstance(item, dict) and isinstance(item.get("name"), str)
                    and type(item.get("first_seq")) is int for item in manifest["journal_segments"]))


def _load_manifest(replica):
    manifest = _read_json(Path(replica) / MANIFEST_NAME)
    return manifest if _manifest_ok(manifest) else None


def _save_manifest(replica, manifest, now):
    manifest["updated_at"] = now
    manifest["app_version"] = os.environ.get("PDV_VERSION", "2.0.0")
    _atomic_write(Path(replica) / MANIFEST_NAME, _canon(manifest).encode("utf-8"))


def _new_manifest(meta, key_id):
    return {"format": "pdv2-mirror", "v": 1,
            "tenant_id": meta.get("tenant_id"), "shop_id": meta.get("shop_id"),
            "terminal_id": meta.get("terminal_id"), "device_id": meta.get("device_id"),
            "tenant_name": meta.get("tenant_name") or "", "shop_name": meta.get("shop_name") or "",
            "terminal_name": meta.get("terminal_name") or "", "key_id": key_id,
            "snapshots": [], "journal_seq": 0, "journal_segments": []}


def _safe_name(name):
    return isinstance(name, str) and name == os.path.basename(name) and name not in ("", ".", "..")


def _unlink_quiet(path):
    try:
        os.unlink(path)
    except OSError:
        pass
    _JOURNAL_CACHE.pop(str(path), None)


# ---------------------------------------------------------------- uma passada

class _Run:
    """Contexto de uma passada: tudo que foi lido do banco e do destino uma única vez."""

    def __init__(self, store, state, meta, key_id, fernet):
        self.store, self.state, self.meta = store, state, meta
        self.key_id, self.fernet = key_id, fernet
        self.replica = _root(state["directory"]) / str(meta["terminal_id"]) / str(meta["device_id"])
        self.manifest = None

    def prepare(self, now):
        """Abre o destino. Devolve True quando é preciso começar uma geração nova."""
        if not Path(self.state["directory"]).is_dir():
            raise FileNotFoundError(errno.ENOENT, "destino ausente")
        self.replica.mkdir(parents=True, exist_ok=True)
        manifest = _load_manifest(self.replica)
        fresh = (manifest is None or manifest.get("key_id") != self.key_id or not manifest["snapshots"]
                 or not manifest["journal_segments"]
                 or any(manifest.get(name) != self.meta.get(name)
                        for name in ("tenant_id", "shop_id", "terminal_id", "device_id")))
        if not fresh:
            self.manifest = manifest
            # Banco local voltou no tempo (restauração manual): o diário antigo não vale mais.
            if _max_seq(self.store) < self.journal_seq():
                self._archive(now)  # pode guardar vendas que o banco restaurado não tem
                fresh = True
        if fresh:
            self.manifest = _new_manifest(self.meta, self.key_id)
        return fresh

    def _archive(self, now):
        """Guarda a geração atual em uma subpasta em vez de apagar."""
        folder = self.replica / f"anterior-{now}"
        folder.mkdir(exist_ok=True)
        for name in os.listdir(self.replica):
            if name == MANIFEST_NAME or name.endswith((SNAPSHOT_SUFFIX, JOURNAL_SUFFIX)):
                os.replace(self.replica / name, folder / name)
                _JOURNAL_CACHE.pop(str(self.replica / name), None)

    def journal_seq(self):
        base = max((item["outbox_seq"] for item in self.manifest["snapshots"]), default=0)
        segments = self.manifest["journal_segments"]
        if not segments:
            return base
        return max(base, _segment_last_seq(self.replica / segments[-1]["name"], self.fernet))

    def append(self, now, upto=None):
        """Leva ao diário as linhas da fila que ainda não estão lá."""
        segment = self.replica / self.manifest["journal_segments"][-1]["name"]
        position = self.journal_seq()
        wrote = False
        while True:
            rows = _outbox_after(self.store, position, upto)
            if not rows:
                break
            _append_journal(segment, rows, self.fernet)
            position, wrote = rows[-1][0], True
        self.state["journal_seq"] = position
        if wrote:
            self.state["last_journal_at"] = now
            self.manifest["journal_seq"] = position
            _save_manifest(self.replica, self.manifest, now)
        return wrote

    def snapshot(self, now, fresh):
        staging, outbox_seq = _local_copy(self.store)
        try:
            if not fresh:
                self.append(now, upto=outbox_seq)  # a cópia anterior continua aproveitável até aqui
            newest = max((item["created_at"] for item in self.manifest["snapshots"]), default=0)
            created = max(now, newest + 1)
            name = f"snapshot-{created}{SNAPSHOT_SUFFIX}"
            header = {"format": "pdv2-mirror-snapshot", "v": 1, "key_id": self.key_id,
                      "terminal_id": self.meta["terminal_id"], "device_id": self.meta["device_id"],
                      "created_at": created, "outbox_seq": outbox_seq, "chunk_bytes": CHUNK_BYTES}
            path = self.replica / name
            size, digest = _write_snapshot(path, staging, header, self.fernet)
            try:
                _, trailer = _decrypt_snapshot(path, self.fernet)
                if trailer["sha256"] != digest or trailer["size"] != size:
                    raise _Damaged("conferência")
            except _Damaged:
                _unlink_quiet(path)
                raise MirrorError("A cópia gravada no disco não passou na conferência.", 409)
        finally:
            _remove_local_copy(staging)
        entry = {"name": name, "created_at": created, "outbox_seq": outbox_seq,
                 "size_bytes": os.stat(path).st_size, "sha256": digest}
        manifest = self.manifest
        manifest["snapshots"] = (manifest["snapshots"] + [entry])[-KEEP_SNAPSHOTS:]
        segment = {"name": _segment_name(outbox_seq + 1), "first_seq": outbox_seq + 1}
        segments = [item for item in manifest["journal_segments"] if item["name"] != segment["name"]] + [segment]
        oldest = manifest["snapshots"][0]["outbox_seq"]
        # Um segmento termina onde o seguinte começa; sai quando a cópia mais antiga já o cobre inteiro.
        manifest["journal_segments"] = [
            item for index, item in enumerate(segments)
            if index == len(segments) - 1 or segments[index + 1]["first_seq"] - 1 > oldest]
        manifest["journal_seq"] = max(outbox_seq, self.state["journal_seq"] if not fresh else 0)
        _save_manifest(self.replica, manifest, now)
        self.state["journal_seq"] = manifest["journal_seq"]
        self.state["last_snapshot_at"] = created
        self._sweep()
        return {"name": name, "created_at": created, "size_bytes": entry["size_bytes"], "outbox_seq": outbox_seq}

    def _sweep(self):
        keep = {MANIFEST_NAME} | {item["name"] for item in self.manifest["snapshots"]}
        keep |= {item["name"] for item in self.manifest["journal_segments"]}
        try:
            names = os.listdir(self.replica)
        except OSError:
            return
        for name in names:
            if name not in keep and (name.endswith((SNAPSHOT_SUFFIX, JOURNAL_SUFFIX, ".tmp"))):
                _unlink_quiet(self.replica / name)

    def next_due(self):
        """Quando a próxima cópia do banco vence se nada mais acontecer."""
        newest = self.manifest["snapshots"][-1]
        moved = self.journal_seq() > newest["outbox_seq"]
        return newest["created_at"] + (SNAPSHOT_INTERVAL_MS if moved else SNAPSHOT_MAX_AGE_MS)

    def snapshot_due(self, now):
        snapshots = self.manifest["snapshots"]
        if not snapshots:
            return True
        age = now - snapshots[-1]["created_at"]
        if age >= SNAPSHOT_MAX_AGE_MS or age < 0:
            return True
        return age >= SNAPSHOT_INTERVAL_MS and self.journal_seq() > snapshots[-1]["outbox_seq"]


def _begin(store, state):
    """_Run pronto para gravar, ou o motivo (texto de estado) de não poder."""
    if not state["enabled"]:
        return None
    meta = store.meta()
    key = _meta_key(meta)
    if key is None or not meta.get("terminal_id") or not meta.get("device_id"):
        return None
    return _Run(store, state, meta, key[0], key[1])


def tick(store, now=None, force_snapshot=False):
    """Uma passada síncrona: diário e, se for hora, cópia do banco. ``now`` em ms. Nunca levanta."""
    try:
        now = _now_ms() if now is None else int(now)
        store_id = _store_id(store)
        with _lock_for(store):
            state = _load_state(store)
            try:
                requested = force_snapshot or store_id in _SNAPSHOT_REQUESTS
                run = _begin(store, state)
                if (run is not None and not requested and not state["last_error"]
                        and now < _NEXT_DUE.get(store_id, 0) and _max_seq(store) == state["journal_seq"]):
                    run = None  # nada novo: não acorda o disco de destino
                if run is not None:
                    _NEXT_DUE.pop(store_id, None)
                    fresh = run.prepare(now)
                    if not fresh:
                        run.append(now)
                    waiting = _SNAPSHOT_RETRY_AT.get(store_id, 0) > now and not force_snapshot
                    if (requested or fresh or run.snapshot_due(now)) and not waiting:
                        try:
                            run.snapshot(now, fresh)
                        except Exception:
                            _SNAPSHOT_RETRY_AT[store_id] = now + SNAPSHOT_RETRY_MS
                            raise
                        _SNAPSHOT_REQUESTS.discard(store_id)
                        _SNAPSHOT_RETRY_AT.pop(store_id, None)
                        run.append(now)
                    if not waiting:
                        state["last_error"], state["last_error_at"] = "", None
                        if run.manifest["snapshots"]:
                            _NEXT_DUE[store_id] = run.next_due()
                    _save_state(store, state)
            except Exception as exc:
                _record_failure(store, state, exc, now)
    except Exception:
        pass
    return status(store)


def snapshot_now(store):
    """Cópia do banco agora, a pedido do gerente. Levanta MirrorError se não der."""
    now = _now_ms()
    with _lock_for(store):
        state = _load_state(store)
        if not state["enabled"]:
            raise MirrorError("A cópia de segurança em outro disco está desligada.", 409)
        run = _begin(store, state)
        if run is None:
            raise MirrorError(MSG_NO_KEY + ".", 409)
        try:
            fresh = run.prepare(now)
            if not fresh:
                run.append(now)
            result = run.snapshot(now, fresh)
            run.append(now)
        except Exception as exc:
            _record_failure(store, state, exc, now)
            if isinstance(exc, MirrorError):
                raise
            raise MirrorError(MSG_ERROR, 409) from None
        _SNAPSHOT_REQUESTS.discard(_store_id(store))
        _SNAPSHOT_RETRY_AT.pop(_store_id(store), None)
        state["last_error"], state["last_error_at"] = "", None
        _save_state(store, state)
        return result


# ---------------------------------------------------------------- estado para a tela

def is_enabled(store):
    try:
        return bool(_load_state(store)["enabled"])
    except Exception:
        return False


def _pending(store, journal_seq):
    try:
        with closing(_open_readonly(store.path)) as conn:
            row = conn.execute("SELECT COUNT(*),MIN(created_at) FROM outbox WHERE seq>?", (journal_seq,)).fetchone()
        return int(row[0]), row[1]
    except (sqlite3.Error, OSError, ValueError):
        return 0, None


def status(store):
    """Estado da réplica lido só do computador do caixa: nunca toca o disco de destino."""
    result = {"supported": True, "enabled": False, "directory": None, "state": "off", "message": MSG_OFF,
              "last_snapshot_at": None, "last_journal_at": None, "journal_seq": 0, "pending_events": 0,
              "last_error": "", "key_id": None}
    try:
        state = _load_state(store)
        key = _meta_key(store.meta())
        result.update(enabled=state["enabled"], directory=state["directory"],
                      last_snapshot_at=state["last_snapshot_at"], last_journal_at=state["last_journal_at"],
                      journal_seq=state["journal_seq"], last_error=state["last_error"],
                      key_id=key[0] if key else None)
        if not state["enabled"]:
            return result
        if key is None:
            result.update(state="no_key", message=MSG_NO_KEY)
            return result
        pending, oldest = _pending(store, state["journal_seq"])
        result["pending_events"] = pending
        now = _now_ms()
        if state["last_error"]:
            result.update(state="error", message=MSG_ERROR)
        elif (state["last_snapshot_at"] is None or now - state["last_snapshot_at"] > LATE_SNAPSHOT_MS
              or (pending and oldest is not None and now - oldest > LATE_JOURNAL_MS)):
            result.update(state="late", message=MSG_LATE)
        else:
            result.update(state="ok", message=MSG_OK)
    except Exception:
        result.update(state="error", message=MSG_ERROR)
    return result


# ---------------------------------------------------------------- discos e configuração

def _volume_id(path):
    """Identidade do volume de um caminho: letra da unidade no Windows, ``st_dev`` nos demais."""
    if sys.platform.startswith("win"):
        return os.path.splitdrive(os.path.abspath(path))[0].lower()
    return os.stat(path).st_dev


def _same_volume(store, directory):
    try:
        return _volume_id(directory) == _volume_id(Path(store.path).parent)
    except OSError:
        return False


def _drive_roots():
    """[(pasta, nome, tipo)] das unidades onde uma réplica pode morar. tipo: True removível, False fixo, None não sei."""
    roots = []
    if sys.platform.startswith("win"):
        try:
            import ctypes
            kernel = ctypes.windll.kernel32
            mask = kernel.GetLogicalDrives()
            for index in range(26):
                if not mask & (1 << index):
                    continue
                root = f"{chr(65 + index)}:\\"
                kind = kernel.GetDriveTypeW(root)
                if kind in (2, 3, 4):  # removível, fixo, rede
                    roots.append((root, root[:2], {2: "pendrive", 3: "disco", 4: "rede"}[kind], kind == 2))
        except Exception:
            pass
        return roots
    patterns = ["/Volumes/*"] if sys.platform == "darwin" else ["/media/*/*", "/run/media/*/*", "/mnt/*"]
    for pattern in patterns:
        for path in sorted(Path("/").glob(pattern.lstrip("/"))):
            try:
                if path.is_dir():
                    roots.append((str(path), path.name, "", None))
            except OSError:
                pass
    return roots


def _size_text(free_bytes):
    gigabytes = free_bytes / 1024 ** 3
    if gigabytes >= 10:
        return f"{gigabytes:.0f} GB livres"
    if gigabytes >= 1:
        return f"{gigabytes:.1f} GB livres".replace(".", ",")
    return f"{free_bytes / 1024 ** 2:.0f} MB livres"


def drives(store):
    """Unidades onde a cópia pode ficar. Unidade sem mídia ou ilegível fica de fora."""
    result = []
    for directory, name, kind, removable in _drive_roots():
        try:
            free = shutil.disk_usage(directory).free
        except OSError:
            continue
        label = " · ".join(part for part in (name, kind, _size_text(free)) if part)
        result.append({"directory": directory, "label": label, "free_bytes": int(free),
                       "removable": removable, "same_volume": _same_volume(store, directory)})
    return result


def configure(store, directory, allow_same_volume=False):
    """Liga a réplica em ``directory`` (e já faz a primeira cópia) ou, com None, desliga."""
    with _lock_for(store):
        state = _load_state(store)
        store_id = _store_id(store)
        if directory is None:
            state.update(enabled=False, directory=None, last_error="", last_error_at=None)
            _save_state(store, state)
            _SNAPSHOT_REQUESTS.discard(store_id)
            _NEXT_DUE.pop(store_id, None)
            return status(store)
        if not isinstance(directory, str) or not directory.strip() or len(directory) > 1024 or "\x00" in directory:
            raise MirrorError("Escolha o disco ou a pasta onde a cópia de segurança vai ficar.")
        target = Path(directory.strip())
        if not target.is_absolute():
            raise MirrorError("Informe o caminho completo da pasta, por exemplo E:\\.")
        try:
            found = target.is_dir()
        except OSError:
            found = False
        if not found:
            raise MirrorError("Esta pasta não foi encontrada. Confira se o disco ou pendrive está conectado.", 404)
        if not allow_same_volume and _same_volume(store, target):
            raise MirrorError(MSG_SAME_VOLUME, 409)
        if not store.meta().get("terminal_id"):
            raise MirrorError("Conecte este caixa à loja antes de ligar a cópia de segurança.", 409)
        probe = _root(target) / f".teste-{secrets.token_hex(4)}.tmp"
        try:
            probe.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write(probe, b"pdv2")
            probe.unlink()
        except OSError:
            raise MirrorError("Não foi possível gravar nesta pasta. Escolha outro disco ou pendrive.", 409) from None
        state = _default_state()
        state.update(directory=str(target), enabled=True)
        _save_state(store, state)
        _SNAPSHOT_RETRY_AT.pop(store_id, None)
        _NEXT_DUE.pop(store_id, None)
        return tick(store, force_snapshot=True)


# ---------------------------------------------------------------- thread

class MirrorWorker:
    """Thread daemon que mantém a réplica em dia. Criada por ``start``."""

    def __init__(self, store):
        self.store = store
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread = threading.Thread(target=self._run, name="pdv-mirror", daemon=True)

    def _run(self):
        while not self._stop.is_set():
            self._wake.wait(JOURNAL_INTERVAL_SECONDS)
            self._wake.clear()
            if self._stop.is_set():
                break
            tick(self.store)

    def wake(self):
        self._wake.set()

    def is_alive(self):
        return self._thread.is_alive()

    def stop(self, timeout=5.0):
        self._stop.set()
        self._wake.set()
        if self._thread.is_alive() and self._thread is not threading.current_thread():
            self._thread.join(timeout)
        with _REGISTRY_LOCK:
            if _WORKERS.get(_store_id(self.store)) is self:
                del _WORKERS[_store_id(self.store)]


def start(store):
    """Sobe (ou devolve) a thread da réplica deste banco."""
    store_id = _store_id(store)
    with _REGISTRY_LOCK:
        worker = _WORKERS.get(store_id)
        if worker is not None and worker.is_alive() and not worker._stop.is_set():
            return worker
        worker = _WORKERS[store_id] = MirrorWorker(store)
        worker._thread.start()
    worker.wake()  # põe em dia o que ficou pendente enquanto o caixa estava desligado
    return worker


def on_event(store):
    """Há evento novo na fila: só acorda a thread. Sem E/S; nunca levanta."""
    try:
        worker = _WORKERS.get(_store_id(store))
        if worker is not None:
            worker.wake()
    except Exception:
        pass


def on_cash_closed(store):
    """Caixa fechado: pede cópia do banco na próxima passada. Sem E/S; nunca levanta."""
    try:
        store_id = _store_id(store)
        _SNAPSHOT_REQUESTS.add(store_id)
        worker = _WORKERS.get(store_id)
        if worker is not None:
            worker.wake()
    except Exception:
        pass


# ---------------------------------------------------------------- leitura (recuperação)

def _replicas(directory):
    """[(pasta da réplica, manifesto)] encontradas a partir de qualquer nível do layout."""
    base = Path(directory)
    found, seen = [], set()
    for pattern in (f"{ROOT_NAME}/*/*/{MANIFEST_NAME}", f"*/*/{MANIFEST_NAME}", f"*/{MANIFEST_NAME}", MANIFEST_NAME):
        try:
            paths = sorted(base.glob(pattern))
        except OSError:
            continue
        for path in paths:
            if str(path) in seen:
                continue
            seen.add(str(path))
            manifest = _load_manifest(path.parent)
            if manifest and isinstance(manifest.get("terminal_id"), str) and isinstance(manifest.get("device_id"), str):
                found.append((path.parent, manifest))
    return found


def _matches(manifest, identity):
    return all(identity.get(name) and manifest.get(name) == identity.get(name)
               for name in ("tenant_id", "shop_id", "terminal_id"))


def candidates(identity, directory=None):
    """Réplicas à vista: as deste caixa primeiro, depois as mais recentes. Nunca levanta."""
    identity = identity if isinstance(identity, dict) else {}
    result = []
    try:
        places = [directory] if directory else [root[0] for root in _drive_roots()]
        for place in places:
            for replica, manifest in _replicas(place):
                snapshots = manifest["snapshots"]
                result.append({
                    "directory": str(place), "replica_id": manifest["device_id"],
                    "terminal_id": manifest["terminal_id"],
                    "terminal_name": str(manifest.get("terminal_name") or ""),
                    "shop_name": str(manifest.get("shop_name") or ""),
                    "updated_at": manifest.get("updated_at") if type(manifest.get("updated_at")) is int else 0,
                    "journal_seq": manifest.get("journal_seq") if type(manifest.get("journal_seq")) is int else 0,
                    "last_snapshot_at": snapshots[-1]["created_at"] if snapshots else None,
                    "matches": _matches(manifest, identity),
                    "current_device": bool(identity.get("device_id")) and manifest["device_id"] == identity.get("device_id"),
                })
    except Exception:
        pass
    result.sort(key=lambda item: (not item["matches"], -item["updated_at"]))
    return result


def _choose_replica(directory, identity, replica_id):
    try:
        found = _replicas(directory) if Path(directory).is_dir() else []
    except OSError:
        found = []
    if replica_id is not None:
        chosen = [item for item in found if item[1]["device_id"] == replica_id]
        if not chosen:
            raise MirrorError(MSG_NOT_FOUND, 404)
        chosen.sort(key=lambda item: item[1]["terminal_id"] != identity.get("terminal_id"))
        replica, manifest = chosen[0]
        if manifest.get("tenant_id") != identity.get("tenant_id") or manifest.get("shop_id") != identity.get("shop_id"):
            raise MirrorError(MSG_OTHER_SHOP, 409)
        if manifest["terminal_id"] != identity.get("terminal_id"):
            raise MirrorError(MSG_OTHER_TERMINAL, 409)
        return replica, manifest
    own = [item for item in found if item[1]["terminal_id"] == identity.get("terminal_id")
           and item[1]["device_id"] != identity.get("device_id")]
    if own:
        replica, manifest = max(own, key=lambda item: item[1].get("updated_at") or 0)
        if manifest.get("tenant_id") != identity.get("tenant_id") or manifest.get("shop_id") != identity.get("shop_id"):
            raise MirrorError(MSG_OTHER_SHOP, 409)
        return replica, manifest
    if found and all(item[1].get("tenant_id") != identity.get("tenant_id")
                     or item[1].get("shop_id") != identity.get("shop_id") for item in found):
        raise MirrorError(MSG_OTHER_SHOP, 409)
    raise MirrorError(MSG_NOT_FOUND, 404)


def _snapshot_pending(path, fernet):
    """Linhas não confirmadas da fila dentro de uma cópia do banco. Levanta _Damaged."""
    with tempfile.TemporaryDirectory(prefix="pdv2-replica-") as folder:
        plain = Path(folder) / "copia.sqlite3"
        with open(plain, "wb") as output:
            header, _ = _decrypt_snapshot(path, fernet, output)
        try:
            with closing(sqlite3.connect(plain)) as conn:
                rows = conn.execute(
                    "SELECT seq,id,payload,created_at FROM outbox WHERE acked_at IS NULL ORDER BY seq").fetchall()
        except sqlite3.Error:
            raise _Damaged("banco")
    events = []
    for seq, event_id, payload, created_at in rows:
        try:
            event = json.loads(payload)
        except (TypeError, ValueError):
            raise _Damaged("fila")
        events.append({"seq": seq, "event_id": event_id, "created_at": created_at, "event": event})
    return header, events


def read_pending(directory, key, identity, replica_id=None):
    """Fila que o computador antigo não chegou a enviar, lida da réplica dele.

    Devolve as linhas não confirmadas da cópia do banco mais recente que estiver inteira,
    mais todo evento do diário posterior a ela, sem repetir ``event_id`` e em ordem de ``seq``.
    """
    opened = _fernet(key)
    if opened is None:
        raise MirrorError("Conecte este caixa à internet para liberar a cópia de segurança.", 409)
    key_id, fernet = opened
    identity = identity if isinstance(identity, dict) else {}
    if not isinstance(directory, str) or not directory.strip():
        raise MirrorError(MSG_NOT_FOUND, 404)
    replica, manifest = _choose_replica(directory.strip(), identity, replica_id)
    if manifest.get("key_id") != key_id:
        raise MirrorError(MSG_WRONG_KEY, 409)

    warnings, snapshot, base_events, tokens, readable = [], None, [], 0, 0
    try:
        names = set(os.listdir(replica))
    except OSError:
        raise MirrorError(MSG_NOT_FOUND, 404) from None
    listed = [item for item in manifest["snapshots"] if _safe_name(item["name"])]
    known = {item["name"] for item in listed}
    for name in sorted(names):  # cópia concluída depois da última gravação do manifesto
        if name.startswith("snapshot-") and name.endswith(SNAPSHOT_SUFFIX) and name not in known:
            header = _snapshot_header(replica / name)
            if header and type(header.get("created_at")) is int and type(header.get("outbox_seq")) is int:
                listed.append({"name": name, "created_at": header["created_at"], "outbox_seq": header["outbox_seq"]})
    for entry in sorted(listed, key=lambda item: item["created_at"], reverse=True):
        if entry["name"] not in names:
            continue
        tokens += 1
        header = _snapshot_header(replica / entry["name"])
        if header and header.get("key_id") != key_id:
            raise MirrorError(MSG_WRONG_KEY, 409)
        try:
            header, base_events = _snapshot_pending(replica / entry["name"], fernet)
        except _Damaged:
            if MSG_DAMAGED_SNAPSHOT not in warnings:
                warnings.append(MSG_DAMAGED_SNAPSHOT)
            continue
        readable += 1
        snapshot = {"name": entry["name"], "created_at": header.get("created_at"),
                    "outbox_seq": header.get("outbox_seq")}
        break
    if snapshot is None:
        base_events = []
        if tokens:
            warnings = [MSG_NO_SNAPSHOT]
    floor = snapshot["outbox_seq"] if snapshot and type(snapshot["outbox_seq"]) is int else 0

    journal, last_seq = {}, floor
    for name in sorted(item for item in names if item.startswith("journal-") and item.endswith(JOURNAL_SUFFIX)):
        try:
            with open(replica / name, "rb") as source:
                lines = source.read().split(b"\n")
        except OSError:
            warnings.append(MSG_BAD_LINE)
            continue
        complete, tail = lines[:-1], lines[-1]
        for line in complete:
            if not line.strip():
                continue
            tokens += 1
            record = _parse_journal_line(line, fernet)
            if record is None:
                if MSG_BAD_LINE not in warnings:
                    warnings.append(MSG_BAD_LINE)
                continue
            readable += 1
            journal.setdefault(record["seq"], record)
        if tail.strip():
            tokens += 1
            record = _parse_journal_line(tail, fernet)
            if record is None:
                warnings.append(MSG_TRUNCATED)
            else:  # só faltou a quebra de linha: o conteúdo é autenticado e está inteiro
                readable += 1
                journal.setdefault(record["seq"], record)
    if tokens and not readable:
        # Nada abriu com esta chave: é outra chave (ou a réplica inteira foi adulterada).
        raise MirrorError(MSG_WRONG_KEY, 409)

    events, seen = [], set()
    for item in base_events:
        if item["event_id"] not in seen and (not floor or item["seq"] <= floor):
            seen.add(item["event_id"])
            events.append(item)
    for seq in sorted(journal):
        last_seq = max(last_seq, seq)
        record = journal[seq]
        if seq > floor and record["event_id"] not in seen:
            seen.add(record["event_id"])
            events.append({"seq": seq, "event_id": record["event_id"],
                           "created_at": record.get("created_at"), "event": record["event"]})
    events.sort(key=lambda item: item["seq"])
    return {"terminal_id": manifest["terminal_id"], "replica_id": manifest["device_id"], "snapshot": snapshot,
            "journal_last_seq": last_seq, "events": events, "warnings": warnings}
