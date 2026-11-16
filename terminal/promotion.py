"""Promoção assistida: um caixa adicional assume como caixa principal com a cópia que guardou.

Usada quando o computador principal não vai voltar (quebrou, queimou, foi roubado). O caixa
adicional abre a cópia do banco que o ``replica_net`` manteve em ``<dados>/replica``, aplica o
diário posterior a ela (sem criar evento novo) e passa a ser o banco da loja.

Passos de ``promote`` (estado em ``<dados>/replica/promotion.json``):

    verifying  -> PIN de gerente conferido na própria cópia
    restoring  -> ``mirror.restore_database`` em ``terminal.sqlite3.promoting``
    replaying  -> ``Store.apply_journal_events``
    finalizing -> ``Store.finish_promotion`` e troca atômica do banco
    done | failed

Qualquer falha apaga o ``.promoting`` e deixa este computador como caixa adicional. Este módulo
não troca o modo nem reinicia o serviço: isso é de quem chama (``station.py``).

Só depende da biblioteca padrão, de ``cryptography`` (via ``mirror``), de ``shared.security`` e de
``TerminalError``; o ``Store`` só é importado na hora de promover.
"""

import json
import os
import shutil
import sqlite3
import tempfile
import threading
import time
from contextlib import closing, contextmanager
from pathlib import Path

from . import mirror, replica_net
from .store import TerminalError

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None
try:
    import msvcrt
except ImportError:
    msvcrt = None

STATE_NAME = "promotion.json"
LOCK_NAME = "promotion.lock"
DATABASE_NAME = "terminal.sqlite3"
PROMOTING_NAME = DATABASE_NAME + ".promoting"
PROFILE_POINTER = "active-profile.json"
MANAGER_ROLES = ("owner", "admin", "manager")
PIN_MAX_FAILURES = 5
PIN_LOCK_MS = 5 * 60 * 1000
STEPS = ("verifying", "restoring", "replaying", "finalizing")

MSG_NO_COPY = "Este caixa ainda não tem uma cópia completa do caixa principal. Não é possível assumir."
MSG_NEWER = "Atualize este caixa antes de assumir: o caixa principal usava uma versão mais nova."
MSG_BAD_PIN = "PIN de gerente inválido"
MSG_LOCKED = "Muitas tentativas; aguarde cinco minutos"
MSG_ALREADY = "Este computador já assumiu como caixa principal."
MSG_RUNNING = "A promoção já está em andamento neste computador."
MSG_INTERRUPTED = "A operação foi interrompida antes de terminar. Tente de novo."
MSG_FAILED = "Não foi possível assumir como caixa principal"

_ACTIVE = set()
_ACTIVE_LOCK = threading.Lock()


def _now_ms():
    return time.time_ns() // 1_000_000


def _base(data_dir):
    return Path(data_dir) / replica_net.REPLICA_DIR


def _default():
    return {"v": 1, "state": "idle", "step": "", "error": "", "started_at": None, "finished_at": None,
            "failures": 0, "locked_until": None, "result": None}


def _load(data_dir):
    saved = _default()
    stored = mirror._read_json(_base(data_dir) / STATE_NAME)
    if stored and stored.get("v") == 1:
        for name in saved:
            if name in stored:
                saved[name] = stored[name]
    if saved["state"] not in ("idle", "running", "done", "failed"):
        saved["state"] = "idle"
    saved["step"] = saved["step"] if isinstance(saved["step"], str) else ""
    saved["error"] = saved["error"] if isinstance(saved["error"], str) else ""
    saved["failures"] = saved["failures"] if type(saved["failures"]) is int and saved["failures"] >= 0 else 0
    saved["locked_until"] = saved["locked_until"] if type(saved["locked_until"]) is int else None
    saved["result"] = saved["result"] if isinstance(saved["result"], dict) else None
    return saved


def _save(data_dir, saved):
    base = _base(data_dir)
    base.mkdir(parents=True, exist_ok=True)
    try:
        base.chmod(0o700)
    except OSError:
        pass
    mirror._atomic_write(base / STATE_NAME, mirror._canon(saved).encode("utf-8"))


