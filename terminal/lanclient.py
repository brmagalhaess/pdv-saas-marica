"""Pinned TLS client of a station. Standard library only.

The server certificate is self-signed, so there is no chain to check. What is
checked is the SHA-256 of the certificate itself, fixed when the station was
linked. The comparison happens right after the handshake and BEFORE any request
byte is written: the station token never reaches an impostor.
"""

import hashlib
import hmac
import http.client
import json
import select
import socket
import ssl
import threading
import time

LAN_API = 1
CODES = ("unreachable", "timeout", "tls", "fingerprint_mismatch")
# The server drops a connection idle for 30 s; never reuse one that is close to that.
MAX_IDLE_SECONDS = 20.0
RETRY_METHODS = ("GET", "HEAD")
# Sent only by the background agents of a station (monitor, print agent, replica puller). The
# local bridge drops every X-PDV-Station* header that comes from the browser, so a page can
# never reach a native /lan/v1 route of the server through it.
NATIVE_HEADER = ("X-PDV-Station-Native", "1")


def generation_proof(key, challenge, terminal_id, generation, fingerprint):
    """Proof that whoever answers as a newer generation of a terminal holds the key of the shop
    copy (only the main cashier and the cashiers that kept its copy have it). Hex, or None."""
    if not isinstance(key, str) or not key or not isinstance(challenge, str) or not challenge:
        return None
    message = "|".join(("pdv2-generation", challenge, str(terminal_id or ""), str(int(generation)),
                        str(fingerprint or "")))
    return hmac.new(key.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).hexdigest()


class LanError(OSError):
    """Network failure talking to the shop server.

    code: "unreachable" (nothing was sent), "timeout" (sent, and no answer came in time
    or the connection dropped before the answer), "tls", "fingerprint_mismatch".
    sent: True when request bytes may have reached the server.
    kind: "refused", "timeout" or "" (used only for the diagnosis on screen).
    """

    def __init__(self, code, message="", *, sent=False, kind=""):
        super().__init__(message or code)
        self.code = code
        self.sent = sent
        self.kind = kind


def _context():
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE  # trust comes from the pinned fingerprint, checked below
    return context


def _handshake(address, port, timeout):
    """Connected TLS socket and the fingerprint of the certificate it presented."""
    try:
        raw = socket.create_connection((address, int(port)), timeout=timeout)
    except (socket.timeout, TimeoutError) as exc:
        raise LanError("unreachable", "sem resposta", kind="timeout") from exc
    except ConnectionRefusedError as exc:
        raise LanError("unreachable", "conexão recusada", kind="refused") from exc
    except (OSError, ValueError, OverflowError) as exc:
        raise LanError("unreachable", type(exc).__name__) from exc
    try:
        raw.settimeout(timeout)
        tls = _context().wrap_socket(raw)
        certificate = tls.getpeercert(binary_form=True)
    except (socket.timeout, TimeoutError) as exc:
        raw.close()
        raise LanError("unreachable", "sem resposta", kind="timeout") from exc
    except (ssl.SSLError, OSError) as exc:
        raw.close()
        raise LanError("tls", type(exc).__name__) from exc
    if not certificate:
        tls.close()
        raise LanError("tls", "sem certificado")
    return tls, hashlib.sha256(certificate).hexdigest()


def _dropped(sock):
    """True when an idle connection was closed by the server (or has unexpected bytes).

    Checked before reuse, so a request is never written into a dead connection: a POST
    that cannot be delivered must fail as "nothing was sent", not as "maybe it arrived".
    """
    try:
        if sock.pending():
            return True
        readable, _writable, _errors = select.select([sock], [], [], 0)
        return bool(readable)
    except (OSError, ValueError):
        return True


def peer_fingerprint(address, port, timeout=5.0):
    """Fingerprint presented at an address. Only for the first contact (hello/enroll)."""
    tls, fingerprint = _handshake(address, port, timeout)
    try:
        tls.close()
    except OSError:
        pass
    return fingerprint


class LanResponse:
    def __init__(self, response, release):
        self._response = response
        self._release = release
        self.status = response.status
        self.header_list = list(response.getheaders())
        self.headers = {}
        for name, value in self.header_list:
            self.headers.setdefault(name.lower(), value)
        self._done = False

    def read(self, amount=-1):
        try:
            data = self._response.read() if amount is None or amount < 0 else self._response.read(amount)
        except (socket.timeout, TimeoutError) as exc:
            self._finish(False)
            raise LanError("timeout", "sem resposta", sent=True, kind="timeout") from exc
        except (OSError, http.client.HTTPException) as exc:
            self._finish(False)
            raise LanError("timeout", type(exc).__name__, sent=True) from exc
        if self._response.isclosed():
            self._finish(True)
        return data

    def json(self):
        try:
            return json.loads(self.read())
        except ValueError:
            return None

    def _finish(self, reusable):
        if not self._done:
            self._done = True
            self._release(reusable and not self._response.will_close)

    def close(self):
        """Safe at any point; an answer left unread costs the connection."""
        self._finish(self._response.isclosed())
        try:
            self._response.close()
        except OSError:
            pass


