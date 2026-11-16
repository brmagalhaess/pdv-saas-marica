"""Réplica contínua do caixa principal nos caixas adicionais, pela rede da loja (fase 3).

Os formatos são os da réplica em segundo disco (``terminal/mirror.py``): cópia do banco
``.pdv2snap`` e diário com uma linha cifrada por evento, com a mesma chave da empresa.

* Lado do caixa principal: ``handle_request`` atende ``/lan/v1/replica/*`` (quem chama é a escuta
  TLS, que já conferiu o token do caixa adicional) e ``status`` alimenta a tela do principal.
* Lado do caixa adicional: ``start_puller`` mantém em ``<dados>/replica`` uma cópia com o mesmo
  desenho de pastas do segundo disco, que os leitores da fase 2 e a promoção abrem sem mudança.

Regras que este módulo nunca quebra:

* nada aqui derruba uma venda: os ganchos só acordam quem espera, sem E/S;
* a cópia do banco é feita em thread própria, uma por vez, nunca dentro de um pedido de venda;
* a chave só sai do principal por ``GET /lan/v1/replica/key`` e, no caixa adicional, só existe em
  ``replica/state.json`` (permissão 600); nunca vai para log, mensagem de erro ou estado de tela.

Só depende da biblioteca padrão, de ``cryptography`` (via ``mirror``) e de ``TerminalError``.
"""

import json
import os
import re
import threading
import time
from pathlib import Path
from urllib.parse import parse_qs

from . import mirror
from .store import TerminalError

PREFIX = "/lan/v1/replica/"
OUT_DIR = "replica-out"
INDEX_NAME = "index.json"
KEEP_OUT = 2
KEEP_LOCAL = 2
STATE_NAME = "state.json"
REPLICA_DIR = "replica"
MAX_WAIT_SECONDS = 25.0
SNAPSHOT_INTERVAL_MS = mirror.SNAPSHOT_INTERVAL_MS
SNAPSHOT_MAX_AGE_MS = mirror.SNAPSHOT_MAX_AGE_MS
SNAPSHOT_RETRY_MS = mirror.SNAPSHOT_RETRY_MS
LATE_MS = 60 * 1000
FORGET_MS = 24 * 60 * 60 * 1000
BACKOFF_MAX_SECONDS = 30.0
DOWNLOAD_CHUNK = 64 * 1024
SERVE_CHUNK = 256 * 1024
MAX_SNAPSHOT_BYTES = 64 * 1024 ** 3

MSG_NO_KEY = "Conecte o caixa principal à internet uma vez para liberar a cópia de segurança."
MSG_RESTORING = "O caixa principal ainda está trazendo os dados da loja."
MSG_SERVER_OFF = "Nenhum caixa adicional está guardando a cópia de segurança"
MSG_SERVER_OK = "Cópia de segurança nos outros caixas em dia"
MSG_SERVER_LATE = "Cópia de segurança nos outros caixas atrasada"
MSG_OK = "Cópia de segurança do caixa principal em dia"
MSG_LATE = "Cópia de segurança atrasada"
MSG_EMPTY = "Este caixa ainda não tem a cópia do caixa principal"
MSG_LOCAL_ERROR = "Não foi possível gravar a cópia de segurança neste computador."
MSG_REJECTED = "O caixa principal enviou dados que não conferem com a chave da cópia de segurança."

_SNAPSHOT_NAME = re.compile(r"snapshot-\d{1,20}" + re.escape(mirror.SNAPSHOT_SUFFIX))
_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
_RANGE = re.compile(r"bytes=(\d{1,18})-")
_SHA = re.compile(r"[0-9a-f]{64}")


def _now_ms():
    return time.time_ns() // 1_000_000


def _version():
    return os.environ.get("PDV_VERSION", "2.0.0")


# ================================================================ lado do caixa principal

class _Server:
    """Estado em memória da réplica de um banco: cópias prontas, espera longa e progresso."""

    def __init__(self):
        self.cond = threading.Condition()
        self.ticks = 0            # sobe a cada evento novo: quem espera compara antes de dormir
        self.entries = None       # cópias prontas, a mais nova por último; None = ainda não lido do disco
        self.building = False
        self.cash_closed = False
        self.retry_at = 0
        self.progress = {}
        self.error = ""


_SERVERS = {}
_SERVERS_LOCK = threading.Lock()
_BUILD_LOCK = threading.Lock()   # uma cópia do banco por vez no processo
_THREADS = []
_EPOCH = 0                       # stop_all troca: quem está em espera longa responde na hora


def _store_id(store):
    return str(store.path)


def _server_for(store):
    key = _store_id(store)
    with _SERVERS_LOCK:
        server = _SERVERS.get(key)
        if server is None:
            server = _SERVERS[key] = _Server()
        return server


def _on_mirror(kind, store):
    """Gancho de ``mirror.on_event`` / ``on_cash_closed``: só marca e acorda. Sem E/S."""
    server = _SERVERS.get(_store_id(store))
    if server is None:
        return
    with server.cond:
        if kind == "cash_closed":
            server.cash_closed = True
        server.ticks += 1
        server.cond.notify_all()


mirror.add_listener(_on_mirror)


def _json_reply(status, payload, extra=None):
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    headers = {"Content-Type": "application/json; charset=utf-8", "Content-Length": str(len(body)),
               "Cache-Control": "no-store"}
    headers.update(extra or {})
    return status, headers, body


def _error(status, message, code, **extra):
    return _json_reply(status, dict({"error": message, "code": code}, **extra))