def _version_tuple(value):
    try:
        return tuple(int(part) for part in str(value).strip().split("."))
    except ValueError:
        return ()


def _copy(data_dir):
    """O que este caixa guarda do principal: chave, identidade e índice da cópia (sem levantar)."""
    state = replica_net._load_state(data_dir)
    manifest = None
    if state["terminal_id"] and state["device_id"]:
        replica = _base(data_dir) / mirror.ROOT_NAME / state["terminal_id"] / state["device_id"]
        try:
            manifest = mirror._open_replica(replica)
            if manifest is not None:
                names = set(os.listdir(replica))
                manifest["snapshots"] = [item for item in manifest["snapshots"] if item["name"] in names]
        except OSError:
            manifest = None
    complete = bool(state["key"] and manifest and manifest["snapshots"])
    return {"state": state, "manifest": manifest, "complete": complete}


def _require_copy(data_dir):
    copy = _copy(data_dir)
    if not copy["complete"]:
        raise TerminalError(MSG_NO_COPY, 409)
    theirs = _version_tuple(copy["manifest"].get("app_version") or copy["state"]["app_version"])
    if theirs and theirs > _version_tuple(replica_net._version()):
        raise TerminalError(MSG_NEWER, 409)
    return copy


def _identity(copy):
    manifest = copy["manifest"]
    return {"tenant_id": manifest.get("tenant_id"), "shop_id": manifest.get("shop_id"),
            "terminal_id": copy["state"]["terminal_id"], "device_id": None}


def _restore(data_dir, copy, target):
    try:
        return mirror.restore_database(str(_base(data_dir)), copy["state"]["key"], _identity(copy), target,
                                       replica_id=copy["state"]["device_id"])
    except mirror.MirrorError:
        # Cópia ausente, de outra chave ou toda danificada: para o lojista é "não tenho a cópia".
        raise TerminalError(MSG_NO_COPY, 409) from None


def _remove_database(path):
    for suffix in ("", "-journal", "-wal", "-shm", ".tmp"):
        try:
            os.unlink(str(path) + suffix)
        except OSError:
            pass


def status(data_dir):
    """Estado do assistente "Assumir como caixa principal" para a tela. Nunca levanta."""
    result = {"available": False, "reason": MSG_NO_COPY, "state": "idle", "step": "", "error": "",
              "shop_name": "", "terminal_name": "", "last_snapshot_at": None, "journal_seq": 0,
              "last_contact_at": None, "locked_until": None}
    try:
        saved = _load(data_dir)
        copy = _copy(data_dir)
        manifest = copy["manifest"] or {}
        snapshots = manifest.get("snapshots") or []
        now = _now_ms()
        state, error = saved["state"], saved["error"]
        if state == "running" and str(Path(data_dir)) not in _ACTIVE:
            state, error = "failed", MSG_INTERRUPTED  # o processo caiu no meio; nada foi trocado
        if state == "done" and not pending(data_dir):
            state = "idle"
        result.update(
            state=state, step=saved["step"] if state == "running" else "", error=error if state == "failed" else "",
            shop_name=str(manifest.get("shop_name") or ""), terminal_name=str(manifest.get("terminal_name") or ""),
            last_snapshot_at=max((item["created_at"] for item in snapshots), default=None),
            journal_seq=int(manifest.get("journal_seq") or 0),
            last_contact_at=copy["state"]["last_contact_at"],
            locked_until=saved["locked_until"] if saved["locked_until"] and saved["locked_until"] > now else None)
        if state == "done":
            result.update(available=False, reason=MSG_ALREADY)
        elif not copy["complete"]:
            result.update(available=False, reason=MSG_NO_COPY)
        else:
            theirs = _version_tuple(manifest.get("app_version") or copy["state"]["app_version"])
            newer = bool(theirs and theirs > _version_tuple(replica_net._version()))
            result.update(available=not newer, reason=MSG_NEWER if newer else "")
    except Exception:
        pass
    return result


