"""Server-generated secrets (operations.secrets_init) and settings reading them by file."""
import hashlib
import io
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

from cryptography.fernet import Fernet

from operations import secrets_init
from operations.secrets_init import ALL_FILES, SecretsError, Store, main
from shared.licensing import generate_keypair, key_id

ROOT = Path(__file__).resolve().parents[1]
NO_SEEDS = {}


class StoreCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name) / "secrets"
        self.store = Store(self.dir, os.getuid(), os.getgid())

    def hashes(self):
        return {name: hashlib.sha256(self.store.path(name).read_bytes()).hexdigest() for name in ALL_FILES}


class GenerationTests(StoreCase):
    def test_first_run_creates_every_file_in_the_expected_format(self):
        report = self.store.ensure(NO_SEEDS)
        self.assertEqual(sorted(report), sorted(ALL_FILES))
        self.assertTrue(all(value == "criado" for value in report.values()), report)
        self.assertGreaterEqual(len(self.store.read("django_secret_key")), 64)
        password = self.store.read("db_password")
        self.assertGreaterEqual(len(password), 32)
        self.assertRegex(password, r"^[A-Za-z0-9_-]+$")  # URL-safe, cabe na DATABASE_URL
        self.assertRegex(self.store.read("restic_password"), r"^[A-Za-z0-9_-]{32,}$")
        Fernet(self.store.read("encryption_key").encode())  # chave Fernet válida
        for private, public in secrets_init.PAIRS:
            self.assertIn("BEGIN PRIVATE KEY", self.store.read(private))
            self.assertEqual(secrets_init._public_of(self.store.read(private)).strip(),
                             self.store.read(public).strip())
        self.assertNotEqual(key_id(self.store.read("license-public.pem")),
                            key_id(self.store.read("release-public.pem")), "pares devem ser distintos")
        self.assertNotEqual(self.store.read("db_password"), self.store.read("restic_password"))

    def test_second_run_changes_nothing(self):
        self.store.ensure(NO_SEEDS)
        before = self.hashes()
        report = self.store.ensure(NO_SEEDS)
        self.assertEqual(set(report.values()), {"mantido"})
        self.assertEqual(self.hashes(), before)
        self.assertEqual([p.name for p in self.dir.iterdir() if p.name.startswith(".")], [])

    def test_existing_file_is_never_overwritten_and_only_missing_ones_are_created(self):
        self.dir.mkdir(mode=0o750)
        (self.dir / "encryption_key").write_text(Fernet.generate_key().decode())
        private, public = generate_keypair()
        (self.dir / "release-private.pem").write_text(private)
        (self.dir / "release-public.pem").write_text(public)
        kept = (self.dir / "encryption_key").read_text()
        report = self.store.ensure(NO_SEEDS)
        self.assertEqual(report["encryption_key"], "mantido")
        self.assertEqual(report["release-private.pem"], "mantido")
        self.assertEqual(report["django_secret_key"], "criado")
        self.assertEqual(self.store.read("encryption_key"), kept)
        self.assertEqual(self.store.read("release-private.pem"), private)

    def test_write_new_refuses_an_existing_file(self):
        self.store.prepare_dir()
        self.store.path("db_password").write_text("existente")
        with self.assertRaises(SecretsError):
            self.store.write_new("db_password", "outro")
        self.assertEqual(self.store.read("db_password"), "existente")
        self.assertEqual([p.name for p in self.dir.iterdir()], ["db_password"])

    def test_missing_public_key_is_derived_from_the_private_key(self):
        self.dir.mkdir(mode=0o750)
        private, public = generate_keypair()
        (self.dir / "license-private.pem").write_text(private)
        report = self.store.ensure(NO_SEEDS)
        self.assertEqual(report["license-public.pem"], "derivado da chave privada")
        self.assertEqual(self.store.read("license-public.pem").strip(), public.strip())

    def test_public_key_without_private_stops_instead_of_making_a_new_pair(self):
        self.dir.mkdir(mode=0o750)
        (self.dir / "release-public.pem").write_text(generate_keypair()[1])
        with self.assertRaisesRegex(SecretsError, "sem release-private.pem"):
            self.store.ensure(NO_SEEDS)
        self.assertFalse(self.store.path("release-private.pem").exists())

    def test_mismatched_pair_stops(self):
        self.dir.mkdir(mode=0o750)
        (self.dir / "license-private.pem").write_text(generate_keypair()[0])
        (self.dir / "license-public.pem").write_text(generate_keypair()[1])
        with self.assertRaisesRegex(SecretsError, "não corresponde"):
            self.store.ensure(NO_SEEDS)

    def test_leftover_temporary_files_are_removed(self):
        self.dir.mkdir(mode=0o750)
        (self.dir / ".db_password.123.tmp").write_text("meio gravado")
        self.store.ensure(NO_SEEDS)
        self.assertFalse((self.dir / ".db_password.123.tmp").exists())