def _params(path, query):
    values = {}
    if "?" in path:
        path, raw = path.split("?", 1)
        values.update({name: items[-1] for name, items in parse_qs(raw).items()})
    for name, value in (query or {}).items():
        if isinstance(value, (list, tuple)):
            if not value:
                continue
            value = value[-1]
        values[str(name)] = value
    return path, values


def _number(values, name, default, low, high, cast=int):
    raw = values.get(name)
    if raw is None or raw == "":
        return default
    try:
        number = cast(str(raw).strip())
    except (TypeError, ValueError):
        raise TerminalError("Pedido inválido", 400) from None
    if not low <= number <= high:
        raise TerminalError("Pedido inválido", 400)
    return number


def _source_state(meta):
    """"restoring", "no_key" ou None (pode copiar), com a mesma regra da réplica em segundo disco."""
    if (meta.get("restore_state") or "done") != "done":
        return "restoring"
    if mirror.key_from_meta(meta) is None or not meta.get("terminal_id") or not meta.get("device_id"):
        return "no_key"
    return None


def _generation(meta):
    """Geração do banco do principal: ``finish_promotion`` soma um a cada promoção (1 se nunca houve)."""
    try:
        return max(int(meta.get("server_generation") or 1), 1)
    except (TypeError, ValueError):
        return 1


def _blocked(state):
    if state == "restoring":
        return _error(409, MSG_RESTORING, "restoring")
    return _error(409, MSG_NO_KEY, "no_key")


def _out_dir(store):
    return Path(store.path).parent / OUT_DIR


def _entry_ok(entry):
    return (isinstance(entry, dict) and isinstance(entry.get("name"), str)
            and _SNAPSHOT_NAME.fullmatch(entry["name"]) is not None
            and all(type(entry.get(name)) is int for name in ("created_at", "outbox_seq", "size_bytes"))
            and isinstance(entry.get("sha256"), str))


def _save_index(directory, entries):
    try:
        mirror._atomic_write(directory / INDEX_NAME, mirror._canon({"v": 1, "entries": entries}).encode("utf-8"))
    except OSError:
        pass  # sem o índice a cópia é refeita na próxima partida; nada se perde


def _sweep_out(directory, entries):
    keep = {INDEX_NAME} | {entry["name"] for entry in entries}
    try:
        names = os.listdir(directory)
    except OSError:
        return
    for name in names:
        if name not in keep and name.endswith((mirror.SNAPSHOT_SUFFIX, ".tmp")):
            mirror._unlink_quiet(directory / name)


def _entries(server, store, meta):
    """Cópias prontas que ainda valem para este banco e esta chave (chamar com ``server.cond``)."""
    directory = _out_dir(store)
    if server.entries is None:
        stored = mirror._read_json(directory / INDEX_NAME) or {}
        server.entries = [entry for entry in stored.get("entries") or [] if _entry_ok(entry)]
    key = mirror.key_from_meta(meta) or {}
    try:
        top = mirror.max_seq(store)
    except Exception:
        top = None
    valid = []
    for entry in server.entries:
        try:
            size = os.stat(directory / entry["name"]).st_size
        except OSError:
            continue
        if (size == entry["size_bytes"] and entry.get("key_id") == key.get("key_id")
                and entry.get("terminal_id") == meta.get("terminal_id")
                and entry.get("device_id") == meta.get("device_id")
                and (top is None or entry["outbox_seq"] <= top)):
            valid.append(entry)
    if valid != server.entries:
        # Chave, banco ou fila mudaram: a cópia antiga não serve mais a ninguém.
        server.entries = valid
        _save_index(directory, valid)
        _sweep_out(directory, valid)
    return valid


def _build(server, store):
    now = _now_ms()
    try:
        with _BUILD_LOCK:
            directory = _out_dir(store)
            entry = mirror.make_snapshot(store, directory)
            header = mirror._snapshot_header(directory / entry["name"]) or {}
            entry.update(key_id=header.get("key_id"), terminal_id=header.get("terminal_id"),
                         device_id=header.get("device_id"))
            with server.cond:
                kept = [item for item in (server.entries or []) if item.get("key_id") == entry["key_id"]
                        and item.get("device_id") == entry["device_id"]]
                server.entries = (kept + [entry])[-KEEP_OUT:]
                _save_index(directory, server.entries)
                _sweep_out(directory, server.entries)
                server.error, server.retry_at = "", 0
    except Exception as exc:
        # Nunca o texto da exceção: pode trazer trecho de dado.
        server.error = str(exc) if isinstance(exc, TerminalError) else f"Falha ao copiar o banco ({type(exc).__name__})."
        server.retry_at = now + SNAPSHOT_RETRY_MS
    finally:
        with server.cond:
            server.building = False
            server.cond.notify_all()


def _maybe_build(server, store, entries, now):
    """Decide se é hora de uma cópia nova e, se for, sobe a thread (chamar com ``server.cond``)."""
    if server.building or 0 < server.retry_at - now <= SNAPSHOT_RETRY_MS:
        return
    due = not entries or server.cash_closed
    if not due:
        newest = entries[-1]
        age = now - newest["created_at"]
        if age >= SNAPSHOT_MAX_AGE_MS or age < 0:
            due = True
        elif age >= SNAPSHOT_INTERVAL_MS:
            try:
                due = mirror.max_seq(store) > newest["outbox_seq"]
            except Exception:
                due = False
    if not due:
        return
    server.building, server.cash_closed = True, False
    thread = threading.Thread(target=_build, args=(server, store), name="pdv-replica-out", daemon=True)
    with _SERVERS_LOCK:
        _THREADS[:] = [item for item in _THREADS if item.is_alive()] + [thread]
    thread.start()


