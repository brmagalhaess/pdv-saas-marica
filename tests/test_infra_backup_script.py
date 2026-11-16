"""Regression tests for infra/backup.sh using fake pg_dump/pg_restore/restic.

The hourly loop used to call the backup inside `if ! (...)`, which disables
`set -e`: a failing pg_dump or restic still ended with `last-success` written.
These tests run the real script against stand-in binaries placed first in PATH.
"""
import os
import shutil
import signal
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "infra" / "backup.sh"

FAKE_PG_DUMP = """#!/bin/sh
echo "pg_dump $*" >> "$FAKE_LOG"
case ",$FAKE_FAIL," in *,pg_dump,*) echo "pg_dump: connection refused" >&2; exit 1 ;; esac
for arg in "$@"; do
  case "$arg" in --file=*) printf 'PGDMP' > "${arg#--file=}" ;; esac
done
"""

FAKE_PG_RESTORE = """#!/bin/sh
echo "pg_restore $*" >> "$FAKE_LOG"
case ",$FAKE_FAIL," in *,pg_restore,*) echo "pg_restore: input file is too short" >&2; exit 1 ;; esac
exit 0
"""

FAKE_RESTIC = """#!/bin/sh
echo "restic $*" >> "$FAKE_LOG"
case ",$FAKE_FAIL," in *,"restic-$1",*) echo "restic $1: Fatal: unable to open repository" >&2; exit 1 ;; esac
case "$1" in
  backup) printf '{"message_type":"summary","snapshot_id":"%s"}\\n' "$FAKE_SNAPSHOT" ;;
  snapshots) printf '[{"id":"%s","short_id":"0a1b2c3d","time":"2026-11-16T03:00:00Z","hostname":"pdv2","tags":["pdv2-database"],"paths":["/tmp/pdv2.dump"]}]\\n' "$FAKE_SNAPSHOT" ;;
esac
exit 0
"""

# The loop reaches `sleep` only after one full iteration; the marker tells the
# test that the status files are final.
FAKE_SLEEP = """#!/bin/sh
: > "$FAKE_SLEPT"
exec "$FAKE_REAL_SLEEP" 30
"""

SNAPSHOT = "0a1b2c3d" + "e" * 56


def available_shells():
    found = {}
    for name in ("sh", "dash"):
        path = shutil.which(name)
        if path:
            found.setdefault(os.path.realpath(path), name)
    return sorted(found.values())


class Sandbox:
    def __init__(self, shell):
        self.shell = shell
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.status = self.root / "status"
        self.work = self.root / "work"
        self.bin = self.root / "bin"
        self.log = self.root / "calls.log"
        self.slept = self.root / "slept"
        self.stderr = self.root / "stderr.log"
        for directory in (self.status, self.work, self.bin):
            directory.mkdir()
        for name, body in (("pg_dump", FAKE_PG_DUMP), ("pg_restore", FAKE_PG_RESTORE),
                           ("restic", FAKE_RESTIC), ("sleep", FAKE_SLEEP)):
            target = self.bin / name
            target.write_text(body)
            target.chmod(0o755)

    def close(self):
        self._tmp.cleanup()

    def env(self, fail="", prune="0"):
        return {
            "PATH": f"{self.bin}{os.pathsep}{os.environ.get('PATH', '')}",
            "PGHOST": "db", "PGDATABASE": "pdv2", "PGUSER": "pdv2", "PGPASSWORD": "fake",
            "RESTIC_REPOSITORY": str(self.root / "repository"), "RESTIC_PASSWORD": "fake",
            "BACKUP_STATUS_DIR": str(self.status), "BACKUP_PRUNE": prune,
            "BACKUP_INTERVAL_SECONDS": "3600", "TMPDIR": str(self.work),
            "FAKE_LOG": str(self.log), "FAKE_FAIL": fail, "FAKE_SLEPT": str(self.slept),
            "FAKE_SNAPSHOT": SNAPSHOT, "FAKE_REAL_SLEEP": shutil.which("sleep") or "/bin/sleep",
        }

    def run_once(self, **options):
        return subprocess.run([self.shell, str(SCRIPT), "once"], env=self.env(**options),
                              capture_output=True, text=True, timeout=60)

    def run_loop_iteration(self, **options):
        """Start the hourly loop and stop it when the first iteration is over."""
        with self.stderr.open("w") as stderr:
            process = subprocess.Popen([self.shell, str(SCRIPT)], env=self.env(**options),
                                       stdout=subprocess.DEVNULL, stderr=stderr,
                                       start_new_session=True)
            try:
                deadline = time.monotonic() + 60
                while not self.slept.exists():
                    if process.poll() is not None:
                        raise AssertionError(f"O laço terminou sozinho ({process.returncode}): "
                                             f"{self.stderr.read_text()}")
                    if time.monotonic() > deadline:
                        raise AssertionError("O laço não concluiu a primeira execução")
                    time.sleep(0.02)
            finally:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()
        return self.stderr.read_text()

    def text(self, name):
        path = self.status / name
        return path.read_text() if path.exists() else None

    def calls(self):
        return self.log.read_text().splitlines() if self.log.exists() else []