class PermissionTests(StoreCase):
    def test_modes_owner_and_group(self):
        self.store.ensure(NO_SEEDS)
        info = self.dir.stat()
        self.assertEqual(stat.S_IMODE(info.st_mode), 0o750)
        for name in ALL_FILES:
            info = self.store.path(name).stat()
            expected = 0o440 if name == "db_password" else 0o400
            self.assertEqual(stat.S_IMODE(info.st_mode), expected, name)
            self.assertEqual((info.st_uid, info.st_gid), (os.getuid(), os.getgid()), name)
            self.assertEqual(stat.S_IMODE(info.st_mode) & 0o007, 0, f"{name} legível por outros")

    def test_loose_modes_are_tightened_on_the_next_run(self):
        self.store.ensure(NO_SEEDS)
        os.chmod(self.store.path("encryption_key"), 0o644)
        os.chmod(self.dir, 0o755)
        self.store.ensure(NO_SEEDS)
        self.assertEqual(stat.S_IMODE(self.store.path("encryption_key").stat().st_mode), 0o400)
        self.assertEqual(stat.S_IMODE(self.dir.stat().st_mode), 0o750)

    @unittest.skipIf(os.getuid() == 0, "root pode trocar o dono")
    def test_wrong_owner_is_reported_instead_of_ignored(self):
        store = Store(self.dir, os.getuid() + 4242, os.getgid())
        with self.assertRaisesRegex(SecretsError, "pertence a"):
            store.ensure(NO_SEEDS)

    def test_symlink_is_refused(self):
        self.dir.mkdir(mode=0o750)
        target = Path(self._tmp.name) / "fora"
        target.write_text(Fernet.generate_key().decode())
        (self.dir / "encryption_key").symlink_to(target)
        with self.assertRaisesRegex(SecretsError, "link simbólico"):
            self.store.ensure(NO_SEEDS)


class OldInstallSeedTests(StoreCase):
    def seeds(self):
        return {"PDV_SECRET_KEY": "s" * 60, "POSTGRES_PASSWORD": "senha-antiga",
                "PDV_ENCRYPTION_KEY": Fernet.generate_key().decode(), "RESTIC_PASSWORD": "restic-antiga"}

    def test_missing_files_take_the_old_variables(self):
        seeds = self.seeds()
        report = self.store.ensure(seeds)
        self.assertEqual(report["db_password"], "criado a partir de POSTGRES_PASSWORD")
        for name, variable in secrets_init.SEEDS.items():
            self.assertEqual(self.store.read(name), seeds[variable])
        self.assertEqual(set(self.store.ensure(seeds).values()), {"mantido"})

    def test_variable_that_differs_from_the_file_stops_the_deploy(self):
        self.store.ensure(NO_SEEDS)
        before = self.hashes()
        with self.assertRaisesRegex(SecretsError, "PDV_ENCRYPTION_KEY"):
            self.store.ensure({"PDV_ENCRYPTION_KEY": Fernet.generate_key().decode()})
        self.assertEqual(self.hashes(), before)

    def test_invalid_old_variables_are_refused(self):
        for variables, message in (({"PDV_SECRET_KEY": "curta"}, "50"),
                                   ({"PDV_ENCRYPTION_KEY": "nao-e-fernet"}, "Fernet")):
            with self.subTest(variables=variables), tempfile.TemporaryDirectory() as directory:
                with self.assertRaisesRegex(SecretsError, message):
                    Store(directory, os.getuid(), os.getgid()).ensure(variables)


class TtyBuffer(io.StringIO):
    def isatty(self):
        return True


