"""Pairing, bootstrap, outbox delivery and durable print worker."""

import json
import os
import threading
import time
from urllib.parse import quote, urlparse

import requests

from .printers import send_bytes
from .store import TerminalError

try:  # the automatic updater ships with the installed build; the terminal works without it
    from . import updater
except ImportError:
    updater = None

try:  # the second-disk copy is optional too; nothing here depends on it
    from . import mirror
except ImportError:
    mirror = None

# Phase 3 hooks, all optional: the network between cashiers, the copy over it and the process
# supervisor belong to other modules. Nothing here starts them; the cashier works without them.
try:
    from . import lan
except ImportError:
    lan = None

try:
    from . import replica_net
except ImportError:
    replica_net = None

try:
    from . import runtime
except ImportError:
    runtime = None


VERSION = os.environ.get("PDV_VERSION", "2.0.0")
# Tells the cloud this build accepts leases longer than the old 72 hours.
CAPABILITIES = "long-lease"
CAPABILITY_HEADERS = {"X-PDV-Capabilities": CAPABILITIES}
LEASE_RENEW_INTERVAL_MS = 60 * 60 * 1000
# The cloud takes at most 50 events and 1 MB per request; stay well below it.
MAX_BATCH_EVENTS = 50
MAX_BATCH_BYTES = 512 * 1024
RESTORE_PAGE_SIZE = 200
# A computer that was replaced asks now and then whether the hand-over was given up.
TRANSFER_PROBE_SECONDS = 5 * 60
# One restore pass at a time, whoever asks (the worker or the recovery screen).
_RESTORE_LOCK = threading.Lock()


def validate_cloud_url(url):
    parsed = urlparse(str(url))
    if parsed.username or parsed.password or parsed.path not in ("", "/") or parsed.query or parsed.fragment:
        raise TerminalError("URL do servidor inválida")
    if parsed.scheme == "https" and parsed.hostname:
        return url.rstrip("/")
    env = os.environ.get("PDV_ENV", "production")
    local = parsed.hostname in ("localhost", "127.0.0.1", "::1")
    dev_host = parsed.hostname in ("cloud", "host.docker.internal")
    if parsed.scheme == "http" and (local or (env in ("development", "test") and dev_host)):
        return url.rstrip("/")
    raise TerminalError("Servidor remoto exige HTTPS; HTTP de desenvolvimento requer host permitido")


def _json_response(response):
    try:
        body = response.json()
    except ValueError as exc:
        error = TerminalError("Servidor retornou JSON inválido", 502)
        error.remote_status = response.status_code  # a proxy may answer 413 in HTML
        raise error from exc
    if not response.ok:
        reason = body.get("error") if isinstance(body, dict) else None
        error = TerminalError(f"Servidor recusou pedido ({response.status_code}): {reason or 'erro'}", 502)
        error.remote_status = response.status_code
        # Stable code of the cloud (terminal_transferred, invalid_cursor...) and the whole answer.
        error.remote_code = body.get("code") if isinstance(body, dict) else None
        error.remote_body = body
        raise error
    if not isinstance(body, dict):
        raise TerminalError("Resposta inesperada do servidor", 502)
    return body


def _request(method, url, **kwargs):
    try:
        return _json_response(requests.request(method, url, timeout=12, **kwargs))
    except requests.RequestException as exc:
        raise TerminalError("Sem conexão com o servidor; dados locais preservados", 503) from exc


def _auth_headers(meta):
    return {"Authorization": "Bearer " + meta["token"], **CAPABILITY_HEADERS}


def _note_transfer(store, exc):
    """The cloud answered that this terminal now lives on another computer."""
    if getattr(exc, "remote_code", None) != "terminal_transferred":
        return False
    body = getattr(exc, "remote_body", None)
    store.mark_transferred(body.get("send_only_until") if isinstance(body, dict) else None)
    return True


def pair(store, cloud_url, license_key, code):
    cloud_url = validate_cloud_url(cloud_url)
    if not isinstance(license_key, str) or not 8 <= len(license_key.strip()) <= 160:
        raise TerminalError("Chave da licença inválida")
    if not isinstance(code, str) or not 4 <= len(code.strip()) <= 100:
        raise TerminalError("Código de pareamento inválido")
    # Reject a reused local database before consuming the cloud's one-time code.
    store.ensure_pairable()
    response = _request("POST", cloud_url + "/api/v1/terminal/pair", headers=dict(CAPABILITY_HEADERS),
                        json={"license_key": license_key.strip(), "code": code.strip(),
                              "device_id": store.identity()["device_id"],
                              "version": VERSION})
    store.set_pairing(response, cloud_url)
    return refresh_bootstrap(store)