def _managers(data_dir, copy):
    """Gerentes ativos lidos da cópia decifrada em arquivo temporário, apagado em seguida."""
    base = _base(data_dir)
    folder = Path(tempfile.mkdtemp(prefix=".pin-", dir=base))
    try:
        plain = folder / "copia.sqlite3"
        _restore(data_dir, copy, plain)
        try:
            _check_restored(plain, copy)  # PIN de outro banco plantado na pasta não autoriza nada
            with closing(sqlite3.connect(plain)) as conn:
                marks = ",".join("?" for _ in MANAGER_ROLES)
                return conn.execute(f"SELECT id,name,role,pin_hash FROM operators WHERE active=1 AND role IN ({marks})",
                                    MANAGER_ROLES).fetchall()
        except sqlite3.Error:
            raise TerminalError(MSG_NO_COPY, 409) from None
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def verify_manager(data_dir, pin):
    """Confere o PIN de um gerente contra a cópia do caixa principal guardada neste computador.

    Cinco erros seguidos bloqueiam por cinco minutos. O PIN nunca é gravado nem registrado.
    """
    from shared.security import verify_pin

    saved, now = _load(data_dir), _now_ms()
    if saved["locked_until"] and saved["locked_until"] > now:
        raise TerminalError(MSG_LOCKED, 429)
    copy = _require_copy(data_dir)
    match = None
    if isinstance(pin, str) and pin:
        for operator_id, name, role, pin_hash in _managers(data_dir, copy):
            if verify_pin(pin, pin_hash):
                match = {"operator_id": operator_id, "name": name, "role": role}
                break
    saved, now = _load(data_dir), _now_ms()
    if saved["locked_until"] and saved["locked_until"] > now:
        raise TerminalError(MSG_LOCKED, 429)
    if match is None:
        saved["failures"] += 1
        locked = saved["failures"] >= PIN_MAX_FAILURES
        if locked:
            saved.update(failures=0, locked_until=now + PIN_LOCK_MS)
        _save(data_dir, saved)
        raise TerminalError(MSG_LOCKED if locked else MSG_BAD_PIN, 429 if locked else 403)
    if saved["failures"] or saved["locked_until"]:
        saved.update(failures=0, locked_until=None)
        _save(data_dir, saved)
    return match


@contextmanager
def _exclusive(data_dir):
    """Uma promoção por vez neste computador: trava de arquivo que o sistema solta se o processo cair."""
    key = str(Path(data_dir))
    with _ACTIVE_LOCK:
        if key in _ACTIVE:
            raise TerminalError(MSG_RUNNING, 409)
        _ACTIVE.add(key)
    handle = None
    try:
        base = _base(data_dir)
        base.mkdir(parents=True, exist_ok=True)
        handle = open(base / LOCK_NAME, "a+b")
        try:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            elif msvcrt is not None:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            raise TerminalError(MSG_RUNNING, 409) from None
        yield
    finally:
        if handle is not None:
            try:
                if fcntl is not None:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                elif msvcrt is not None:
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
            handle.close()
        with _ACTIVE_LOCK:
            _ACTIVE.discard(key)


def _seal(path):
    """Deixa o banco em um arquivo só (sem -wal/-shm), conferido e gravado no disco."""
    with closing(sqlite3.connect(path, timeout=10)) as conn:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.execute("PRAGMA journal_mode=DELETE")
        if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise TerminalError(f"{MSG_FAILED}: o banco não passou na verificação.", 409)
    for suffix in ("-journal", "-wal", "-shm"):
        try:
            os.unlink(str(path) + suffix)
        except OSError:
            pass
    descriptor = os.open(path, os.O_RDWR | mirror._BIN)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _check_restored(path, copy):
    """O banco decifrado é o do caixa principal esperado e não traz segredo que não devia."""
    with closing(sqlite3.connect(path)) as conn:
        meta = dict(conn.execute("SELECT key,value FROM meta").fetchall())
    if (meta.get("terminal_id") != copy["state"]["terminal_id"] or meta.get("device_id") != copy["state"]["device_id"]
            or "token" in meta or "mirror_key" in meta):
        raise TerminalError(MSG_NO_COPY, 409)