class KitTests(StoreCase):
    def test_main_never_prints_secret_values(self):
        out = io.StringIO()
        with unittest.mock.patch.dict(os.environ, {"PDV_SECRETS_DIR": str(self.dir),
                                                   "PDV_SECRETS_UID": str(os.getuid()),
                                                   "PDV_SECRETS_GID": str(os.getgid())}), \
                unittest.mock.patch.dict(os.environ, {name: "" for name in secrets_init.SEEDS.values()}):
            self.assertEqual(main([], stdout=out), 0)
        printed = out.getvalue()
        for name in ALL_FILES:
            self.assertIn(name, printed)
            self.assertNotIn(self.store.read(name).strip().splitlines()[1 if name.endswith(".pem") else 0], printed)

    def test_kit_refuses_a_non_terminal_output(self):
        self.store.ensure(NO_SEEDS)
        out = io.StringIO()
        with unittest.mock.patch.dict(os.environ, {"PDV_SECRETS_DIR": str(self.dir)}), \
                unittest.mock.patch("sys.stderr", io.StringIO()):
            self.assertEqual(main(["--kit"], stdout=out), 2)
        self.assertEqual(out.getvalue(), "")

    def test_kit_on_a_terminal_contains_every_secret(self):
        self.store.ensure(NO_SEEDS)
        out = TtyBuffer()
        with unittest.mock.patch.dict(os.environ, {"PDV_SECRETS_DIR": str(self.dir)}):
            self.assertEqual(main(["--kit"], stdout=out), 0)
        for name in ALL_FILES:
            self.assertIn(self.store.read(name), out.getvalue())

    @unittest.skipUnless(shutil.which("sh"), "sem sh")
    def test_kit_script_restores_identical_files_and_refuses_to_overwrite(self):
        self.store.ensure(NO_SEEDS)
        kit = self.store.kit()
        target = Path(self._tmp.name) / "nova-vps"
        env = {"PATH": os.environ.get("PATH", ""), "PDV_SECRETS_DIR": str(target)}
        result = subprocess.run(["sh"], input=kit, text=True, capture_output=True, env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        restored = Store(target, os.getuid(), os.getgid())
        for name in ALL_FILES:
            self.assertEqual(restored.path(name).read_bytes(), self.store.path(name).read_bytes(), name)
            expected = 0o440 if name == "db_password" else 0o400
            self.assertEqual(stat.S_IMODE(restored.path(name).stat().st_mode), expected, name)
        # Restored volume is accepted as is by the next deploy.
        self.assertEqual(set(restored.ensure(NO_SEEDS).values()), {"mantido"})
        # Different file already there: refused unless SUBSTITUIR=1.
        os.chmod(target / "encryption_key", 0o600)
        (target / "encryption_key").write_text(Fernet.generate_key().decode())
        refused = subprocess.run(["sh"], input=kit, text=True, capture_output=True, env=env)
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("DIFERENTE", refused.stderr)
        self.assertNotEqual(restored.read("encryption_key"), self.store.read("encryption_key"))
        forced = subprocess.run(["sh"], input=kit, text=True, capture_output=True,
                                env={**env, "SUBSTITUIR": "1"})
        self.assertEqual(forced.returncode, 0, forced.stderr)
        self.assertEqual(restored.read("encryption_key"), self.store.read("encryption_key"))

    def test_kit_requires_every_file(self):
        self.store.ensure(NO_SEEDS)
        self.store.path("restic_password").unlink()
        with self.assertRaisesRegex(SecretsError, "restic_password"):
            self.store.kit()


class SettingsFromFilesTests(StoreCase):
    """Production settings read secret key and DB password from secrets-init files."""

    def load_settings(self, **extra):
        self.store.ensure(NO_SEEDS)
        env = {
            "PATH": os.environ.get("PATH", ""), "PYTHONPATH": f"{ROOT}{os.pathsep}{ROOT / 'cloud'}",
            "PDV_ENV": "production", "PDV_ALLOWED_HOSTS": "pdv2.example.com",
            "PDV_SECRET_KEY_FILE": str(self.store.path("django_secret_key")),
            "DATABASE_URL": "postgresql://pdv2@pdv2-db:5432/pdv2",
            "PDV_DATABASE_PASSWORD_FILE": str(self.store.path("db_password")),
            "PDV_LICENSE_PRIVATE_KEY_FILE": str(self.store.path("license-private.pem")),
        }
        env.update(extra)
        code = ("import json; from pdvcloud import settings as s; "
                "print(json.dumps([s.SECRET_KEY, s.DATABASES['default']['PASSWORD']]))")
        return subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True)

    def test_secret_key_and_database_password_come_from_files(self):
        result = self.load_settings()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), '["%s", "%s"]' % (
            self.store.read("django_secret_key"), self.store.read("db_password")))

    def test_variables_still_win_over_files(self):
        result = self.load_settings(PDV_SECRET_KEY="v" * 60,
                                    DATABASE_URL="postgresql://pdv2:senha-da-url@pdv2-db:5432/pdv2")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), '["%s", "senha-da-url"]' % ("v" * 60))

    def test_unreadable_secret_file_stops_production(self):
        result = self.load_settings(PDV_SECRET_KEY_FILE=str(self.dir / "nao-existe"))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("PDV_SECRET_KEY_FILE", result.stderr)
        self.assertNotIn(self.store.read("db_password"), result.stderr)


if __name__ == "__main__":
    unittest.main()