def release_terminal(store):
    """"Desistir da recuperação": ask the cloud to give the terminal back to the computer it
    was taken from. Best effort: True only when the cloud undid the hand-over."""
    meta = store.meta()
    if not meta.get("token") or not meta.get("cloud_url"):
        return False
    try:
        response = _request("POST", meta["cloud_url"] + "/api/v1/terminal/release",
                            headers=_auth_headers(meta), json={})
    except TerminalError:
        return False  # offline, or a cloud without the route: the local way out still works
    return response.get("released") is True


def regularize_promotion(store, cloud_url, license_key, code):
    """After an additional cashier took over: the cloud hands the same terminal to this computer.

    Same call as a recovery pairing ("Trocar de computador" in the panel), but the data is
    already here, so nothing is cleared and no restore starts.
    """
    cloud_url = validate_cloud_url(cloud_url)
    if not isinstance(license_key, str) or not 8 <= len(license_key.strip()) <= 160:
        raise TerminalError("Chave da licença inválida")
    if not isinstance(code, str) or not 4 <= len(code.strip()) <= 100:
        raise TerminalError("Código de pareamento inválido")
    # Refuse before spending the cloud's one-time code.
    if store.promotion_status()["state"] != "awaiting_cloud":
        raise TerminalError("Este caixa não está aguardando a regularização com a nuvem.", 409)
    response = _request("POST", cloud_url + "/api/v1/terminal/pair", headers=dict(CAPABILITY_HEADERS),
                        json={"license_key": license_key.strip(), "code": code.strip(),
                              "device_id": store.promotion_device_id(),
                              "version": VERSION})
    try:
        store.adopt_pairing(response, cloud_url)
    except TerminalError as exc:
        token = response.get("token")
        if getattr(exc, "code", None) == "other_terminal" and isinstance(token, str) and token:
            # The code belonged to another terminal and the cloud has just moved that one here:
            # give it back to its computer (best effort) before telling the person.
            try:
                _request("POST", cloud_url + "/api/v1/terminal/release",
                         headers={"Authorization": "Bearer " + token, **CAPABILITY_HEADERS}, json={})
            except TerminalError:
                pass
        raise
    result = {"regularized": True, "bootstrap": None}
    try:
        result["bootstrap"] = refresh_bootstrap(store)
    except TerminalError as exc:
        # The pairing is saved and the code is spent: the worker repeats the first load.
        result["warning"] = str(exc)
    return result


def refresh_bootstrap(store):
    try:
        return _refresh_bootstrap(store)
    except TerminalError as exc:
        _note_transfer(store, exc)
        raise


def _refresh_bootstrap(store):
    meta = store.meta()
    if not meta.get("token") or not meta.get("cloud_url"):
        raise TerminalError("Terminal ainda não pareado", 409)
    headers = _auth_headers(meta)
    first = None
    products = []
    cursor = None
    seen = set()
    while True:
        url = meta["cloud_url"] + "/api/v1/terminal/bootstrap?limit=500"
        if cursor is not None:
            if cursor in seen:
                raise TerminalError("Paginação de catálogo repetida", 502)
            seen.add(cursor)
            url += "&cursor=" + str(cursor)
        page = _request("GET", url, headers=headers)
        if first is None:
            first = page
        elif (page.get("public_key") != first.get("public_key") or
              page.get("operators") != first.get("operators")):
            raise TerminalError("Catálogo mudou durante a carga; tente novamente", 502)
        if not isinstance(page.get("products"), list):
            raise TerminalError("Página de catálogo inválida", 502)
        products.extend(page["products"])
        cursor = page.get("next_cursor")
        if cursor is None:
            break
        if len(products) > 100000:
            raise TerminalError("Catálogo excede o limite do terminal", 502)
    first["products"] = products
    result = store.apply_bootstrap(first)
    store.clear_sync_error()
    try:
        check_release(store)
    except TerminalError as exc:
        # Release metadata is advisory. An outage never prevents a renewed lease.
        store.set_release_result(error=str(exc))
    return result


def renew_lease(store, force=False):
    """Renew the offline lease on its own route, which does not depend on the catalog.

    At most once an hour unless forced. Returns None when it was not due or the
    cloud does not have the route yet (the bootstrap keeps renewing in that case).
    """
    meta = store.meta()
    if not meta.get("token") or not meta.get("cloud_url"):
        raise TerminalError("Terminal ainda não pareado", 409)
    if not force and int(time.time() * 1000) - int(meta.get("lease_renewed_at") or 0) < LEASE_RENEW_INTERVAL_MS:
        return None
    try:
        response = _request("POST", meta["cloud_url"] + "/api/v1/terminal/lease", headers=_auth_headers(meta),
                            json={"version": VERSION, "client_time": int(time.time())})
    except TerminalError as exc:
        if getattr(exc, "remote_status", None) in (404, 405):
            store.mark_lease_attempt()
            return None
        _note_transfer(store, exc)
        raise
    if "lease" not in response:
        store.mark_lease_attempt()
        return None
    return store.apply_lease(response)


