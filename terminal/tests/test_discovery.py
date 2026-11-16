"""UDP discovery of the shop server. Ephemeral ports on loopback only."""

import json
import socket
import unittest
from unittest.mock import patch

from terminal import discovery

HOST = "127.0.0.1"
SHOP = "0b8f6f0e-1111-4222-8333-444455556666"
INFO = {"shop_id": SHOP, "shop_name": "Mercado X", "terminal_id": "t-1", "terminal_name": "Caixa 1",
        "host": "CAIXA-PRINCIPAL", "port": 8011, "fingerprint": "ab" * 32, "api": 1, "version": "2.1.0",
        "generation": 1}


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.info = dict(INFO)
        self.responder = discovery.start_responder(lambda: self.info, host=HOST, port=0)
        self.addCleanup(self.responder.stop)
        self.target = [(HOST, self.responder.port)]

    def ask(self, packet, wait=0.4):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(wait)
            sock.sendto(packet, (HOST, self.responder.port))
            try:
                return json.loads(sock.recvfrom(4096)[0])
            except socket.timeout:
                return None

    def question(self, **changes):
        return json.dumps({"pdv2": "discover", "v": 1, "nonce": "0123456789abcdef", "shop_id": None,
                           **changes}).encode()

    def test_answer_has_the_fixed_shape_and_echoes_the_nonce(self):
        answer = self.ask(self.question())
        self.assertEqual(answer, {**INFO, "pdv2": "server", "v": 1, "nonce": "0123456789abcdef"})
        self.assertEqual(self.ask(self.question(nonce="fedcba9876543210"))["nonce"], "fedcba9876543210")

    def test_answers_only_the_right_shop_or_null(self):
        self.assertIsNotNone(self.ask(self.question(shop_id=SHOP)))
        self.assertIsNotNone(self.ask(self.question(shop_id=None)))
        self.assertIsNone(self.ask(self.question(shop_id="outra-loja")))

    def test_malformed_packets_are_ignored(self):
        for packet in (b"", b"nada", b"\xff\xfe\x00", b"[]", b'{"pdv2":"discover"}',
                       self.question(v=2), self.question(pdv2="server"), self.question(nonce="curto"),
                       self.question(nonce="ZZZZZZZZZZZZZZZZ"), self.question(shop_id=5),
                       self.question(shop_id=""), self.question(extra="x" * 600)):
            self.assertIsNone(self.ask(packet, wait=0.1), packet[:40])
        self.assertIsNotNone(self.ask(self.question()), "the responder survives bad packets")

    def test_silent_when_the_lan_listener_is_off(self):
        self.info = None
        self.assertIsNone(self.ask(self.question()))

    def test_at_most_five_answers_a_second_for_one_address(self):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(0.3)
            for _index in range(12):
                sock.sendto(self.question(), (HOST, self.responder.port))
            answers = 0
            try:
                while True:
                    sock.recvfrom(4096)
                    answers += 1
            except socket.timeout:
                pass
        self.assertEqual(answers, discovery.MAX_ANSWERS_PER_SECOND)

    def test_discover_returns_the_answer_with_the_source_address(self):
        found = discovery.discover(SHOP, timeout=1.0, targets=self.target)
        self.assertEqual(found, [{**INFO, "v": 1, "nonce": found[0]["nonce"], "address": HOST}])
        self.assertEqual(discovery.discover(None, timeout=1.0, targets=self.target)[0]["shop_id"], SHOP)
        self.assertEqual(discovery.discover("outra-loja", timeout=0.3, targets=self.target), [])

    def test_discover_ignores_answers_with_another_nonce_or_shape(self):
        impostor = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        impostor.bind((HOST, 0))
        impostor.settimeout(2)
        self.addCleanup(impostor.close)
        import threading

        def reply():
            try:
                packet, source = impostor.recvfrom(4096)
            except OSError:
                return
            nonce = json.loads(packet)["nonce"]
            for answer in ({**INFO, "pdv2": "server", "v": 1, "nonce": "0" * 16},
                           {**INFO, "pdv2": "server", "v": 1, "nonce": nonce, "fingerprint": "curto"},
                           {**INFO, "pdv2": "server", "v": 1, "nonce": nonce, "port": "8011"},
                           {**INFO, "pdv2": "server", "v": 1, "nonce": nonce, "shop_id": "outra-loja"}):
                impostor.sendto(json.dumps(answer).encode(), source)
            impostor.sendto(b"lixo", source)

        threading.Thread(target=reply, daemon=True).start()
        self.assertEqual(discovery.discover(SHOP, timeout=0.6, attempts=1,
                                            targets=[(HOST, impostor.getsockname()[1])]), [])

    def test_discover_without_any_server_is_empty(self):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as free:
            free.bind((HOST, 0))
            port = free.getsockname()[1]
        self.assertEqual(discovery.discover(SHOP, timeout=0.2, targets=[HOST], port=port), [])

    def test_ports_and_local_addresses(self):
        self.assertEqual(discovery.PORT, 8012)
        self.assertEqual(discovery.default_port({}), 8012)
        self.assertEqual(discovery.default_port({"PDV_DISCOVERY_PORT": "0"}), 0)
        self.assertEqual(discovery.default_port({"PDV_DISCOVERY_PORT": "abc"}), 8012)
        addresses = discovery.local_addresses()
        self.assertIsInstance(addresses, list)
        self.assertFalse(any(address.startswith("127.") for address in addresses))
        with patch.object(discovery, "local_addresses", return_value=["192.168.0.20", "10.1.2.3"]):
            self.assertEqual(discovery.broadcast_targets(), ["255.255.255.255", "192.168.0.255", "10.1.2.255"])

    def test_network_profile(self):
        with patch.object(discovery.os, "name", "posix"):
            self.assertEqual(discovery.network_profile(), "unknown")
        discovery._PROFILE_CACHE.update(at=0.0, value="unknown")
        self.addCleanup(discovery._PROFILE_CACHE.update, at=0.0, value="unknown")
        with patch.object(discovery.os, "name", "nt"), patch.object(discovery.subprocess, "run") as run:
            run.return_value.stdout = "Private,Public\n"
            self.assertEqual(discovery.network_profile(), "public")
            run.return_value.stdout = "Private\n"
            self.assertEqual(discovery.network_profile(), "public", "cached for a minute")
            self.assertEqual(run.call_count, 1)
            discovery._PROFILE_CACHE["at"] = 0.0
            self.assertEqual(discovery.network_profile(), "private")
            discovery._PROFILE_CACHE["at"] = 0.0
            run.side_effect = OSError("sem powershell")
            self.assertEqual(discovery.network_profile(), "unknown")


if __name__ == "__main__":
    unittest.main()
