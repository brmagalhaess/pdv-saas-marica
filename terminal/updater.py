"""Automatic update of an installed terminal. Standard library only.

A verified release manifest (stored by ``sync.check_release``) is downloaded to
``<data>/updates/<version>/``, checked against its size and SHA-256 and kept as
"staged". Applying extracts it to ``<install>/app-<version>/``, swaps
``current.txt`` atomically and asks the service host for a restart. The host
confirms ``/health`` on the next start and moves the pointer back if it fails.
Nothing is ever applied with an open cashier or from a source checkout.
"""

import hashlib
import http.client
import json
import os
import secrets
import shutil
import stat
import threading
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from urllib.parse import urlsplit

from . import paths

DEV_REASON = "instalação de desenvolvimento"
STATES = ("idle", "downloading", "staged", "applying", "failed")
HEALTH_TIMEOUT = 60
PACKAGE_NAME = "package.zip"
BUNDLE_MARKER = "bundle.json"
RESTART_REQUEST = "restart.request"
MAX_BUNDLE_BYTES = 2 * 1024 ** 3
MAX_BUNDLE_FILES = 20000
RETRY_BASE_SECONDS = 5 * 60
RETRY_MAX_SECONDS = 6 * 60 * 60
RESUME_RETRY_SECONDS = 60
PRINT_BUSY_MS = 2 * 60 * 1000
FORMAT_FROZEN = "frozen-win64"
FORMAT_SOURCE = "source"

_STATE_LOCK = threading.RLock()
_APPLY_LOCK = threading.Lock()
_DOWNLOAD_LOCK = threading.Lock()
_download_thread = None


class UpdateError(Exception):
    """Definitive failure of this attempt: the package is discarded."""


class DownloadInterrupted(UpdateError):
    """Connection dropped: the partial file is kept and the download resumes."""


# ---------------------------------------------------------------- locations

class Context:
    def __init__(self, install_root=None, data_dir=None, env=None):
        self.env = os.environ if env is None else env
        root = install_root or paths.install_root(self.env)
        self.install_root = Path(root) if root else None
        self.pointer_version = paths.read_pointer(self.install_root)
        self.installed = self.pointer_version is not None
        self.data_dir = Path(data_dir or paths.data_dir(self.env, installed=self.installed))
        self.updates = self.data_dir / "updates"

    @property
    def running_version(self):
        """Version of the code in this process (the pointer may already be ahead)."""
        value = str(self.env.get("PDV_VERSION") or "").strip()
        if paths.VERSION_RE.fullmatch(value):
            return value
        return self.pointer_version or paths.source_version(self.env)

    @property
    def bundle_format(self):
        """Package flavour this installation can run."""
        folder = paths.app_dir(self.install_root, self.pointer_version)
        return FORMAT_FROZEN if (folder / "PDV2Server.exe").exists() else FORMAT_SOURCE


def package_path(updates, version):
    paths.parse_version(version)
    return Path(updates) / str(version) / PACKAGE_NAME


# -------------------------------------------------------------------- state