class BackupScriptCases:
    shell = "sh"

    def sandbox(self):
        box = Sandbox(self.shell)
        self.addCleanup(box.close)
        return box

    def assert_failure_recorded(self, box, message):
        self.assertIsNone(box.text("last-success"), "falha registrada como sucesso")
        self.assertTrue((box.text("last-failure") or "").strip().isdigit())
        self.assertIn(message, box.text("last-error") or "")
        self.assertEqual(list(box.work.iterdir()), [], "dump temporário não foi removido")

    def test_once_success_records_success(self):
        box = self.sandbox()
        result = box.run_once()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(box.text("last-success").strip().isdigit())
        self.assertIsNone(box.text("last-failure"))
        self.assertIsNone(box.text("last-error"))
        self.assertIn(SNAPSHOT, box.text("latest.json"))
        self.assertIn(SNAPSHOT, box.text("snapshots.json"))
        self.assertEqual(list(box.work.iterdir()), [])

    def test_once_failure_exits_nonzero_and_records_failure(self):
        box = self.sandbox()
        result = box.run_once(fail="restic-backup")
        self.assertNotEqual(result.returncode, 0)
        self.assert_failure_recorded(box, "restic backup")
        self.assertIsNone(box.text("latest.json"))

    def test_loop_success_records_success_and_clears_old_failure(self):
        box = self.sandbox()
        (box.status / "last-failure").write_text("1000\n")
        (box.status / "last-error").write_text("Falha antiga")
        box.run_loop_iteration()
        self.assertTrue(box.text("last-success").strip().isdigit())
        self.assertIsNone(box.text("last-failure"))
        self.assertIsNone(box.text("last-error"))

    def test_loop_failure_at_any_stage_never_records_success(self):
        stages = (
            ("pg_dump", "0", "pg_dump"),
            ("pg_restore", "0", "pg_restore --list"),
            ("restic-cat,restic-init", "0", "Repositório Restic"),
            ("restic-backup", "0", "restic backup"),
            ("restic-forget", "1", "restic forget"),
            ("restic-snapshots", "0", "restic snapshots"),
        )
        for fail, prune, message in stages:
            with self.subTest(stage=fail):
                box = self.sandbox()
                stderr = box.run_loop_iteration(fail=fail, prune=prune)
                self.assert_failure_recorded(box, message)
                self.assertIn("Backup failed", stderr)

    def test_loop_failure_keeps_previous_success_older_than_failure(self):
        box = self.sandbox()
        (box.status / "last-success").write_text("1000\n")
        box.run_loop_iteration(fail="pg_dump")
        self.assertEqual(box.text("last-success").strip(), "1000")
        # operations/views_backup.py reports "error" when the failure is newer.
        self.assertGreater(int(box.text("last-failure")), 1000)
        self.assertIn("pg_dump", box.text("last-error"))
        self.assertFalse(any(call.startswith("restic backup") for call in box.calls()))

    def test_missing_repository_is_initialized_before_backup(self):
        box = self.sandbox()
        box.run_loop_iteration(fail="restic-cat")
        calls = [call.split()[1] for call in box.calls() if call.startswith("restic ")]
        self.assertEqual(calls, ["cat", "init", "backup", "snapshots"])
        self.assertTrue(box.text("last-success").strip().isdigit())

    def test_prune_runs_before_snapshot_list_and_success(self):
        box = self.sandbox()
        result = box.run_once(prune="1")
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = [call.split()[1] for call in box.calls() if call.startswith("restic ")]
        self.assertEqual(calls, ["cat", "backup", "forget", "snapshots"])
        self.assertTrue(box.text("last-success").strip().isdigit())


for _shell in available_shells():
    globals()[f"BackupScript_{_shell}_Tests"] = type(
        f"BackupScript_{_shell}_Tests", (BackupScriptCases, unittest.TestCase), {"shell": _shell})


if __name__ == "__main__":
    unittest.main()
