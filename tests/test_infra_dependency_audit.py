"""The dependency audit blocks fixable vulnerabilities and only warns about the rest."""
import contextlib
import io
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import audit_dependencies  # noqa: E402

CERTIFI = {"name": "certifi", "version": "2026.7.22", "vulns": [
    {"id": "GHSA-6q48-6c7w-9gw2", "aliases": ["PYSEC-2026-4320"], "fix_versions": ["2026.10.19"]}]}
NO_FIX = {"name": "somepkg", "version": "1.0", "vulns": [{"id": "GHSA-xxxx-yyyy-zzzz", "fix_versions": []}]}
CLEAN = {"name": "Django", "version": "5.2.17", "vulns": []}
SKIPPED = {"name": "pywin32", "skip_reason": "not found on PyPI"}


def run(report):
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "audit.json"
        path.write_text(json.dumps(report))
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = audit_dependencies.main(["--report", str(path)])
    return code, output.getvalue()


class DependencyAuditTests(unittest.TestCase):
    def test_vulnerability_with_fixed_release_fails_the_build(self):
        code, output = run({"dependencies": [CLEAN, CERTIFI, CERTIFI]})
        self.assertEqual(code, 1)
        self.assertEqual(output.count("GHSA-6q48-6c7w-9gw2"), 1)
        self.assertIn("GHSA-6q48-6c7w-9gw2 / PYSEC-2026-4320", output)
        self.assertIn("2026.10.19", output)

    def test_vulnerability_without_fix_only_warns(self):
        code, output = run({"dependencies": [CLEAN, NO_FIX, SKIPPED]})
        self.assertEqual(code, 0)
        self.assertIn("GHSA-xxxx-yyyy-zzzz", output)
        self.assertIn("pywin32", output)
        self.assertIn("1 sem correção", output)

    def test_clean_report_passes(self):
        code, output = run({"dependencies": [CLEAN]})
        self.assertEqual(code, 0)
        self.assertIn("1 dependências auditadas; 0 com correção disponível", output)

    def test_report_without_json_is_an_audit_failure(self):
        original = audit_dependencies.subprocess.run

        class Result:
            stdout, stderr = "", "ERROR: could not resolve requirements"

        audit_dependencies.subprocess.run = lambda *args, **kwargs: Result()
        try:
            with self.assertRaises(SystemExit) as raised:
                audit_dependencies.run_pip_audit(["requirements.txt"])
        finally:
            audit_dependencies.subprocess.run = original
        self.assertIn("could not resolve", str(raised.exception.code))

    def test_pinned_lists_are_past_the_certifi_advisory_and_ship_tzdata(self):
        for name in ("requirements.txt", "terminal/requirements.txt"):
            text = (ROOT / name).read_text()
            version = re.search(r"^certifi==(\d+)\.(\d+)\.(\d+)$", text, re.M)
            self.assertIsNotNone(version, name)
            self.assertGreaterEqual(tuple(int(part) for part in version.groups()), (2026, 10, 19), name)
            # Unconditional on purpose: Windows terminals have no system zone database.
            self.assertRegex(text, r"(?m)^tzdata==[\d.]+$", name)


if __name__ == "__main__":
    unittest.main()