def _touch(server, station, **changes):
    station_id = str((station or {}).get("id") or "")
    if not station_id:
        return
    with server.cond:
        item = server.progress.setdefault(station_id, {"journal_seq": 0, "snapshot_outbox_seq": None, "at": 0})
        item.update(changes)
        item["at"] = _now_ms()


def _route_status(server, store):
    meta = store.meta()
    state = _source_state(meta)
    key = mirror.key_from_meta(meta) or {}
    snapshot = None
    if state is None:
        now = _now_ms()
        with server.cond:
            entries = _entries(server, store, meta)
            _maybe_build(server, store, entries, now)
            if entries:
                newest = entries[-1]
                snapshot = {name: newest[name] for name in ("name", "created_at", "outbox_seq", "size_bytes", "sha256")}
        state = "ok" if snapshot else "preparing"
    try:
        outbox_seq = mirror.max_seq(store)
    except Exception:
        outbox_seq = 0
    return _json_reply(200, {
        "v": 1, "state": state, "terminal_id": meta.get("terminal_id"), "device_id": meta.get("device_id"),
        "tenant_id": meta.get("tenant_id"), "shop_id": meta.get("shop_id"),
        "tenant_name": meta.get("tenant_name") or "", "shop_name": meta.get("shop_name") or "",
        "terminal_name": meta.get("terminal_name") or "", "key_id": key.get("key_id"),
        "app_version": _version(), "generation": _generation(meta), "outbox_seq": outbox_seq, "snapshot": snapshot,
        "server_time_ms": _now_ms()})


def _route_key(store):
    meta = store.meta()
    key = mirror.key_from_meta(meta)
    if key is None:
        return _error(409, MSG_NO_KEY, "no_key")
    return _json_reply(200, key)


class _FileBody:
    """Corpo de resposta em blocos; fecha o arquivo ao terminar ou em ``close()``."""

    def __init__(self, handle, remaining):
        self._handle, self._remaining = handle, remaining

    def __iter__(self):
        return self

    def __next__(self):
        if self._remaining <= 0 or self._handle is None:
            self.close()
            raise StopIteration
        block = self._handle.read(min(SERVE_CHUNK, self._remaining))
        if not block:
            self.close()
            raise StopIteration
        self._remaining -= len(block)
        return block

    def close(self):
        handle, self._handle = self._handle, None
        if handle is not None:
            handle.close()


def _route_snapshot(server, store, name, headers):
    gone = _error(404, "Esta cópia não existe mais no caixa principal.", "snapshot_gone")
    if not _SNAPSHOT_NAME.fullmatch(name or ""):
        return gone
    meta = store.meta()
    state = _source_state(meta)
    if state:
        return _blocked(state)
    with server.cond:
        entry = next((item for item in _entries(server, store, meta) if item["name"] == name), None)
    if entry is None:
        return gone
    try:
        handle = open(_out_dir(store) / name, "rb")
    except OSError:
        return gone
    size, start, status = entry["size_bytes"], 0, 200
    requested = headers.get("range")
    extra = {"Content-Type": "application/octet-stream", "Accept-Ranges": "bytes", "Cache-Control": "no-store"}
    if requested:
        match = _RANGE.fullmatch(str(requested).strip())
        if not match or int(match.group(1)) >= size:
            handle.close()
            reply = _error(416, "Trecho da cópia inválido.", "range_invalid")
            reply[1]["Content-Range"] = f"bytes */{size}"
            return reply
        start, status = int(match.group(1)), 206
        handle.seek(start)
        extra["Content-Range"] = f"bytes {start}-{size - 1}/{size}"
    extra["Content-Length"] = str(size - start)
    return status, extra, _FileBody(handle, size - start)


def _route_journal(server, store, values):
    after = _number(values, "after", 0, 0, 2 ** 62)
    wait = _number(values, "wait", 0.0, 0.0, MAX_WAIT_SECONDS, float)
    limit = _number(values, "limit", mirror.JOURNAL_BATCH, 1, mirror.JOURNAL_BATCH)
    after_id = values.get("after_id") or None
    if after_id is not None and not (isinstance(after_id, str) and _ID.fullmatch(after_id)):
        raise TerminalError("Pedido inválido", 400)
    deadline, epoch = time.monotonic() + wait, _EPOCH
    while True:
        with server.cond:
            ticks = server.ticks
        meta = store.meta()
        state = _source_state(meta)
        if state:
            return _blocked(state)
        top = mirror.max_seq(store)
        if after > top:
            return _error(409, "A fila do caixa principal recomeçou.", "journal_reset", outbox_seq=top)
        if after_id is not None and after and mirror.event_id_at(store, after) != after_id:
            # Mesmo número, outro evento: quem pede seguiu outra linha do tempo (o principal foi
            # trocado por promoção ou restaurado). Ele guarda o que tem e começa outra geração.
            return _error(409, "A fila do caixa principal não é a que este caixa acompanhava.", "journal_reset",
                          outbox_seq=top)
        after_id = None  # conferido uma vez; a espera longa não repete a leitura
        lines = mirror.journal_lines(store, after, limit) if top > after else []
        remaining = deadline - time.monotonic()
        if lines or remaining <= 0 or epoch != _EPOCH:
            break
        with server.cond:
            if server.ticks == ticks:  # nenhum evento entre a leitura e agora
                server.cond.wait(min(remaining, 5.0))
    body = b"".join(lines)
    key = mirror.key_from_meta(meta) or {}
    return 200, {"Content-Type": "application/octet-stream", "Content-Length": str(len(body)),
                 "Cache-Control": "no-store", "X-PDV-Journal-Last": str(top),
                 "X-PDV-Journal-Count": str(len(lines)), "X-PDV-Journal-Key": str(key.get("key_id") or "")}, body


