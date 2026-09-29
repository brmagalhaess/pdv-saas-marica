"""Creates and verifies a consistent SQLite backup; storage encryption is still required."""
import hashlib
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

from app import DB_PATH


def backup(destination):
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    target = destination / f"pdv-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.sqlite3"
    if target.exists():
        raise FileExistsError(target)
    source = sqlite3.connect(DB_PATH)
    copy = sqlite3.connect(target)
    try:
        source.backup(copy)
        result = copy.execute("PRAGMA integrity_check").fetchone()[0]
        if result != "ok":
            raise RuntimeError(f"Backup inválido: {result}")
    finally:
        copy.close()
        source.close()
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    target.with_suffix(".sha256").write_text(f"{digest}  {target.name}\n")
    return target


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("Uso: python3 backup.py DIRETORIO_DE_BACKUP")
    print(backup(sys.argv[1]))
