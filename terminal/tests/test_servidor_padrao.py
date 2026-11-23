"""The installed cashier already knows the CrownTech server: at the customer's shop nobody
types an address, only the license key and the pairing code."""
import unittest

from terminal import paths


class DefaultCloudUrlTests(unittest.TestCase):
    def test_installed_program_points_to_the_production_server(self):
        self.assertEqual(paths.default_cloud_url({}, frozen=True), "https://pdv2.crowntech.com.br")

    def test_source_code_run_keeps_the_address_field(self):
        self.assertEqual(paths.default_cloud_url({}, frozen=False), "")

    def test_environment_variable_wins_everywhere(self):
        env = {"PDV_CLOUD_URL": "http://127.0.0.1:8000"}
        self.assertEqual(paths.default_cloud_url(env, frozen=True), "http://127.0.0.1:8000")
        self.assertEqual(paths.default_cloud_url(env, frozen=False), "http://127.0.0.1:8000")


if __name__ == "__main__":
    unittest.main()