def _route_ack(server, station, values, body):
    data = {}
    if body:
        try:
            data = json.loads(body.decode("utf-8") if isinstance(body, (bytes, bytearray)) else body)
        except (ValueError, UnicodeError, AttributeError):
            raise TerminalError("Pedido inválido", 400) from None
        if not isinstance(data, dict):
            raise TerminalError("Pedido inválido", 400)
    merged = dict(values)
    merged.update({name: data[name] for name in ("journal_seq", "snapshot_outbox_seq") if data.get(name) is not None})
    journal_seq = _number(merged, "journal_seq", None, 0, 2 ** 62)
    if journal_seq is None:
        raise TerminalError("Pedido inválido", 400)
    snapshot_seq = _number(merged, "snapshot_outbox_seq", None, 0, 2 ** 62)
    _touch(server, station, journal_seq=journal_seq, snapshot_outbox_seq=snapshot_seq)
    return _json_reply(200, {"ok": True})


def handle_request(store, station, method, path, query, headers, body=None):
    """Atende ``/lan/v1/replica/*`` para um caixa adicional já autenticado pela escuta TLS.

    Devolve ``(status, cabeçalhos, corpo)``; o corpo é ``bytes`` ou um iterador de ``bytes``.
    O ``ack`` aceita os números no corpo JSON (``body``) ou na query. Nunca levanta.
    """
    try:
        path, values = _params(str(path or ""), query)
        headers = {str(name).lower(): value for name, value in (headers or {}).items()}
        method = str(method or "").upper()
        if not path.startswith(PREFIX):
            return _error(404, "Rota não encontrada", "not_found")
        route = path[len(PREFIX):]
        server = _server_for(store)
        wanted = "POST" if route == "ack" else "GET"
        if method != wanted:
            return _error(405, "Método não permitido", "method_not_allowed")
        _touch(server, station)
        if route == "status":
            return _route_status(server, store)
        if route == "key":
            return _route_key(store)
        if route.startswith("snapshot/"):
            return _route_snapshot(server, store, route[len("snapshot/"):], headers)
        if route == "journal":
            reply = _route_journal(server, store, values)
            _touch(server, station)
            return reply
        if route == "ack":
            return _route_ack(server, station, values, body)
        return _error(404, "Rota não encontrada", "not_found")
    except TerminalError as exc:
        return _error(exc.status, str(exc), getattr(exc, "code", None) or "bad_request")
    except Exception as exc:
        return _error(500, f"Falha na cópia de segurança pela rede ({type(exc).__name__}).", "replica_error")


def status(store):
    """Como está a cópia nos caixas adicionais, para a tela do principal. Nunca levanta.

    ``ok`` quando pelo menos um caixa adicional está com a cópia em dia; ``late`` quando há caixas
    conhecidos e nenhum está em dia; ``off`` quando nenhum caixa adicional puxou a cópia.
    """
    result = {"supported": True, "state": "off", "message": MSG_SERVER_OFF, "stations": {}}
    try:
        server = _SERVERS.get(_store_id(store))
        if server is None:
            return result
        now = _now_ms()
        with server.cond:
            for station_id in [key for key, item in server.progress.items() if now - item["at"] > FORGET_MS]:
                del server.progress[station_id]
            progress = {key: dict(item) for key, item in server.progress.items()}
        current = False
        for station_id, item in progress.items():
            behind, oldest = mirror._pending(store, item["journal_seq"])
            result["stations"][station_id] = {"journal_seq": item["journal_seq"], "behind": behind, "at": item["at"]}
            fresh = now - item["at"] <= LATE_MS and item["snapshot_outbox_seq"] is not None
            if fresh and not (behind and oldest is not None and now - oldest > LATE_MS):
                current = True
        if progress:
            result.update(state="ok" if current else "late", message=MSG_SERVER_OK if current else MSG_SERVER_LATE)
    except Exception:
        pass
    return result


def stop_all():
    """Solta quem está em espera longa, espera a cópia em andamento e esquece o estado em memória."""
    global _EPOCH
    _EPOCH += 1
    with _SERVERS_LOCK:
        servers, threads = list(_SERVERS.values()), list(_THREADS)
    for server in servers:
        with server.cond:
            server.ticks += 1
            server.cond.notify_all()
    for thread in threads:
        if thread.is_alive() and thread is not threading.current_thread():
            thread.join(30)
    with _SERVERS_LOCK:
        _SERVERS.clear()
        _THREADS[:] = [thread for thread in _THREADS if thread.is_alive()]
    for puller in list(_PULLERS.values()):
        puller.stop()


# ================================================================ lado do caixa adicional

class _Network(Exception):
    """Sem contato com o caixa principal (rede, TLS, conexão cortada)."""


class _Refused(Exception):
    """O caixa principal respondeu, mas não com o que foi pedido."""

    def __init__(self, status, code=""):
        super().__init__(f"HTTP {status}")
        self.status, self.code = status, code


class _Rejected(Exception):
    """Dado recebido que não confere com a chave ou com a identidade esperada."""


class _Again(Exception):
    """O principal mudou no meio da passada: começar de novo, sem contar como erro."""


def _base_dir(data_dir):
    return Path(data_dir) / REPLICA_DIR


def _default_state():
    return {"v": 1, "terminal_id": None, "device_id": None, "key": None, "journal_seq": 0, "server_seq": 0,
            "last_snapshot": None, "last_contact_at": None, "last_error": "", "error_kind": "",
            "app_version": "", "hint": "", "behind_since": None, "generation": None}


