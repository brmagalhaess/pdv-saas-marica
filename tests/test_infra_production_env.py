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


def service_block(name):
    """Text of one service in compose.production.yaml (two-space indented keys)."""
    body = COMPOSE.split("\nservices:\n", 1)[1].split("\nvolumes:\n", 1)[0]
    match = re.search(r"^  %s:\n(.*?)(?=^  [a-z][a-z-]*:\n|\Z)" % re.escape(name), body, flags=re.MULTILINE | re.DOTALL)
    if not match:
        raise AssertionError(f"serviço {name} ausente")
    return match.group(1)


SECRET_VARIABLES = ("PDV_SECRET_KEY", "POSTGRES_PASSWORD", "PDV_ENCRYPTION_KEY", "RESTIC_PASSWORD",
                    "PDV_LICENSE_PRIVATE_KEY_PATH", "PDV_LICENSE_PUBLIC_KEY_PATH",
                    "PDV_RELEASE_PRIVATE_KEY_PATH", "PDV_RELEASE_PUBLIC_KEY_PATH")


class ServerGeneratedSecretsTests(unittest.TestCase):
    """The server generates its secrets; the Dokploy panel only has non-secret settings."""

    def test_no_secret_is_required_from_the_panel(self):
        required = re.findall(r"\$\{([A-Z][A-Z0-9_]*):\?", COMPOSE)
        self.assertEqual(sorted(set(required)), ["PDV_DOMAIN", "PDV_TRUSTED_PROXY_CIDRS"])
        self.assertNotRegex(COMPOSE, r"(?m)^secrets:", "bloco top-level secrets: voltou")
        self.assertNotIn("/run/secrets/", COMPOSE)

    def test_example_has_no_secret_variables(self):
        variables = example_variables()
        for name in SECRET_VARIABLES:
            self.assertNotIn(name, variables)
        self.assertIn("RESTIC_REPOSITORY", variables)
        self.assertIn("PDV_BACKUP_OFFSITE", variables)

    def test_secrets_init_runs_first_without_root_and_writes_the_volume(self):
        block = service_block("secrets-init")
        self.assertIn("command: [python, -m, operations.secrets_init]", block)
        self.assertIn('user: "10001:70"', block)
        self.assertIn("volumes: [pdv2_secrets:/secrets]", block)
        self.assertIn("cap_drop: [ALL]", block)
        self.assertIn('restart: "no"', block)
        self.assertRegex(COMPOSE, r"(?m)^  pdv2_secrets:$")
        for service in ("db", "migrate", "cloud", "worker", "backup"):
            with self.subTest(service=service):
                self.assertRegex(service_block(service),
                                 r"secrets-init: \{condition: service_completed_successfully\}")

    def test_every_consumer_reads_secrets_by_file(self):
        environment = cloud_environment()
        self.assertEqual(environment["PDV_SECRET_KEY_FILE"], "/secrets/django_secret_key")
        self.assertEqual(environment["PDV_ENCRYPTION_KEY_FILE"], "/secrets/encryption_key")
        self.assertEqual(environment["PDV_DATABASE_PASSWORD_FILE"], "/secrets/db_password")
        self.assertEqual(environment["DATABASE_URL"], "postgresql://pdv2@pdv2-db:5432/pdv2")
        for kind in ("LICENSE", "RELEASE"):
            for side in ("PRIVATE", "PUBLIC"):
                self.assertEqual(environment[f"PDV_{kind}_{side}_KEY_FILE"],
                                 f"/secrets/{kind.lower()}-{side.lower()}.pem")
        self.assertNotIn("PDV_SECRET_KEY", environment)
        self.assertNotIn("PDV_ENCRYPTION_KEY", environment)
        self.assertIn("volumes: [pdv2_secrets:/secrets:ro]", COMPOSE.split("x-service:", 1)[1].split("services:", 1)[0])
        self.assertIn("pdv2_secrets:/secrets:ro", service_block("cloud"))
        db = service_block("db")
        self.assertIn("POSTGRES_PASSWORD_FILE: /secrets/db_password", db)
        self.assertNotIn("POSTGRES_PASSWORD:", db)
        self.assertIn("pdv2_secrets:/secrets:ro", db)

    def test_backup_reads_passwords_by_file_and_saves_the_secrets(self):
        backup = service_block("backup")
        self.assertIn("PGPASSWORD_FILE: /secrets/db_password", backup)
        self.assertIn("RESTIC_PASSWORD_FILE: /secrets/restic_password", backup)
        self.assertIn("RESTIC_REPOSITORY: ${RESTIC_REPOSITORY:-/status/restic}", backup)
        self.assertIn("BACKUP_SECRETS_DIR: /secrets", backup)
        self.assertIn("pdv2_secrets:/secrets:ro", backup)
        self.assertNotRegex(backup, r"PGPASSWORD: |RESTIC_PASSWORD: ")
        script = (ROOT / "infra" / "backup.sh").read_text()
        self.assertIn("--tag pdv2-secrets", script)

    def test_offsite_flag_is_a_variable_defaulting_to_off(self):
        self.assertEqual(cloud_environment().get("PDV_BACKUP_OFFSITE"), "${PDV_BACKUP_OFFSITE:-0}")

    def test_cloud_image_prepares_the_secrets_mount_point_for_10001_70(self):
        dockerfile = (ROOT / "infra" / "Dockerfile.cloud").read_text()
        self.assertIn("chown 10001:70 /secrets && chmod 0750 /secrets", dockerfile)

    def test_docs_explain_the_recovery_kit(self):
        docs = (ROOT / "docs" / "DEPLOY-DOKPLOY.md").read_text()
        self.assertIn("python -m operations.secrets_init --kit", docs)
        self.assertIn("restic_password", docs)


if __name__ == "__main__":
    unittest.main()
