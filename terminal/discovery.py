"""Finding the shop server on the local network. Standard library only.

A station broadcasts one small UDP question; the server answers by unicast.
The answer is NOT authenticated: it is only a hint of where to connect. Trust
comes from the pinned certificate fingerprint (linked station) or from the
6-digit code typed by the manager (new station).
"""

import json
import os
import secrets
import socket
import subprocess
import threading
import time

PORT = 8012
VERSION = 1
MAX_PACKET = 512
MAX_ANSWERS_PER_SECOND = 5
_HEX = set("0123456789abcdef")
_PROFILE_CACHE = {"at": 0.0, "value": "unknown"}
_PROFILE_LOCK = threading.Lock()


def default_port(env=None):
    env = os.environ if env is None else env
    try:
        port = int(env.get("PDV_DISCOVERY_PORT") or PORT)
    except ValueError:
        return PORT
    return port if 0 <= port < 65536 else PORT


def _is_hex(value, length):
    return isinstance(value, str) and len(value) == length and set(value) <= _HEX


def parse_request(packet):
    """The question of a station, or None for anything else."""
    if not packet or len(packet) > MAX_PACKET:
        return None
    try:
        value = json.loads(packet.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(value, dict) or value.get("pdv2") != "discover" or value.get("v") != VERSION:
        return None
    nonce, shop_id = value.get("nonce"), value.get("shop_id")
    if not _is_hex(nonce, 16):
        return None
    if shop_id is not None and (not isinstance(shop_id, str) or not 0 < len(shop_id) <= 64):
        return None
    return {"nonce": nonce, "shop_id": shop_id}


def parse_answer(packet, nonce):
    """The answer of a server to our question, or None."""
    try:
        value = json.loads(packet.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(value, dict) or value.get("pdv2") != "server" or value.get("v") != VERSION:
        return None
    if value.get("nonce") != nonce or not _is_hex(value.get("fingerprint"), 64):
        return None
    port = value.get("port")
    if isinstance(port, bool) or not isinstance(port, int) or not 0 < port < 65536:
        return None
    if not isinstance(value.get("shop_id"), str):
        return None
    return value


class Responder:
    """Answers discovery questions while `info()` returns a dict."""

    def __init__(self, info, host="0.0.0.0", port=None):
        self.info = info
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            if os.name != "nt":  # on Windows it would let another process share the port
                self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.socket.bind((host, default_port() if port is None else port))
        except OSError:
            self.socket.close()
            raise
        self.socket.settimeout(0.2)
        self.port = self.socket.getsockname()[1]
        self.answered = 0
        self._recent = {}
        self._stop = threading.Event()
        self.thread = threading.Thread(target=self._loop, name="pdv-lan-discovery", daemon=True)
        self.thread.start()

    def _allowed(self, address):
        now = time.monotonic()
        moments = [moment for moment in self._recent.get(address, ()) if now - moment < 1.0]
        allowed = len(moments) < MAX_ANSWERS_PER_SECOND
        if allowed:
            moments.append(now)
        self._recent[address] = moments
        if len(self._recent) > 256:
            self._recent = {key: value for key, value in self._recent.items() if value and now - value[-1] < 1.0}
        return allowed

    def _answer(self, packet, address):
        question = parse_request(packet)
        if question is None:
            return None
        details = self.info()
        if not isinstance(details, dict):
            return None
        if question["shop_id"] is not None and question["shop_id"] != details.get("shop_id"):
            return None
        if not self._allowed(address[0]):
            return None
        return json.dumps({**details, "pdv2": "server", "v": VERSION, "nonce": question["nonce"]},
                          ensure_ascii=False).encode("utf-8")

    def _loop(self):
        while not self._stop.is_set():
            try:
                packet, address = self.socket.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                if self._stop.is_set():
                    return
                time.sleep(0.2)
                continue
            try:
                answer = self._answer(packet, address)
                if answer is not None:
                    self.socket.sendto(answer, address)
                    self.answered += 1
            except Exception:
                continue  # one bad packet never stops the responder

    def stop(self):
        self._stop.set()
        if self.thread.is_alive() and self.thread is not threading.current_thread():
            self.thread.join(timeout=2)
        try:
            self.socket.close()
        except OSError:
            pass


def start_responder(info, *, host="0.0.0.0", port=None):
    return Responder(info, host, port)


def local_addresses():
    """IPv4 addresses of this computer on its networks (never loopback)."""
    found = []
    for probe in ("10.255.255.255", "192.168.255.255", "172.31.255.255"):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.connect((probe, 9))  # no packet leaves: this only picks the route
                found.append(sock.getsockname()[0])
        except OSError:
            continue
    try:
        for item in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            found.append(item[4][0])
    except OSError:
        pass
    unique = []
    for address in found:
        if address and not address.startswith("127.") and address != "0.0.0.0" and address not in unique:
            unique.append(address)
    return unique


def broadcast_targets():
    targets = ["255.255.255.255"]
    for address in local_addresses():
        directed = address.rsplit(".", 1)[0] + ".255"
        if directed not in targets:
            targets.append(directed)
    return targets


def discover(shop_id=None, *, timeout=2.0, attempts=2, port=None, targets=None):
    """Servers that answered: each answer plus {"address": source ip}."""
    port = default_port() if port is None else port
    nonce = secrets.token_hex(8)
    question = json.dumps({"pdv2": "discover", "v": VERSION, "nonce": nonce, "shop_id": shop_id}).encode("utf-8")
    destinations = [tuple(target) if isinstance(target, (tuple, list)) else (target, port)
                    for target in (broadcast_targets() if targets is None else targets)]
    servers, seen = [], set()
    attempts = max(1, int(attempts))
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    except OSError:
        return servers
    with sock:
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            sock.bind(("0.0.0.0", 0))
        except OSError:
            return servers
        for _attempt in range(attempts):
            for destination in destinations:
                try:
                    sock.sendto(question, destination)
                except OSError:
                    continue
            deadline = time.monotonic() + timeout / attempts
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                sock.settimeout(remaining)
                try:
                    packet, source = sock.recvfrom(4096)
                except OSError:  # includes the timeout
                    break
                answer = parse_answer(packet, nonce)
                if answer is None or (shop_id is not None and answer.get("shop_id") != shop_id):
                    continue
                key = (source[0], answer["port"], answer["fingerprint"])
                if key in seen:
                    continue
                seen.add(key)
                answer.pop("pdv2", None)
                servers.append({**answer, "address": source[0]})
            if servers:
                break
    return servers


def network_profile():
    """Windows network category ("private", "public", "domain"); "unknown" elsewhere."""
    if os.name != "nt":
        return "unknown"
    with _PROFILE_LOCK:
        now = time.monotonic()
        if _PROFILE_CACHE["at"] and now - _PROFILE_CACHE["at"] < 60:
            return _PROFILE_CACHE["value"]
        value = "unknown"
        try:
            result = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command",
                 "(Get-NetConnectionProfile | Select-Object -ExpandProperty NetworkCategory) -join ','"],
                capture_output=True, text=True, timeout=3, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            categories = [item.strip().lower() for item in (result.stdout or "").split(",") if item.strip()]
            if "public" in categories:
                value = "public"
            elif "domainauthenticated" in categories:
                value = "domain"
            elif "private" in categories:
                value = "private"
        except (OSError, subprocess.SubprocessError):
            value = "unknown"
        _PROFILE_CACHE.update(at=now, value=value)
        return value
