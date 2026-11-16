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

    def test_same_network_needs_a_real_adapter_on_the_network_of_the_server(self):
        lan_card = {"address": "192.168.0.20", "prefix": 24, "name": "Ethernet Intel(R) Ethernet", "up": True}
        wsl = {"address": "172.24.80.1", "prefix": 20, "name": "vEthernet (WSL) Hyper-V Virtual Ethernet Adapter",
               "up": True}
        vpn = {"address": "10.8.0.6", "prefix": 8, "name": "Ethernet 3 TAP-Windows Adapter V9", "up": True}

        def check(listed, route, target="192.168.0.10"):
            return discovery.same_network(target, addresses=lambda: [item["address"] for item in listed],
                                          listed=lambda: listed, route=lambda _target: route)

        self.assertTrue(check([lan_card, wsl], "192.168.0.20"))
        # Cable out: only the WSL adapter keeps an address; there is no route to the server through it.
        self.assertFalse(check([wsl], None))
        self.assertFalse(check([wsl], "172.24.80.1"), "an address on another network is not the shop")
        # A VPN that takes the route (even one that claims the network of the server).
        self.assertFalse(check([vpn], "10.8.0.6", target="10.8.0.1"))
        self.assertFalse(check([dict(lan_card, up=False)], "192.168.0.20"), "adapter without link")
        self.assertFalse(check([lan_card], "192.168.0.20", target="192.168.1.10"), "another network (/24)")
        self.assertTrue(check([dict(lan_card, prefix=16)], "192.168.0.20", target="192.168.1.10"), "a /16 shop")
        # The same computer (development) only needs some address; a name falls back to the plain check.
        self.assertTrue(check([lan_card], None, target="127.0.0.1"))
        self.assertFalse(check([], None, target="127.0.0.1"))
        self.assertTrue(discovery.virtual_adapter("VirtualBox Host-Only Ethernet Adapter"))
        self.assertFalse(discovery.virtual_adapter("Realtek PCIe GbE Family Controller", "Ethernet"))

    def test_interfaces_on_windows_come_with_the_size_of_the_network(self):
        discovery._INTERFACES_CACHE.update(at=0.0, value=None)
        self.addCleanup(discovery._INTERFACES_CACHE.update, at=0.0, value=None)
        answer = json.dumps([
            {"address": "192.168.0.20", "prefix": 23, "alias": "Ethernet", "description": "Realtek", "status": "Up"},
            {"address": "172.24.80.1", "prefix": 20, "alias": "vEthernet (WSL)", "description": "Hyper-V",
             "status": "Up"},
            {"address": "10.0.0.5", "prefix": 24, "alias": "Wi-Fi", "description": "Intel", "status": "Disconnected"}])
        with patch.object(discovery.os, "name", "nt"), patch.object(discovery.subprocess, "run") as run:
            run.return_value.stdout = answer
            found = discovery.interfaces()
        self.assertEqual([(item["address"], item["prefix"], item["up"]) for item in found],
                         [("192.168.0.20", 23, True), ("172.24.80.1", 20, True), ("10.0.0.5", 24, False)])
        self.assertTrue(discovery.virtual_adapter(found[1]["name"]))
        self.assertTrue(discovery.same_network("192.168.1.30", listed=lambda: found,
                                               route=lambda _target: "192.168.0.20"))

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