class LanClient:
    """One server, one pinned fingerprint. Persistent connections, one per thread."""

    def __init__(self, address, port, fingerprint, token=None, *, timeout=10.0, connect_timeout=3.0):
        self.address = str(address)
        self.port = int(port)
        self.fingerprint = str(fingerprint or "").lower()
        self.token = token
        self.timeout = timeout
        self.connect_timeout = min(connect_timeout, timeout) if timeout else connect_timeout
        self._local = threading.local()
        self._all = set()
        self._lock = threading.Lock()

    # -- connections
    def _connect(self):
        tls, presented = _handshake(self.address, self.port, self.connect_timeout)
        if not hmac.compare_digest(presented.encode("ascii"), self.fingerprint.encode("ascii", "replace")):
            try:
                tls.close()
            except OSError:
                pass
            raise LanError("fingerprint_mismatch", "o servidor apresentou outra identidade")
        connection = http.client.HTTPConnection(self.address, self.port)
        # Never let http.client open a socket by itself: it would be plain TCP, with no TLS and no
        # fingerprint check, and the token would leave in the clear. A closed connection fails.
        connection.auto_open = 0
        connection.sock = tls
        with self._lock:
            self._all.add(connection)
        return connection

    def _drop(self, connection):
        with self._lock:
            self._all.discard(connection)
        try:
            connection.close()
        except OSError:
            pass
        if getattr(self._local, "connection", None) is connection:
            self._local.connection = None

    def _acquire(self):
        connection = getattr(self._local, "connection", None)
        if connection is not None:
            idle = time.monotonic() - getattr(self._local, "used_at", 0.0)
            if (getattr(self._local, "busy", False) or connection.sock is None or idle > MAX_IDLE_SECONDS
                    or _dropped(connection.sock)):
                self._drop(connection)
                connection = None
        if connection is None:
            connection = self._connect()
            self._local.connection = connection
        return connection

    def _release(self, connection, reusable):
        self._local.busy = False
        self._local.used_at = time.monotonic()
        if not reusable:
            self._drop(connection)

    # -- requests
    def request(self, method, path, *, body=None, headers=None, timeout=None):
        method = method.upper()
        headers = list(headers.items()) if isinstance(headers, dict) else list(headers or [])
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers.append(("Content-Type", "application/json"))
        elif isinstance(body, str):
            body = body.encode("utf-8")
        names = {name.lower() for name, _value in headers}
        if "host" not in names:
            headers.append(("Host", f"{self.address}:{self.port}"))
        if self.token and "x-pdv-station-token" not in names:
            headers.append(("X-PDV-Station-Token", self.token))
        if "x-pdv-station-api" not in names:
            headers.append(("X-PDV-Station-Api", str(LAN_API)))
        if "accept-encoding" not in names:
            headers.append(("Accept-Encoding", "identity"))
        attempts = 2 if method in RETRY_METHODS else 1
        for attempt in range(attempts):
            connection = self._acquire()
            try:
                return self._send(connection, method, path, body, headers,
                                  self.timeout if timeout is None else timeout)
            except LanError as exc:
                self._release(connection, False)
                # A GET repeats once when the connection was dropped. A POST never repeats here:
                # the screen repeats it with the same request_id.
                if attempt + 1 < attempts and exc.kind != "timeout":
                    continue
                raise
        raise LanError("unreachable")  # pragma: no cover

    def _send(self, connection, method, path, body, headers, timeout):
        self._local.busy = True
        sent = False
        try:
            connection.sock.settimeout(timeout)
            connection.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
            for name, value in headers:
                if name.lower() != "content-length":
                    connection.putheader(name, value)
            if body is not None or method in ("POST", "PUT", "PATCH"):
                connection.putheader("Content-Length", str(len(body or b"")))
            sent = True
            connection.endheaders(body or None)
            response = connection.getresponse()
        except (socket.timeout, TimeoutError) as exc:
            raise LanError("timeout", "sem resposta", sent=True, kind="timeout") from exc
        except (http.client.NotConnected, AttributeError) as exc:
            # The connection was closed by another thread (close()) before a byte left.
            raise LanError("unreachable", type(exc).__name__, sent=False) from exc
        except (OSError, http.client.HTTPException, ValueError) as exc:
            raise LanError("timeout" if sent else "unreachable", type(exc).__name__, sent=sent) from exc
        return LanResponse(response, lambda reusable: self._release(connection, reusable))

    def close(self):
        """Closes every connection of this client (all threads)."""
        with self._lock:
            connections, self._all = list(self._all), set()
        for connection in connections:
            try:
                connection.close()
            except OSError:
                pass