def load_state(updates):
    try:
        value = json.loads((Path(updates) / "state.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        value = {}
    if not isinstance(value, dict) or value.get("state") not in STATES:
        value = {"state": "idle"}
    return value


def save_state(updates, **changes):
    """Merge and persist atomically. A value of None removes the key."""
    updates = Path(updates)
    with _STATE_LOCK:
        state = load_state(updates)
        state.update(changes)
        state = {key: value for key, value in state.items() if value is not None}
        updates.mkdir(parents=True, exist_ok=True)
        temporary = updates / f".state-{secrets.token_hex(4)}.tmp"
        try:
            with temporary.open("w", encoding="utf-8") as stream:
                json.dump(state, stream, ensure_ascii=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, updates / "state.json")
        finally:
            temporary.unlink(missing_ok=True)
        return state


def _reset(updates, **extra):
    return save_state(updates, state="idle", version=None, error=None, attempts=None,
                      retry_at=None, **extra)


# ----------------------------------------------------------------- manifest

def available_manifest(store, current_version):
    """Verified manifest saved by the terminal, only when newer than this version."""
    try:
        payload = json.loads(store.meta().get("release_info") or "null")
    except ValueError:
        return None
    if not isinstance(payload, dict) or payload.get("component", "terminal") != "terminal":
        return None
    try:
        if not paths.is_newer(payload.get("version", ""), current_version):
            return None
    except ValueError:
        return None
    digest = str(payload.get("sha256", ""))
    size = payload.get("size_bytes")
    if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        return None
    if type(size) is not int or not 0 < size <= MAX_BUNDLE_BYTES or not isinstance(payload.get("url"), str):
        return None
    return payload


def manifest_block(manifest, current_version):
    """Why this manifest cannot be applied by swapping folders, or ''."""
    kind = manifest.get("kind")
    if kind == "installer":
        return "esta versão exige o instalador completo"
    if kind != "bundle":
        return "esta versão não traz pacote de atualização automática"
    minimum = manifest.get("min_version")
    if minimum:
        try:
            if paths.parse_version(current_version) < paths.parse_version(minimum):
                return f"esta versão exige o instalador completo (mínimo {minimum})"
        except ValueError:
            return "manifesto com versão mínima inválida"
    return ""


def check_url(url, env=None):
    """HTTPS always; plain HTTP only to this machine in development or tests."""
    env = os.environ if env is None else env
    parsed = urlsplit(str(url))
    if parsed.username or parsed.password or not parsed.hostname:
        raise UpdateError("Endereço do pacote inválido")
    if parsed.scheme == "https":
        return url
    development = env.get("PDV_ENV", "production") in ("development", "test")
    if parsed.scheme == "http" and development and parsed.hostname in ("127.0.0.1", "localhost", "::1"):
        return url
    raise UpdateError("O pacote de atualização exige HTTPS")


# ----------------------------------------------------------------- download

class _SafeRedirect(urllib.request.HTTPRedirectHandler):
    def __init__(self, env):
        self.env = env

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        try:
            check_url(newurl, self.env)
        except UpdateError as exc:
            raise urllib.error.URLError(str(exc)) from exc
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def file_sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def verify_package(path, manifest):
    """Size first (cheap), then SHA-256. Raises UpdateError on any difference."""
    path = Path(path)
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise UpdateError("Pacote de atualização não encontrado") from exc
    if size != manifest["size_bytes"]:
        raise UpdateError("Tamanho do pacote diferente do manifesto")
    if file_sha256(path) != manifest["sha256"]:
        raise UpdateError("SHA-256 do pacote não confere com o manifesto")
    return path


def download(manifest, updates, *, env=None, opener=None, timeout=30, chunk=64 * 1024, progress=None):
    """Download (or resume) the package and return its verified path."""
    env = os.environ if env is None else env
    url = check_url(manifest["url"], env)
    size = manifest["size_bytes"]
    final = package_path(updates, manifest["version"])
    part = final.with_name(PACKAGE_NAME + ".part")
    final.parent.mkdir(parents=True, exist_ok=True)
    if final.exists():
        try:
            return verify_package(final, manifest)
        except UpdateError:
            final.unlink(missing_ok=True)
    offset = part.stat().st_size if part.exists() else 0
    if offset > size:
        part.unlink()
        offset = 0
    if offset < size:
        request = urllib.request.Request(url, headers={"User-Agent": "PDV2-Updater",
                                                       "Accept-Encoding": "identity"})
        if offset:
            request.add_header("Range", f"bytes={offset}-")
        opener = opener or urllib.request.build_opener(_SafeRedirect(env))
        try:
            response = opener.open(request, timeout=timeout)
        except urllib.error.HTTPError as exc:
            if exc.code == 416:
                # The server has fewer bytes than the manifest promised.
                part.unlink(missing_ok=True)
                raise UpdateError("Tamanho do pacote diferente do manifesto") from exc
            raise DownloadInterrupted(f"Servidor de atualização respondeu {exc.code}") from exc
        except (OSError, http.client.HTTPException) as exc:
            raise DownloadInterrupted("Sem conexão com o servidor de atualização") from exc
        with response:
            status = getattr(response, "status", None) or response.getcode()
            resumed = (offset and status == 206 and
                       str(response.headers.get("Content-Range", "")).startswith(f"bytes {offset}-"))
            if not resumed:
                if status != 200:
                    raise DownloadInterrupted(f"Servidor de atualização respondeu {status}")
                offset = 0
            with part.open("ab" if resumed else "wb") as stream:
                try:
                    while True:
                        block = response.read(chunk)
                        if not block:
                            break
                        offset += len(block)
                        if offset > size:
                            raise UpdateError("Tamanho do pacote diferente do manifesto")
                        stream.write(block)
                        if progress:
                            progress(offset)
                except (OSError, http.client.HTTPException) as exc:
                    raise DownloadInterrupted("Download interrompido; será retomado") from exc
                except UpdateError:
                    stream.close()
                    part.unlink(missing_ok=True)
                    raise
                finally:
                    if not stream.closed:
                        stream.flush()
                        os.fsync(stream.fileno())
        if offset < size:
            raise DownloadInterrupted("Download interrompido; será retomado")
    try:
        verify_package(part, manifest)
    except UpdateError:
        part.unlink(missing_ok=True)
        raise
    os.replace(part, final)
    return final


# --------------------------------------------------------------- extraction

def _bundle_members(archive, version):
    """Validate every entry before a single byte is written to disk."""
    prefix = paths.app_dir_name(version) + "/"
    members, seen, total = [], set(), 0
    infos = archive.infolist()
    if len(infos) > MAX_BUNDLE_FILES:
        raise UpdateError("Pacote com arquivos demais")
    for info in infos:
        name = info.filename
        if "\\" in name or ":" in name or "\x00" in name or name.startswith("/"):
            raise UpdateError("Pacote com caminho inválido: " + name[:80])
        parts = name.rstrip("/").split("/")
        if any(part in ("", ".", "..") or part != part.rstrip(" .") for part in parts):
            raise UpdateError("Pacote com caminho inválido: " + name[:80])
        if not (name + "/").startswith(prefix):
            raise UpdateError("Pacote com arquivo fora de " + prefix)
        mode = info.external_attr >> 16
        kind = stat.S_IFMT(mode)
        if kind == stat.S_IFLNK or kind not in (0, stat.S_IFREG, stat.S_IFDIR):
            raise UpdateError("Pacote com link ou arquivo especial: " + name[:80])
        key = name.rstrip("/").lower()
        if key in seen:
            raise UpdateError("Pacote com arquivo repetido: " + name[:80])
        seen.add(key)
        total += info.file_size
        if total > MAX_BUNDLE_BYTES:
            raise UpdateError("Pacote grande demais depois de extraído")
        members.append((info, parts, info.is_dir() or kind == stat.S_IFDIR))
    return members, total


def inspect_bundle(package, version, expected_format=None):
    """Structure and marker check without extracting. Returns the marker."""
    try:
        with zipfile.ZipFile(package) as archive:
            _bundle_members(archive, version)
            try:
                marker = json.loads(archive.read(f"{paths.app_dir_name(version)}/{BUNDLE_MARKER}"))
            except (KeyError, ValueError) as exc:
                raise UpdateError("Pacote sem identificação de versão") from exc
    except (zipfile.BadZipFile, OSError) as exc:
        raise UpdateError("Pacote de atualização corrompido") from exc
    if not isinstance(marker, dict) or marker.get("version") != version:
        raise UpdateError("Pacote não corresponde à versão do manifesto")
    if expected_format and marker.get("format") != expected_format:
        raise UpdateError("Pacote incompatível com esta instalação")
    return marker


def extract_bundle(package, version, install_root, expected_format=None):
    """Extract to a private folder and move into place only when complete."""
    install_root = Path(install_root)
    inspect_bundle(package, version, expected_format)
    name = paths.app_dir_name(version)
    target = install_root / name
    workdir = install_root / f".staging-{version}-{secrets.token_hex(4)}"
    try:
        with zipfile.ZipFile(package) as archive:
            members, total = _bundle_members(archive, version)
            if shutil.disk_usage(install_root).free < total + 50 * 1024 * 1024:
                raise UpdateError("Espaço em disco insuficiente para atualizar")
            for info, parts, is_dir in members:
                destination = workdir.joinpath(*parts)
                if is_dir:
                    destination.mkdir(parents=True, exist_ok=True)
                    continue
                destination.parent.mkdir(parents=True, exist_ok=True)
                written = 0
                with archive.open(info) as source, destination.open("wb") as output:
                    for block in iter(lambda: source.read(1024 * 1024), b""):
                        written += len(block)
                        if written > info.file_size:
                            raise UpdateError("Pacote de atualização corrompido")
                        output.write(block)
                    output.flush()
                    os.fsync(output.fileno())
                if (info.external_attr >> 16) & 0o111:
                    destination.chmod(0o755)
        if target.exists():
            shutil.rmtree(target)
        os.replace(workdir / name, target)
    except (zipfile.BadZipFile, OSError) as exc:
        raise UpdateError("Não foi possível extrair a atualização: " + str(exc)[:160]) from exc
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    return target


def cleanup_versions(install_root, keep):
    """Remove app-<version> folders and leftovers, keeping the listed versions."""
    keep = {paths.app_dir_name(version) for version in keep if version}
    removed = []
    for entry in Path(install_root).iterdir():
        stale = entry.name.startswith(".staging-") or (
            entry.name.startswith("app-") and paths.VERSION_RE.fullmatch(entry.name[4:])
            and entry.name not in keep)
        if stale and entry.is_dir():
            shutil.rmtree(entry, ignore_errors=True)
            removed.append(entry.name)
    return removed


def _cleanup_downloads(updates, keep=()):
    updates = Path(updates)
    if not updates.is_dir():
        return
    for entry in updates.iterdir():
        if entry.is_dir() and entry.name not in keep:
            shutil.rmtree(entry, ignore_errors=True)


# ------------------------------------------------------------ public status

def _cash_open(store):
    # Any computer of the shop: an update restarts the cashier all of them depend on.
    return getattr(store, "any_cash_open", lambda: store.cash_state() is not None)()


def _print_busy(store, now_ms=None):
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    for job in store.print_jobs():
        if job["status"] == "queued":
            return True
        # A job left in "sending" by a crash must not block updates forever.
        if job["status"] == "sending" and now_ms - int(job.get("updated_at") or 0) < PRINT_BUSY_MS:
            return True
    return False


def status(store, *, install_root=None, data_dir=None):
    ctx = Context(install_root, data_dir)
    current = ctx.running_version
    available = available_manifest(store, current)
    result = {"current_version": current, "available": available, "staged_version": None,
              "state": "idle", "error": "", "can_apply": False, "blocked_reason": "",
              "installed": ctx.installed, "previous_version": None, "downloaded_bytes": 0}
    if not ctx.installed:
        result["blocked_reason"] = DEV_REASON
        return result
    state = load_state(ctx.updates)
    result["state"] = state["state"]
    result["error"] = state.get("error") or ""
    result["previous_version"] = state.get("previous_version")
    staged = state.get("staged_version")
    if staged and package_path(ctx.updates, staged).exists():
        result["staged_version"] = staged
    if state["state"] == "applying":
        result["blocked_reason"] = "atualização em andamento; aguarde o reinício"
        return result
    if not available:
        result["blocked_reason"] = "nenhuma atualização disponível"
        return result
    part = package_path(ctx.updates, available["version"]).with_name(PACKAGE_NAME + ".part")
    if part.exists():
        result["downloaded_bytes"] = part.stat().st_size
    reason = manifest_block(available, current)
    if not reason and state.get("failed_version") == available["version"]:
        reason = f"a versão {available['version']} não iniciou e foi desfeita; aguarde uma nova versão"
    if not reason and result["staged_version"] != available["version"]:
        reason = "o pacote ainda está sendo baixado" if state["state"] == "downloading" \
            else "o pacote ainda não foi baixado"
    if not reason and _cash_open(store):
        reason = "há um caixa aberto; feche o caixa para atualizar"
    if not reason and _print_busy(store):
        reason = "há impressões na fila; aguarde terminar"
    result["blocked_reason"] = reason
    result["can_apply"] = not reason
    return result


# ---------------------------------------------------------------- auto step

def _retry_delay(attempts):
    return min(RETRY_MAX_SECONDS, RETRY_BASE_SECONDS * 2 ** max(0, attempts - 1))


def _download_job(ctx, manifest, now):
    version = manifest["version"]
    save_state(ctx.updates, state="downloading", version=version, error=None)
    try:
        download(manifest, ctx.updates, env=ctx.env)
        inspect_bundle(package_path(ctx.updates, version), version, ctx.bundle_format)
    except DownloadInterrupted as exc:
        save_state(ctx.updates, state="downloading", version=version, error=str(exc),
                   retry_at=now() + RESUME_RETRY_SECONDS)
        return
    except UpdateError as exc:
        shutil.rmtree(package_path(ctx.updates, version).parent, ignore_errors=True)
        attempts = int(load_state(ctx.updates).get("attempts") or 0) + 1
        save_state(ctx.updates, state="failed", version=version, error=str(exc), attempts=attempts,
                   staged_version=None, retry_at=now() + _retry_delay(attempts))
        return
    except Exception as exc:  # never let the worker thread die because of an update
        save_state(ctx.updates, state="failed", version=version,
                   error=f"{type(exc).__name__}: {exc}"[:200], retry_at=now() + RETRY_BASE_SECONDS)
        return
    _cleanup_downloads(ctx.updates, keep=(version,))
    save_state(ctx.updates, state="staged", version=version, staged_version=version, error=None,
               attempts=None, retry_at=None, staged_sha256=manifest["sha256"])


def _auto_apply_now(env, now):
    """Unattended apply only inside the quiet window (default 02:00-05:00 local)."""
    window = str(env.get("PDV_UPDATE_AUTO_APPLY", "2-5")).strip().lower()
    if window in ("0", "off", "no", "false", ""):
        return False
    try:
        start, end = (int(part) for part in window.split("-"))
    except ValueError:
        return False
    hour = time.localtime(now).tm_hour
    return start <= hour < end if start <= end else (hour >= start or hour < end)


def auto_step(store, online, *, block=False, install_root=None, data_dir=None, now=time.time):
    """Called by the worker on every pass. Downloads in a side thread; never raises."""
    global _download_thread
    try:
        ctx = Context(install_root, data_dir)
        if not ctx.installed:
            return
        state = load_state(ctx.updates)
        if state["state"] == "applying":
            return
        current = ctx.running_version
        manifest = available_manifest(store, current)
        if not manifest:
            if state["state"] != "idle" or state.get("staged_version"):
                # The release was withdrawn or is already installed.
                _cleanup_downloads(ctx.updates)
                _reset(ctx.updates, staged_version=None)
            return
        version = manifest["version"]
        if manifest_block(manifest, current) or state.get("failed_version") == version:
            return
        if state.get("staged_version") == version and package_path(ctx.updates, version).exists():
            if state.get("staged_sha256") != manifest["sha256"]:
                # Same version republished with other contents: download again.
                _cleanup_downloads(ctx.updates)
                _reset(ctx.updates, staged_version=None)
            elif _auto_apply_now(ctx.env, now()) and status(
                    store, install_root=ctx.install_root, data_dir=ctx.data_dir)["can_apply"]:
                request_apply(store, install_root=ctx.install_root, data_dir=ctx.data_dir)
            return
        if not online or float(state.get("retry_at") or 0) > now():
            return
        if state.get("version") not in (None, version):
            save_state(ctx.updates, attempts=None)
        if block:
            with _DOWNLOAD_LOCK:
                _download_job(ctx, manifest, now)
            return
        if _download_thread is not None and _download_thread.is_alive():
            return

        def job():
            with _DOWNLOAD_LOCK:
                _download_job(ctx, manifest, now)

        _download_thread = threading.Thread(target=job, name="pdv-updater-download", daemon=True)
        _download_thread.start()
    except Exception:
        return


# -------------------------------------------------------------------- apply

def _request_restart(ctx, version):
    """Leave a note for the service host, which restarts into the new pointer."""
    ctx.updates.mkdir(parents=True, exist_ok=True)
    temporary = ctx.updates / (RESTART_REQUEST + ".tmp")
    temporary.write_text(json.dumps({"version": version, "at": int(time.time())}), encoding="utf-8")
    os.replace(temporary, ctx.updates / RESTART_REQUEST)
    return "service" if ctx.env.get("PDV_SUPERVISED") == "1" else "manual"


def request_apply(store, *, install_root=None, data_dir=None, restart=None):
    """Apply the staged version if nothing is in use. Returns a dict, never raises."""
    ctx = Context(install_root, data_dir)
    if not _APPLY_LOCK.acquire(blocking=False):
        return {"ok": False, "error": "atualização em andamento; aguarde o reinício"}
    try:
        current = status(store, install_root=ctx.install_root, data_dir=ctx.data_dir)
        if not current["can_apply"]:
            return {"ok": False, "error": current["blocked_reason"], **_public(current)}
        manifest = current["available"]
        version = manifest["version"]
        previous = ctx.pointer_version
        package = package_path(ctx.updates, version)
        try:
            verify_package(package, manifest)
        except UpdateError as exc:
            shutil.rmtree(package.parent, ignore_errors=True)
            save_state(ctx.updates, state="failed", version=version, error=str(exc), staged_version=None,
                       retry_at=time.time() + RETRY_BASE_SECONDS)
            return {"ok": False, "error": str(exc)}
        try:
            backup = store.backup_now()
        except Exception as exc:
            message = "Backup do banco falhou; atualização cancelada: " + str(exc)[:160]
            save_state(ctx.updates, error=message)
            return {"ok": False, "error": message}
        # The cashier could have been opened while the backup was running.
        if _cash_open(store):
            return {"ok": False, "error": "há um caixa aberto; feche o caixa para atualizar"}
        try:
            extract_bundle(package, version, ctx.install_root, ctx.bundle_format)
        except UpdateError as exc:
            save_state(ctx.updates, state="failed", version=version, error=str(exc),
                       retry_at=time.time() + RETRY_BASE_SECONDS)
            return {"ok": False, "error": str(exc)}
        save_state(ctx.updates, state="applying", version=version, previous_version=previous,
                   error=None, backup=backup.get("name"), apply_started_at=int(time.time()))
        try:
            paths.write_pointer(ctx.install_root, version)
        except OSError as exc:
            shutil.rmtree(paths.app_dir(ctx.install_root, version), ignore_errors=True)
            message = "Sem permissão para trocar a versão: " + str(exc)[:160]
            save_state(ctx.updates, state="staged", error=message)
            return {"ok": False, "error": message}
        mode = (restart or _request_restart)(ctx, version)
        return {"ok": True, "version": version, "previous_version": previous, "restart": mode,
                "backup": backup.get("name"),
                "message": "Atualização aplicada; o PDV2 vai reiniciar em instantes." if mode == "service"
                else "Atualização aplicada; reinicie o PDV2 para concluir."}
    finally:
        _APPLY_LOCK.release()


def _public(current):
    return {key: current[key] for key in ("current_version", "staged_version", "state", "can_apply",
                                          "blocked_reason")}


# ------------------------------------------- next start: confirm or roll back

def pending_apply(install_root, data_dir):
    """State of an apply that still waits for its health confirmation, or None."""
    state = load_state(Path(data_dir) / "updates")
    if state["state"] != "applying" or state.get("version") != paths.read_pointer(install_root):
        return None
    return state


def confirm_apply(install_root, data_dir):
    """The new version answered /health: keep it and drop everything older."""
    updates = Path(data_dir) / "updates"
    state = load_state(updates)
    current = paths.read_pointer(install_root)
    cleanup_versions(install_root, keep=(current, state.get("previous_version")))
    _cleanup_downloads(updates)
    return save_state(updates, state="idle", version=None, error=None, staged_version=None,
                      staged_sha256=None, failed_version=None, attempts=None, retry_at=None,
                      apply_started_at=None, confirmed_version=current, confirmed_at=int(time.time()))


def rollback(install_root, data_dir, reason=""):
    """Point back to the previous version and remember which one failed."""
    updates = Path(data_dir) / "updates"
    state = load_state(updates)
    failed = paths.read_pointer(install_root)
    previous = state.get("previous_version")
    if not previous or previous == failed or not paths.app_dir(install_root, previous).is_dir():
        save_state(updates, state="failed",
                   error="Não há versão anterior instalada para voltar; reinstale o PDV2.")
        return None
    paths.write_pointer(install_root, previous)
    message = f"A versão {failed} não iniciou; o PDV2 voltou para a versão {previous}."
    if reason:
        message += " " + reason
    save_state(updates, state="failed", version=failed, failed_version=failed, error=message,
               staged_version=None, staged_sha256=None, apply_started_at=None, previous_version=None)
    _cleanup_downloads(updates)
    if failed:
        shutil.rmtree(paths.app_dir(install_root, failed), ignore_errors=True)
    return previous


def probe_health(url=None, timeout=2):
    """True when the local terminal answers /health as itself."""
    try:
        with urllib.request.urlopen(url or paths.health_url(), timeout=timeout) as response:
            body = json.loads(response.read(4096))
    except (OSError, ValueError, http.client.HTTPException):
        return False
    return isinstance(body, dict) and body.get("status") == "ok" and body.get("service") == "terminal-local"


def probe_responding(url=None, timeout=2):
    """True when the local terminal answers at all, healthy or in damaged-database mode.

    The launcher uses this: a terminal that started with a damaged database
    still serves the recovery screen, which the shopkeeper must be able to open.
    """
    try:
        with urllib.request.urlopen(url or paths.health_url(), timeout=timeout) as response:
            body = json.loads(response.read(4096))
    except urllib.error.HTTPError as error:
        try:
            body = json.loads(error.read(4096))
        except (OSError, ValueError):
            return False
    except (OSError, ValueError, http.client.HTTPException):
        return False
    return isinstance(body, dict) and body.get("service") == "terminal-local"


def await_health(probe, timeout=HEALTH_TIMEOUT, *, alive=lambda: True, interval=1.0,
                 sleep=time.sleep, clock=time.monotonic):
    """Poll until healthy, until the process dies or until the deadline."""
    deadline = clock() + timeout
    while True:
        if probe():
            return True
        if not alive() or clock() >= deadline:
            return False
        sleep(interval)


def confirm_or_rollback(install_root, data_dir, probe, timeout=HEALTH_TIMEOUT, **wait):
    """Run after a start. Returns 'confirmed', 'rolled_back' or None (nothing pending)."""
    if not pending_apply(install_root, data_dir):
        return None
    if await_health(probe, timeout, **wait):
        confirm_apply(install_root, data_dir)
        return "confirmed"
    rollback(install_root, data_dir)
    return "rolled_back"