def _swap(data_dir, promoting, now):
    """Troca atômica: o banco que existia aqui é renomeado (nunca apagado) e o promovido assume."""
    data_dir = Path(data_dir)
    target = data_dir / DATABASE_NAME
    kept = None
    if target.exists():
        try:
            _seal(target)
        except (sqlite3.Error, OSError, TerminalError):
            pass  # banco antigo ilegível: vai guardado do jeito que está
        stamp = now
        while (data_dir / f"terminal-antes-da-promocao-{stamp}.sqlite3").exists():
            stamp += 1
        kept = data_dir / f"terminal-antes-da-promocao-{stamp}.sqlite3"
        for suffix in ("-wal", "-shm", "-journal"):
            if os.path.exists(str(target) + suffix):
                os.replace(str(target) + suffix, str(kept) + suffix)
        os.replace(target, kept)
    try:
        os.unlink(data_dir / PROFILE_POINTER)  # o banco promovido é o da pasta de dados, não o de um perfil
    except OSError:
        pass
    os.replace(promoting, target)
    try:
        target.chmod(0o600)
    except OSError:
        pass
    mirror._fsync_dir(data_dir)
    return kept


def promote(data_dir, pin, *, station_id, station_name, store_factory=None):
    """Faz deste caixa adicional o caixa principal. Levanta TerminalError e não muda nada se falhar."""
    data_dir = Path(data_dir)
    promoting = data_dir / PROMOTING_NAME
    with _exclusive(data_dir):
        if pending(data_dir):
            raise TerminalError(MSG_ALREADY, 409)
        saved = _load(data_dir)
        saved.update(state="running", step="", error="", started_at=_now_ms(), finished_at=None, result=None)

        def step(name):
            saved["step"] = name
            _save(data_dir, saved)

        _remove_database(promoting)  # sobra de uma tentativa interrompida
        try:
            step("verifying")
            manager = verify_manager(data_dir, pin)
            for name in ("failures", "locked_until"):
                saved[name] = _load(data_dir)[name]
            copy = _require_copy(data_dir)
            key = copy["state"]["key"]

            step("restoring")
            restored = _restore(data_dir, copy, promoting)
            _check_restored(promoting, copy)

            step("replaying")
            if store_factory is None:
                from .store import Store as store_factory
            store = store_factory(promoting)
            applied = store.apply_journal_events(restored["events"])

            step("finalizing")
            finished = store.finish_promotion(station_id, station_name, key, manager["operator_id"])
            del store
            _seal(promoting)
            now = _now_ms()
            kept = _swap(data_dir, promoting, now)
            result = {"promoted": True, "applied": int((applied or {}).get("applied") or 0),
                      "journal_last_seq": restored["journal_last_seq"],
                      "snapshot_at": restored["snapshot"]["created_at"],
                      "previous_server_cash_open": bool((finished or {}).get("previous_server_cash_open")),
                      "warnings": list(restored["warnings"])}
            saved.update(state="done", step="", finished_at=now,
                         result=dict(result, previous_database=kept.name if kept else None))
            _save(data_dir, saved)
            return result
        except BaseException as exc:
            _remove_database(promoting)
            # Só mensagem pronta para a tela: o texto de outra exceção pode trazer dado ou caminho.
            text = str(exc) if isinstance(exc, TerminalError) else f"{MSG_FAILED} ({type(exc).__name__})."
            for name in ("failures", "locked_until"):
                saved[name] = _load(data_dir)[name]
            saved.update(state="failed", error=text[:300], finished_at=_now_ms())
            try:
                _save(data_dir, saved)
            except OSError:
                pass
            if isinstance(exc, TerminalError) or not isinstance(exc, Exception):
                raise
            raise TerminalError(text, 500) from None


def pending(data_dir):
    """True quando a promoção terminou e o banco já está no lugar: falta só trocar o modo.

    Cobre a queda entre a troca do banco e a troca de modo: na partida, quem vê True sobe como
    caixa principal.
    """
    try:
        return _load(data_dir)["state"] == "done" and (Path(data_dir) / DATABASE_NAME).is_file()
    except Exception:
        return False
