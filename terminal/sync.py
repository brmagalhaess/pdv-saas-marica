"""Pairing, bootstrap, outbox delivery and durable print worker."""

import json
import os
import threading
import time
from urllib.parse import urlparse

import requests

from .printers import send_bytes
from .store import TerminalError


VERSION = os.environ.get("PDV_VERSION", "2.0.0")
# The cloud takes at most 50 events and 1 MB per request; stay well below it.
MAX_BATCH_EVENTS = 50
MAX_BATCH_BYTES = 512 * 1024


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
        raise error
    if not isinstance(body, dict):
        raise TerminalError("Resposta inesperada do servidor", 502)
    return body


def _request(method, url, **kwargs):
    try:
        return _json_response(requests.request(method, url, timeout=12, **kwargs))
    except requests.RequestException as exc:
        raise TerminalError("Sem conexão com o servidor; dados locais preservados", 503) from exc


def pair(store, cloud_url, license_key, code):
    cloud_url = validate_cloud_url(cloud_url)
    if not isinstance(license_key, str) or not 8 <= len(license_key.strip()) <= 160:
        raise TerminalError("Chave da licença inválida")
    if not isinstance(code, str) or not 4 <= len(code.strip()) <= 100:
        raise TerminalError("Código de pareamento inválido")
    # Reject a reused local database before consuming the cloud's one-time code.
    store.ensure_pairable()
    response = _request("POST", cloud_url + "/api/v1/terminal/pair",
                        json={"license_key": license_key.strip(), "code": code.strip(),
                              "device_id": store.identity()["device_id"],
                              "version": VERSION})
    store.set_pairing(response, cloud_url)
    return refresh_bootstrap(store)


def refresh_bootstrap(store):
    meta = store.meta()
    if not meta.get("token") or not meta.get("cloud_url"):
        raise TerminalError("Terminal ainda não pareado", 409)
    headers = {"Authorization": "Bearer " + meta["token"]}
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


def check_release(store):
    """Verify signed metadata using the public key pinned at device pairing."""
    from shared.licensing import LicenseError
    from shared.releases import verify_release

    meta = store.meta()
    if not meta.get("cloud_url") or not meta.get("public_key"):
        raise TerminalError("Terminal não pareado", 409)
    response = _request("GET", meta["cloud_url"] +
                        "/api/v1/ops/releases/current?component=terminal&channel=stable")
    if response.get("public_key") and response["public_key"] != meta["public_key"]:
        raise TerminalError("Chave de atualização diferente da chave fixada", 403)
    envelope = response.get("release")
    if envelope is None:
        store.set_release_result()
        return None
    try:
        payload = verify_release(envelope, meta["public_key"])
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
                                headers={"Authorization": "Bearer " + meta["token"]},
                                json={"events": events})
            break
        except TerminalError as exc:
            if getattr(exc, "remote_status", None) == 413 and len(events) > 1:
                events = events[:len(events) // 2]  # body too large: retry with half
                continue
            store.set_sync_result(str(exc))
            raise
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
                            headers={"Authorization": "Bearer " + meta["token"]},
                            json={"version": VERSION, "pending_events": status["pending"],
                                  "last_error": last_error[:300],
                                  "client_time": int(time.time())})
    except TerminalError as exc:
        store.set_heartbeat_result(str(exc))
        raise
    store.set_heartbeat_result()
    store.clear_sync_error()
    return response


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
            if meta.get("terminal_id") and overdue and self.store.has_local_activity():
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
        if paired:
            try:
                heartbeat_at = int(self.store.meta().get("last_heartbeat_at") or 0)
                if int(time.time() * 1000) - heartbeat_at > 30 * 1000:
                    heartbeat_once(self.store)
            except Exception as exc:
                errors.append("Heartbeat: " + _describe(exc))
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
        return errors

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