def check_release(store):
    """Verify signed metadata with the pinned release key (the license key on older pairings)."""
    from shared.licensing import LicenseError
    from shared.releases import verify_release

    meta = store.meta()
    if not meta.get("cloud_url") or not meta.get("public_key"):
        raise TerminalError("Terminal não pareado", 409)
    response = _request("GET", meta["cloud_url"] +
                        "/api/v1/ops/releases/current?component=terminal&channel=stable&current=" + VERSION)
    release_key = meta.get("release_public_key") or meta["public_key"]
    if response.get("public_key") and response["public_key"] != release_key:
        raise TerminalError("Chave de atualização diferente da chave fixada", 403)
    envelope = response.get("release")
    if envelope is None:
        store.set_release_result()
        return None
    try:
        payload = verify_release(envelope, release_key)
    except LicenseError as exc:
        raise TerminalError("Manifesto de atualização sem assinatura válida", 403) from exc
    if payload.get("component") != "terminal" or payload.get("channel") != "stable":
        raise TerminalError("Manifesto de atualização não corresponde ao terminal", 403)
    store.set_release_result(payload)
    return payload


def _fit_batch(events, limit=MAX_BATCH_BYTES):
    """Longest prefix of the queue that fits the request size (always one event)."""
    batch, size = [], 0
    for event in events:
        size += len(json.dumps(event)) + 1  # same encoding requests uses for json=
        if batch and size > limit:
            break
        batch.append(event)
    return batch


