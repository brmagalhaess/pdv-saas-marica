"""Every variable of the production env example must reach a container through the compose file."""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = (ROOT / "compose.production.yaml").read_text()
EXAMPLE = (ROOT / "infra" / "production.env.example").read_text()


def example_variables():
    return re.findall(r"^([A-Z][A-Z0-9_]*)=", EXAMPLE, flags=re.MULTILINE)


def cloud_environment():
    """Keys of the x-env anchor, shared by migrate, cloud and worker."""
    block = COMPOSE.split("x-env: &env\n", 1)[1].split("\nx-service:", 1)[0]
    return dict(re.findall(r"^  ([A-Z][A-Z0-9_]*): (.*)$", block, flags=re.MULTILINE))


class ProductionEnvExampleTests(unittest.TestCase):
    def test_every_example_variable_is_interpolated_by_compose(self):
        variables = example_variables()
        self.assertIn("PDV_DOMAIN", variables)
        missing = [name for name in variables if not re.search(r"\$\{%s[:?}-]" % re.escape(name), COMPOSE)]
        self.assertEqual(missing, [], "variáveis do exemplo que o compose de produção não repassa")

    def test_admin_and_log_level_reach_the_django_containers(self):
        environment = cloud_environment()
        self.assertEqual(environment.get("PDV_ENABLE_DJANGO_ADMIN"), "${PDV_ENABLE_DJANGO_ADMIN:-0}")
        self.assertEqual(environment.get("PDV_LOG_LEVEL"), "${PDV_LOG_LEVEL:-INFO}")

    def test_settings_variables_documented_as_optional_are_passed(self):
        docs = (ROOT / "docs" / "DEPLOY-DOKPLOY.md").read_text()
        table = docs.split("### Variáveis opcionais", 1)[1].split("\n## ", 1)[0]
        documented = re.findall(r"^\| `([A-Z][A-Z0-9_]*)` \|", table, flags=re.MULTILINE)
        self.assertIn("PDV_ENABLE_DJANGO_ADMIN", documented)
        missing = [name for name in documented if not re.search(r"\$\{%s[:?}-]" % re.escape(name), COMPOSE)]
        self.assertEqual(missing, [], "variáveis documentadas que o compose de produção não repassa")


if __name__ == "__main__":
    unittest.main()
