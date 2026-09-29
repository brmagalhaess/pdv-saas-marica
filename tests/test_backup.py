import hashlib
import sqlite3
import tempfile
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app
import backup


class BackupTests(unittest.TestCase):
    def test_backup_can_be_restored_with_original_data(self):
        previous_app_db, previous_backup_db = app.DB_PATH, backup.DB_PATH
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = root / "original.sqlite3"
            app.DB_PATH = original
            backup.DB_PATH = original
            app.init_db()
            with app.db() as conn:
                conn.execute("INSERT INTO tenants VALUES ('tenant-a','Mercado A',1)")
                conn.commit()
            archived = backup.backup(root / "archives")
            self.assertTrue(archived.exists())
            expected = archived.with_suffix(".sha256").read_text().split()[0]
            self.assertEqual(hashlib.sha256(archived.read_bytes()).hexdigest(), expected)
            restored = root / "restored.sqlite3"
            restored.write_bytes(archived.read_bytes())
            with sqlite3.connect(restored) as conn:
                self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                self.assertEqual(conn.execute("SELECT name FROM tenants WHERE id='tenant-a'").fetchone()[0], "Mercado A")
        app.DB_PATH, backup.DB_PATH = previous_app_db, previous_backup_db


if __name__ == "__main__":
    unittest.main()