def _load_state(data_dir):
    state = _default_state()
    stored = mirror._read_json(_base_dir(data_dir) / STATE_NAME)
    if stored and stored.get("v") == 1:
        for name in state:
            if name in stored:
                state[name] = stored[name]
    for name in ("terminal_id", "device_id"):
        if not isinstance(state[name], str) or not _ID.fullmatch(state[name]):
            state[name] = None
    if mirror._fernet(state["key"]) is None:
        state["key"] = None
    for name in ("journal_seq", "server_seq"):
        if type(state[name]) is not int or state[name] < 0:
            state[name] = 0
    for name in ("last_contact_at", "behind_since"):
        if type(state[name]) is not int:
            state[name] = None
    if not _entry_ok(state["last_snapshot"]):
        state["last_snapshot"] = None
    if type(state["generation"]) is not int or state["generation"] < 1:
        state["generation"] = None
    for name in ("last_error", "error_kind", "app_version", "hint"):
        if not isinstance(state[name], str):
            state[name] = ""
    return state


def puller_status(data_dir):
    """Estado da cópia neste caixa adicional, lido só de ``replica/state.json``. Nunca levanta e
    nunca devolve a chave."""
    result = {"state": "empty", "message": MSG_EMPTY, "journal_seq": 0, "server_seq": 0,
              "last_snapshot_at": None, "last_contact_at": None, "last_error": ""}
    try:
        state = _load_state(data_dir)
        snapshot = state["last_snapshot"]
        result.update(journal_seq=state["journal_seq"], server_seq=state["server_seq"],
                      last_snapshot_at=snapshot["created_at"] if snapshot else None,
                      last_contact_at=state["last_contact_at"], last_error=state["last_error"])
        now = _now_ms()
        if state["hint"] == "no_key":
            result.update(state="no_key", message=MSG_NO_KEY)
        elif snapshot is None:
            pass
        elif state["last_error"] and state["error_kind"] in ("local", "data"):
            result.update(state="error", message=MSG_REJECTED if state["error_kind"] == "data" else MSG_LOCAL_ERROR)
        elif (state["last_contact_at"] is None or now - state["last_contact_at"] > LATE_MS
              or (state["behind_since"] is not None and now - state["behind_since"] > LATE_MS)):
            result.update(state="late", message=MSG_LATE)
        else:
            result.update(state="ok", message=MSG_OK)
    except Exception:
        result.update(state="error", message=MSG_LOCAL_ERROR)
    return result


def _append_raw(path, data):
    """Acrescenta linhas já prontas ao diário com um único fsync, isolando sobra de gravação cortada."""
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | mirror._BIN, 0o600)
    with os.fdopen(descriptor, "r+b") as target:
        size = target.seek(0, os.SEEK_END)
        if size:
            target.seek(size - 1)
            if target.read(1) != b"\n":
                data = b"\n" + data
            target.seek(0, os.SEEK_END)
        target.write(data)
        target.flush()
        os.fsync(target.fileno())
    mirror._JOURNAL_CACHE.pop(str(path), None)


