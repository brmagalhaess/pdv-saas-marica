"""Endereço amigável do caixa: http://pdv2.localhost/ (porta 80 quando livre, senão 8010)."""
import os
import tempfile
import unittest
from unittest.mock import patch

from terminal import paths
from terminal.app import create_app


class FriendlyAddressTests(unittest.TestCase):
    def test_service_prefers_port_80_and_records_the_choice(self):
        with tempfile.TemporaryDirectory() as data:
            self.assertEqual(paths.choose_service_port({}, data, try_bind=lambda port: port == 80), 80)
            with open(os.path.join(data, paths.PORT_FILE), encoding="utf-8") as stream:
                self.assertEqual(stream.read(), "80")
            self.assertEqual(paths.choose_service_port({}, data, try_bind=lambda port: False), 8010)
            with open(os.path.join(data, paths.PORT_FILE), encoding="utf-8") as stream:
                self.assertEqual(stream.read(), "8010")

    def test_explicit_port_always_wins(self):
        with tempfile.TemporaryDirectory() as data:
            self.assertEqual(paths.choose_service_port({"PDV_TERMINAL_PORT": "8030"}, data,
                                                       try_bind=lambda port: True), 8030)
            self.assertFalse(os.path.exists(os.path.join(data, paths.PORT_FILE)))

    def test_installed_launcher_reads_the_port_the_service_chose(self):
        with tempfile.TemporaryDirectory() as data:
            with open(os.path.join(data, paths.PORT_FILE), "w", encoding="utf-8") as stream:
                stream.write("80")
            with patch.object(paths, "read_pointer", return_value="2.1.0"), \
                    patch.object(paths, "data_dir", return_value=data):
                self.assertEqual(paths.terminal_port({}), 80)
        self.assertEqual(paths.terminal_port({}), paths.DEFAULT_PORT)

    def test_address_without_port_on_80(self):
        self.assertEqual(paths.cashier_address(80), "http://pdv2.localhost/")
        self.assertEqual(paths.cashier_address(8010), "http://pdv2.localhost:8010/")

    def test_cashier_answers_on_the_friendly_host(self):
        with tempfile.TemporaryDirectory() as data:
            client = create_app({"TESTING": True, "DATA_DIR": data}).test_client()
            self.assertEqual(client.get("/api/session", base_url="http://pdv2.localhost").status_code, 200)
            self.assertEqual(client.get("/api/session", base_url="http://outro.exemplo").status_code, 403)


if __name__ == "__main__":
    unittest.main()