def sync_once(store):
    """Acknowledges only a complete, ordered server response after cloud commit."""
    events = _fit_batch(store.pending_events(MAX_BATCH_EVENTS))
    if not events:
        # Nothing left to deliver: an old delivery error must not outlive a cloud
        # that is answering (the heartbeat is the contact made with an empty queue).
        status = store.sync_status()
        if status["last_sync_error"] and status["last_heartbeat_at"] and not status["last_heartbeat_error"]:
            store.clear_sync_error()
        return {"sent": 0, "remaining": 0}
    meta = store.meta()
    if not meta.get("token") or not meta.get("cloud_url"):
        raise TerminalError("Terminal ainda não pareado", 409)
    while True:
        try:
            response = _request("POST", meta["cloud_url"] + "/api/v1/terminal/sync",
                                headers=_auth_headers(meta),
                                json={"events": events})
            break
        except TerminalError as exc:
            if getattr(exc, "remote_status", None) == 413 and len(events) > 1:
                events = events[:len(events) // 2]  # body too large: retry with half
                continue
            _note_transfer(store, exc)  # send-only period over: the queue stays here, untouched
            store.set_sync_result(str(exc))
            raise
    if response.get("send_only") is True:
        # Old computer of a transferred terminal: the cloud still takes its queue for a while.
        store.mark_transferred(response.get("send_only_until"))
    elif response.get("send_only") is False:
        store.clear_transferred()  # the hand-over was given up: this is the cashier again
    expected = [event["event_id"] for event in events]
    if response.get("accepted_event_ids") != expected:
        store.set_sync_result("Confirmação incompleta do servidor")
        raise TerminalError("Servidor não confirmou o lote completo; reenvio será tentado", 502)
    store.record_sync_conflicts(response.get("conflicts") or [])
    # Quarantined events were received by the cloud and wait for review there.
    # They are acknowledged like the others so one bad event never blocks the queue.
    quarantined = store.record_quarantined(response.get("quarantined"), events)
    store.ack_events(expected)
    store.set_sync_result()
    result = {"sent": len(expected), "remaining": store.sync_status()["pending"]}
    if response.get("conflicts"):
        result["conflicts"] = response["conflicts"]
    if quarantined:
        result["quarantined"] = quarantined
    return result


def heartbeat_once(store):
    """Advertise terminal presence even when the outbox is empty."""
    meta = store.meta()
    if not meta.get("token") or not meta.get("cloud_url"):
        raise TerminalError("Terminal ainda não pareado", 409)
    status = store.sync_status()
    last_error = status.get("last_sync_error") or ""
    if not last_error and status.get("catalog_skipped"):
        last_error = f"Catálogo: {status['catalog_skipped']} item(ns) ignorado(s) pelo terminal"
    try:
        response = _request("POST", meta["cloud_url"] + "/api/v1/terminal/heartbeat",
                            headers=_auth_headers(meta),
                            json={"version": VERSION, "pending_events": status["pending"],
                                  "last_error": last_error[:300],
                                  "client_time": int(time.time())})
    except TerminalError as exc:
        _note_transfer(store, exc)
        store.set_heartbeat_result(str(exc))
        raise
    store.set_heartbeat_result()
    store.clear_transferred()  # only the current computer of the terminal gets an answer here
    store.clear_sync_error()
    return response


def restore_step(store, max_pages=20):
    """Advance the recovery of the shop on a new computer. Returns store.restore_status().

    Safe to call from the worker and from the recovery screen at the same time, and
    again after any failure: the cursor kept in the database says where to resume.
    """
    if not _RESTORE_LOCK.acquire(blocking=False):
        return store.restore_status()
    try:
        _restore_run(store, max_pages)
    except TerminalError as exc:
        # No connection, cloud refusing, bad page: nothing is lost, the next pass tries again.
        _note_transfer(store, exc)
        store.set_restore_error(str(exc))
    finally:
        _RESTORE_LOCK.release()
    return store.restore_status()


def _restore_run(store, max_pages):
    status = store.restore_status()
    if not status["active"] or status["paused"]:
        return
    if status["state"] == "importing":
        # The queue read from the backup copy goes up first; then everything comes down again.
        for _ in range(40):
            if not sync_once(store)["remaining"]:
                break
        if not store.restore_second_pass():
            return
    elif status["state"] not in ("pending", "restoring"):
        return  # awaiting_backup: waits for the person's answer
    meta = store.meta()
    if not meta.get("token") or not meta.get("cloud_url"):
        raise TerminalError("Terminal ainda não pareado", 409)
    for _ in range(max(1, int(max_pages))):
        cursor = store.meta().get("restore_cursor") or ""
        url = (meta["cloud_url"] + f"/api/v1/terminal/restore?limit={RESTORE_PAGE_SIZE}&cursor="
               + quote(cursor, safe=""))
        try:
            page = _request("GET", url, headers=_auth_headers(meta))
        except TerminalError as exc:
            if getattr(exc, "remote_code", None) != "invalid_cursor" or not cursor:
                raise
            # The cloud no longer accepts this position: start over, counting one attempt.
            if store.restore_restart()["paused"]:
                return
            continue
        result = store.apply_restore(page, cursor)
        if result.get("skipped") or result["state"] != "restoring" or store.restore_status()["paused"]:
            return


def print_once(store):
    job = store.begin_print()
    if not job:
        return None
    try:
        destination = send_bytes(job["driver"], job["target"], job["payload"], job["id"])
    except Exception as exc:
        store.finish_print(job["id"], error=exc)
        return {"id": job["id"], "status": "uncertain", "error": str(exc)}
    store.finish_print(job["id"])
    return {"id": job["id"], "status": "printed", "destination": destination}


def _describe(exc):
    """Operator-facing text; unexpected errors keep their type for diagnosis."""
    return str(exc) if isinstance(exc, TerminalError) else f"{type(exc).__name__}: {exc}"


class Worker:
    """Single background loop for print spool and eventual cloud synchronization."""

    def __init__(self, store, interval=5):
        self.store = store
        self.interval = interval
        self.stop_event = threading.Event()
        self.transfer_probe_at = None
        self.thread = self._new_thread()

    def _new_thread(self):
        return threading.Thread(target=self.run, name="pdv-terminal-worker", daemon=True)

    def start(self):
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread.is_alive():
            self.thread.join(timeout=5)

    def ensure_running(self):
        """Start a new thread if the loop died without being asked to stop."""
        if self.stop_event.is_set() or self.thread.is_alive() or self.thread.ident is None:
            return False
        self.thread = self._new_thread()
        self.thread.start()
        return True

    def run_once(self):
        """One pass over every duty. A failing step never prevents the next ones."""
        errors = []
        try:
            meta = self.store.meta()
            overdue = int(time.time() * 1000) - int(meta.get("last_backup_at") or 0) >= 24 * 60 * 60 * 1000
            # Nothing to protect before the first local record; a copy made right
            # after pairing would only block correcting a wrong pairing.
            # The same holds while a recovery is bringing back what the cloud already keeps.
            if (meta.get("terminal_id") and overdue and self.store.has_local_activity()
                    and not self.store.restore_status()["active"]):
                self.store.backup_now()
        except Exception as exc:
            errors.append("Backup local: " + _describe(exc))
        try:
            while print_once(self.store):
                pass
        except Exception as exc:
            errors.append(_describe(exc))
        try:
            paired = bool(self.store.meta().get("token"))
        except Exception as exc:
            errors.append(_describe(exc))
            paired = False
        online = False
        try:
            transferred = paired and self.store.is_transferred()
        except Exception as exc:
            errors.append(_describe(exc))
            transferred = False
        if transferred:
            # This terminal moved to another computer. All that is left to do here is
            # deliver the queue, for as long as the cloud still takes it.
            try:
                # Once the cloud closed the send-only period, repeating the upload changes nothing.
                if self.store.sync_status()["pending"] and not self.store.send_only_closed():
                    result = sync_once(self.store)
                    if result["remaining"]:
                        self.stop_event.wait(0.1)
            except Exception as exc:
                errors.append(_describe(exc))
            try:
                self._probe_transfer()
            except Exception as exc:
                errors.append(_describe(exc))
            return errors
        if paired:
            try:
                heartbeat_at = int(self.store.meta().get("last_heartbeat_at") or 0)
                if int(time.time() * 1000) - heartbeat_at > 30 * 1000:
                    heartbeat_once(self.store)
                meta = self.store.meta()
                online = bool(meta.get("last_heartbeat_at")) and not meta.get("last_heartbeat_error")
            except Exception as exc:
                errors.append("Heartbeat: " + _describe(exc))
            try:
                # Every contact that worked renews the lease and re-anchors the clock.
                if online:
                    renew_lease(self.store)
            except Exception as exc:
                errors.append("Licença: " + _describe(exc))
            try:
                # A new computer brings the shop's data back before it may sell.
                if self.store.restore_status()["active"]:
                    restored = restore_step(self.store)
                    if restored["error"]:
                        errors.append("Recuperação: " + restored["error"])
            except Exception as exc:
                errors.append("Recuperação: " + _describe(exc))
            try:
                # Printing or backup-copy setup changed: tell the cloud, so it can come back later.
                self.store.ensure_config_event()
            except Exception as exc:
                errors.append(_describe(exc))
            try:
                result = sync_once(self.store)
                if result["remaining"]:
                    self.stop_event.wait(0.1)
            except Exception as exc:
                errors.append(_describe(exc))
            try:
                bootstrap_at = int(self.store.meta().get("bootstrap_at") or 0)
                # Keep the local catalog close to the cloud so product
                # edits and stock receipts reach an open cashier quickly.
                # Pending local events are preserved by Store.apply_bootstrap.
                if int(time.time() * 1000) - bootstrap_at > 30 * 1000:
                    refresh_bootstrap(self.store)
            except Exception as exc:
                errors.append(_describe(exc))
        try:
            # Keeps the anti-rollback mark moving while no browser is asking for the license.
            self.store.lease_status()
        except Exception as exc:
            errors.append(_describe(exc))
        if updater is not None and paired:
            try:
                updater.auto_step(self.store, online)
            except Exception as exc:
                errors.append("Atualização: " + _describe(exc))
        return errors

    def _probe_transfer(self):
        """"Desistir da recuperação" on the new computer gives the terminal back to this one:
        a heartbeat that is answered means so (heartbeat_once clears the mark). The expected
        refusal only refreshes the send-only date."""
        now = time.monotonic()
        if self.transfer_probe_at is not None and now - self.transfer_probe_at < TRANSFER_PROBE_SECONDS:
            return
        self.transfer_probe_at = now
        try:
            heartbeat_once(self.store)
        except TerminalError as exc:
            if getattr(exc, "remote_code", None) != "terminal_transferred" and exc.status != 503:
                raise

    def run(self):
        failures = 0
        while not self.stop_event.is_set():
            # This thread is the only one that syncs, renews the license and
            # prints: no exception may end it (a locked database, for example).
            try:
                errors = self.run_once()
                if errors:
                    self.store.set_sync_result("; ".join(errors)[:300])
            except Exception:
                errors = ["Falha interna do sincronizador"]
            failures = min(failures + 1, 6) if errors else 0
            self.stop_event.wait(max(self.interval, min(60, self.interval * 2 ** failures)))


def watch_worker(current_worker, interval=30, stop_event=None):
    """Background check that revives the worker thread if it ever dies.

    `current_worker` returns the active Worker (it changes when the profile does).
    """
    stop_event = stop_event or threading.Event()

    def loop():
        while not stop_event.wait(interval):
            try:
                worker = current_worker()
                if worker is not None:
                    worker.ensure_running()
            except Exception:
                pass

    thread = threading.Thread(target=loop, name="pdv-terminal-watchdog", daemon=True)
    thread.start()
    return stop_event