class Puller:
    """Mantém a cópia do caixa principal neste computador. ``start_puller`` cria e sobe a thread;
    ``cycle`` faz uma passada síncrona (usada pela thread e pelos testes)."""

    LONG_POLL_SECONDS = MAX_WAIT_SECONDS

    def __init__(self, data_dir, client_factory, *, interval=2.0):
        self.data_dir = Path(data_dir)
        self.base = _base_dir(data_dir)
        self.client_factory = client_factory
        self.interval = max(float(interval), 0.05)
        self.failures = 0
        self._again = 0
        self._client = None
        self._progressed = False
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread = threading.Thread(target=self._run, name="pdv-replica-pull", daemon=True)
        self.state = _load_state(data_dir)

    # ----- thread
    def start(self):
        if not self._thread.is_alive() and not self._stop.is_set():
            self._thread.start()
        return self

    def is_alive(self):
        return self._thread.is_alive()

    def wake(self):
        self._wake.set()

    def stop(self, timeout=5.0):
        self._stop.set()
        self._wake.set()
        if self._thread.is_alive() and self._thread is not threading.current_thread():
            self._thread.join(timeout)
        with _SERVERS_LOCK:
            if _PULLERS.get(str(self.data_dir)) is self:
                del _PULLERS[str(self.data_dir)]

    def next_delay(self):
        """Recuo depois de ``failures`` falhas seguidas: intervalo, dobrando até 30 s."""
        if not self.failures:
            return self.interval
        return min(self.interval * (2 ** (self.failures - 1)), BACKOFF_MAX_SECONDS)

    def _run(self):
        while not self._stop.is_set():
            started = time.monotonic()
            done = self.cycle(wait=self.LONG_POLL_SECONDS)
            if self._stop.is_set():
                break
            if done and self._progressed:
                continue
            pause = self.next_delay()
            if done:
                pause = max(pause - (time.monotonic() - started), 0)
            if pause:
                self._wake.wait(pause)
            self._wake.clear()

    # ----- conversa com o principal
    def _request(self, method, path, *, body=None, headers=None, timeout=None):
        try:
            if self._client is None:
                self._client = self.client_factory()
            return self._client.request(method, path, body=body, headers=headers, timeout=timeout)
        except Exception:
            # O texto do erro de rede não é guardado: pode trazer endereço ou trecho do pedido.
            raise _Network() from None

    def _drop_client(self):
        client, self._client = self._client, None
        try:
            if client is not None and hasattr(client, "close"):
                client.close()
        except Exception:
            pass

    @staticmethod
    def _read_all(response, limit=64 * 1024 * 1024):
        data = bytearray()
        try:
            while True:
                block = response.read(DOWNLOAD_CHUNK)
                if not block:
                    break
                data += block
                if len(data) > limit:
                    raise _Rejected()
        except _Rejected:
            raise
        except Exception:
            return bytes(data), False
        finally:
            try:
                response.close()
            except Exception:
                pass
        return bytes(data), True

    def _json(self, method, path, *, body=None, timeout=15.0):
        headers = {"Accept": "application/json"}
        if body is not None:
            body = json.dumps(body, separators=(",", ":")).encode("utf-8")
            headers["Content-Type"] = "application/json"
        response = self._request(method, path, body=body, headers=headers, timeout=timeout)
        data, whole = self._read_all(response, 1024 * 1024)
        if not whole:
            raise _Network()
        try:
            payload = json.loads(data.decode("utf-8")) if data else {}
        except (ValueError, UnicodeError):
            payload = None
        if response.status != 200:
            raise _Refused(response.status, str((payload or {}).get("code") or "") if isinstance(payload, dict) else "")
        if not isinstance(payload, dict):
            raise _Rejected()
        return payload

    # ----- disco
    def _save_state(self):
        self.base.mkdir(parents=True, exist_ok=True)
        try:
            self.base.chmod(0o700)
        except OSError:
            pass
        mirror._atomic_write(self.base / STATE_NAME, mirror._canon(self.state).encode("utf-8"))

    def _replica(self):
        return self.base / mirror.ROOT_NAME / self.state["terminal_id"] / self.state["device_id"]

    def _fernet(self):
        return mirror._fernet(self.state["key"])[1]

    def _journal_seq(self, manifest):
        segments = manifest["journal_segments"]
        if not segments:
            return 0
        last = segments[-1]
        return max(last["first_seq"] - 1, mirror._segment_last_seq(self._replica() / last["name"], self._fernet()))

    def _journal_tail_id(self, manifest, position):
        """``event_id`` da linha do diário daqui com ``seq == position``; None se a posição vem só da
        cópia do banco (nenhuma linha gravada com esse número)."""
        if position <= 0:
            return None
        for segment in reversed(manifest["journal_segments"]):
            seq, event_id = mirror._segment_last(self._replica() / segment["name"], self._fernet())
            if seq:
                return event_id if seq == position and isinstance(event_id, str) and _ID.fullmatch(event_id) else None
        return None

    def _new_manifest(self, info):
        manifest = mirror._new_manifest(info, self.state["key"]["key_id"])
        manifest["app_version"] = self.state["app_version"]
        return manifest

    def _save_manifest(self, manifest, now):
        manifest["updated_at"] = now
        manifest["app_version"] = self.state["app_version"]
        mirror._atomic_write(self._replica() / mirror.MANIFEST_NAME, mirror._canon(manifest).encode("utf-8"))

    def _archive(self, now):
        """Guarda a geração atual em ``replica/anterior-<ms>/`` (nunca apaga) e zera os contadores."""
        root = self.base / mirror.ROOT_NAME
        has_files = False
        try:
            has_files = any(path.is_file() for path in root.rglob("*"))
        except OSError:
            pass
        if has_files:
            stamp = now
            while (self.base / f"anterior-{stamp}").exists():
                stamp += 1
            folder = self.base / f"anterior-{stamp}"
            folder.mkdir(parents=True)
            os.replace(root, folder / mirror.ROOT_NAME)
            mirror._fsync_dir(self.base)
        for cached in [name for name in mirror._JOURNAL_CACHE if name.startswith(str(root))]:
            mirror._JOURNAL_CACHE.pop(cached, None)
        self.state.update(terminal_id=None, device_id=None, journal_seq=0, last_snapshot=None, behind_since=None)

    def _sweep(self, manifest, keep_part=None):
        keep = {mirror.MANIFEST_NAME} | {item["name"] for item in manifest["snapshots"]}
        keep |= {item["name"] for item in manifest["journal_segments"]}
        if keep_part:
            keep.add(keep_part)
        replica = self._replica()
        try:
            names = os.listdir(replica)
        except OSError:
            return
        for name in names:
            if name not in keep and name.endswith((mirror.SNAPSHOT_SUFFIX, mirror.JOURNAL_SUFFIX, ".tmp", ".part")):
                mirror._unlink_quiet(replica / name)

    # ----- cópia do banco
    def _download(self, remote, manifest, now):
        replica, name, size = self._replica(), remote["name"], remote["size_bytes"]
        part = replica / (name + ".part")
        self._sweep(manifest, keep_part=part.name)
        try:
            have = os.stat(part).st_size
        except OSError:
            have = 0
        if have > size:
            mirror._unlink_quiet(part)
            have = 0
        if have < size:
            headers = {"Accept": "application/octet-stream"}
            if have:
                headers["Range"] = f"bytes={have}-"
            response = self._request("GET", PREFIX + "snapshot/" + name, headers=headers, timeout=30.0)
            try:
                if response.status in (404, 416):
                    mirror._unlink_quiet(part)
                    raise _Again()
                if response.status == 200:
                    have = 0  # o principal mandou do começo: recomeça o arquivo
                elif response.status != 206 or not have:
                    raise _Refused(response.status)
                else:
                    span = str((getattr(response, "headers", None) or {}).get("content-range") or "")
                    if span and not span.startswith(f"bytes {have}-"):
                        mirror._unlink_quiet(part)  # o principal respondeu outro trecho: recomeça
                        raise _Again()
                flags = os.O_WRONLY | os.O_CREAT | mirror._BIN | (os.O_APPEND if have else os.O_TRUNC)
                cut = False
                with os.fdopen(os.open(part, flags, 0o600), "ab" if have else "wb") as target:
                    try:
                        while have < size:
                            try:
                                block = response.read(min(DOWNLOAD_CHUNK, size - have))
                            except Exception:
                                cut = True
                                break
                            if not block:
                                cut = True
                                break
                            target.write(block)
                            have += len(block)
                            self._progressed = True
                    finally:
                        target.flush()
                        os.fsync(target.fileno())
                if cut or have < size:
                    raise _Network()  # o que chegou fica no .part e é retomado por Range
            finally:
                try:
                    response.close()
                except Exception:
                    pass
        check = mirror.verify_snapshot(part, self.state["key"])
        header = check["header"] if check else {}
        if (check is None or check["sha256"] != remote["sha256"]
                or header.get("key_id") != self.state["key"]["key_id"]
                or header.get("terminal_id") != self.state["terminal_id"]
                or header.get("device_id") != self.state["device_id"]
                or header.get("outbox_seq") != remote["outbox_seq"]):
            mirror._unlink_quiet(part)
            raise _Rejected()
        position = self._journal_seq(manifest)
        os.replace(part, replica / name)
        mirror._fsync_dir(replica)
        entry = {name_: remote[name_] for name_ in ("name", "created_at", "outbox_seq", "size_bytes", "sha256")}
        manifest["snapshots"] = ([item for item in manifest["snapshots"] if item["name"] != name] + [entry])[-KEEP_LOCAL:]
        # Primeira cópia: o diário começa logo depois dela. Depois disso ele nunca pula: o segmento
        # novo começa onde o anterior parou, e o antigo só sai quando a cópia mais velha o cobre.
        first = (position if manifest["journal_segments"] else remote["outbox_seq"]) + 1
        segment = {"name": mirror._segment_name(first), "first_seq": first}
        segments = [item for item in manifest["journal_segments"] if item["name"] != segment["name"]] + [segment]
        oldest = manifest["snapshots"][0]["outbox_seq"]
        manifest["journal_segments"] = [item for index, item in enumerate(segments)
                                        if index == len(segments) - 1 or segments[index + 1]["first_seq"] - 1 > oldest]
        manifest["journal_seq"] = max(first - 1, position)
        self._save_manifest(manifest, now)
        self._sweep(manifest)
        self.state["last_snapshot"] = entry
        self.state["journal_seq"] = self._journal_seq(manifest)
        self._progressed = True

    # ----- diário
    def _pull_journal(self, manifest, wait):
        """Uma leva do diário. Devolve quantas linhas entraram; grava só linha conferida."""
        position = self._journal_seq(manifest)
        path = f"{PREFIX}journal?after={position}&wait={int(wait)}&limit={mirror.JOURNAL_BATCH}"
        tail_id = self._journal_tail_id(manifest, position)
        if tail_id:
            path += f"&after_id={tail_id}"  # o principal confere que ainda é a mesma linha do tempo
        response = self._request("GET", path, headers={"Accept": "application/octet-stream"},
                                 timeout=float(wait) + 15.0)
        data, whole = self._read_all(response)
        if response.status == 409:
            code = ""
            try:
                code = str(json.loads(data.decode("utf-8")).get("code") or "")
            except (ValueError, UnicodeError, AttributeError):
                pass
            if code == "journal_reset":
                self._archive(_now_ms())
                self._save_state()
                raise _Again()
            raise _Refused(409, code)
        if response.status != 200:
            raise _Refused(response.status)
        headers = getattr(response, "headers", None) or {}
        try:
            self.state["server_seq"] = max(int(headers.get("x-pdv-journal-last")), position)
        except (TypeError, ValueError):
            pass
        announced = headers.get("x-pdv-journal-key")
        if announced and announced != self.state["key"]["key_id"]:
            raise _Again()  # a chave mudou no principal: a próxima passada busca a nova
        accepted, rejected = [], False
        for line in data.split(b"\n")[:-1]:  # o que vem depois da última quebra de linha está incompleto
            if not line:
                continue
            record = mirror.parse_journal_line(line, self.state["key"])
            identity = record["event"].get("identity") if record else None
            if (record is None or record["seq"] <= position or not isinstance(identity, dict)
                    or identity.get("terminal_id") != self.state["terminal_id"]):
                rejected = True
                break
            accepted.append(line + b"\n")
            position = record["seq"]
        if accepted:
            _append_raw(self._replica() / manifest["journal_segments"][-1]["name"], b"".join(accepted))
            self.state["journal_seq"] = position
            self.state["server_seq"] = max(self.state["server_seq"], position)
            self._progressed = True
        if rejected:
            raise _Rejected()
        if not whole:
            raise _Network()
        return len(accepted)

    def _drain_journal(self, manifest, wait):
        got = self._pull_journal(manifest, wait)
        while got >= mirror.JOURNAL_BATCH:
            got = self._pull_journal(manifest, 0)

    def _ack(self):
        snapshot = self.state["last_snapshot"]
        body = {"journal_seq": self.state["journal_seq"],
                "snapshot_outbox_seq": snapshot["outbox_seq"] if snapshot else None}
        path = f"{PREFIX}ack?journal_seq={body['journal_seq']}"
        if snapshot:
            path += f"&snapshot_outbox_seq={snapshot['outbox_seq']}"
        self._json("POST", path, body=body)

    def _mark_position(self, now):
        state = self.state
        if state["server_seq"] > state["journal_seq"]:
            state["behind_since"] = state["behind_since"] or now
        else:
            state["behind_since"] = None

    # ----- uma passada
    def _cycle(self, wait):
        state, now = self.state, _now_ms()
        info = self._json("GET", PREFIX + "status")
        state["last_contact_at"] = now
        remote_state = info.get("state")
        if remote_state in ("no_key", "restoring"):
            state.update(hint=remote_state, last_error="", error_kind="")
            self._save_state()
            return
        terminal_id, device_id, key_id = info.get("terminal_id"), info.get("device_id"), info.get("key_id")
        if (not isinstance(terminal_id, str) or not _ID.fullmatch(terminal_id) or not isinstance(device_id, str)
                or not _ID.fullmatch(device_id) or not isinstance(key_id, str) or not key_id
                or type(info.get("outbox_seq")) is not int or info["outbox_seq"] < 0):
            raise _Rejected()
        if state["terminal_id"] and (state["terminal_id"], state["device_id"]) != (terminal_id, device_id):
            self._archive(now)  # o principal agora é outro banco: a cópia do anterior fica guardada
        generation = info.get("generation")
        generation = generation if type(generation) is int and generation >= 1 else None
        if generation is not None and state["generation"] is not None and generation != state["generation"]:
            # Outro caixa assumiu como principal: a identidade é a mesma, a linha do tempo não. O que
            # este caixa recebeu do principal antigo fica guardado, nunca misturado com o do novo.
            self._archive(now)
        if generation is not None:
            state["generation"] = generation
        if state["key"] is None or state["key"]["key_id"] != key_id:
            key = self._json("GET", PREFIX + "key")
            key = {"key_id": key.get("key_id"), "key": key.get("key")}
            if mirror._fernet(key) is None:
                raise _Rejected()
            if key["key_id"] != key_id:
                raise _Again()
            if state["key"] is not None:
                self._archive(now)  # arquivos da chave antiga não se misturam com os da nova
            state["key"] = key
        state.update(terminal_id=terminal_id, device_id=device_id, hint="",
                     app_version=str(info.get("app_version") or "")[:40])
        replica = self._replica()
        replica.mkdir(parents=True, exist_ok=True)
        manifest = mirror._load_manifest(replica)
        if manifest is None or manifest.get("key_id") != key_id:
            manifest = self._new_manifest(info)
        if info["outbox_seq"] < self._journal_seq(manifest):
            # O principal voltou no tempo (banco restaurado): esta cópia pode ter vendas que ele não tem.
            self._archive(now)
            state.update(terminal_id=terminal_id, device_id=device_id)
            replica.mkdir(parents=True, exist_ok=True)
            manifest = self._new_manifest(info)
        state["server_seq"] = info["outbox_seq"]
        state["journal_seq"] = self._journal_seq(manifest)
        self._save_state()

        remote = info.get("snapshot")
        if remote is not None:
            if (not _entry_ok(remote) or not _SHA.fullmatch(remote["sha256"])
                    or not 0 < remote["size_bytes"] <= MAX_SNAPSHOT_BYTES):
                raise _Rejected()
            local = manifest["snapshots"][-1] if manifest["snapshots"] else None
            if local is None or (local["name"] != remote["name"] and
                                 (remote["outbox_seq"], remote["created_at"]) > (local["outbox_seq"], local["created_at"])):
                self._download(remote, manifest, now)
                self._save_state()
        if not manifest["snapshots"] or not manifest["journal_segments"]:
            self._mark_position(now)
            state.update(last_error="", error_kind="")
            self._save_state()
            return  # sem cópia base o diário não teria onde se apoiar
        state["last_snapshot"] = manifest["snapshots"][-1]

        self._drain_journal(manifest, 0)
        self._ack()
        self._mark_position(_now_ms())
        state.update(last_error="", error_kind="", last_contact_at=_now_ms())
        self._save_state()
        if wait > 0 and not self._stop.is_set():
            before = state["journal_seq"]
            self._drain_journal(manifest, wait)
            state["last_contact_at"] = _now_ms()
            if state["journal_seq"] != before:
                self._ack()
            self._mark_position(_now_ms())
            self._save_state()

    def _failed(self, text, kind):
        self.failures += 1
        self._drop_client()
        self.state.update(last_error=text[:300], error_kind=kind)
        try:
            self._mark_position(_now_ms())
            self._save_state()
        except Exception:
            pass

    def cycle(self, wait=0.0):
        """Uma passada: estado, chave, cópia do banco, diário e confirmação. Devolve True quando
        terminou sem erro; nunca levanta."""
        with self._lock:
            self._progressed = False
            try:
                try:
                    self._cycle(wait)
                    self._again = 0
                except _Again:
                    self._again += 1
                    if self._again > 3:  # não era passageiro: recua como em qualquer outra falha
                        raise _Refused(409, "again") from None
                    self._progressed = True  # recomeça já, sem recuo
                self.failures = 0
                return True
            except _Network:
                self._failed("Sem contato com o caixa principal.", "network")
            except _Refused as exc:
                self._failed(f"O caixa principal recusou o pedido ({exc.status}).", "server")
            except _Rejected:
                self._failed(MSG_REJECTED, "data")
            except OSError as exc:
                self._failed(f"{MSG_LOCAL_ERROR} ({type(exc).__name__})", "local")
            except Exception as exc:
                self._failed(f"Falha na cópia de segurança ({type(exc).__name__}).", "local")
            return False


_PULLERS = {}


def start_puller(data_dir, client_factory, *, interval=2.0):
    """Sobe (ou devolve) a thread ``pdv-replica-pull`` deste computador."""
    key = str(Path(data_dir))
    with _SERVERS_LOCK:
        puller = _PULLERS.get(key)
        if puller is not None and puller.is_alive() and not puller._stop.is_set():
            return puller
        puller = _PULLERS[key] = Puller(data_dir, client_factory, interval=interval)
        puller.start()
    return puller
