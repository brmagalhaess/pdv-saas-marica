#!/usr/bin/env python3
"""Validate an encrypted Restic backup by restoring it into a disposable DB.

The live database is never a restore target. The compose project and env file
are configurable so the same procedure runs locally or against production.
"""
import argparse
import json
import re
import secrets
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--compose", type=Path, default=ROOT / "compose.local.yaml",
                        help="Docker Compose file (default: compose.local.yaml)")
    parser.add_argument("--env-file", type=Path, default=ROOT / ".runtime/local.env",
                        help="Compose env file (default: .runtime/local.env)")
    parser.add_argument("--report", type=Path, default=ROOT / ".runtime/backup-restore-report.json",
                        help="Write a machine-readable verification report")
    parser.add_argument("--require-external", action="store_true",
                        help="Fail unless RESTIC_REPOSITORY is a remote URL")
    return parser.parse_args()


def env_value(path, key):
    if not path.exists():
        return ""
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        if name.strip() == key:
            return value.strip().strip("'\"")
    return ""


def main():
    args = parse_args()
    compose_path = args.compose if args.compose.is_absolute() else ROOT / args.compose
    env_path = args.env_file if args.env_file.is_absolute() else ROOT / args.env_file
    if not compose_path.exists():
        raise SystemExit(f"Compose não encontrado: {compose_path}")
    compose = ["docker", "compose", "--env-file", str(env_path), "-f", str(compose_path)]

    repository = env_value(env_path, "RESTIC_REPOSITORY")
    if args.require_external and (not repository or re.match(r"^/|^file:", repository)):
        raise SystemExit("RESTIC_REPOSITORY precisa apontar para um destino externo (S3/B2/compatível)")

    def run(command):
        return subprocess.run(compose + command, cwd=ROOT, check=True,
                              capture_output=True, text=True).stdout

    name = "pdv2_restore_" + secrets.token_hex(6)
    if not re.fullmatch(r"pdv2_restore_[a-f0-9]{12}", name):
        raise RuntimeError("Nome de banco temporário inválido")

    print("Executando backup criptografado no destino configurado...")
    run(["exec", "-T", "backup", "pdv2-backup", "once"])
    snapshots_raw = run(["exec", "-T", "backup", "restic", "snapshots",
                         "--tag", "pdv2-database", "--json"])
    snapshots = json.loads(snapshots_raw)
    if not snapshots:
        raise RuntimeError("Nenhum snapshot pdv2-database foi encontrado")
    snapshot_row = max(snapshots, key=lambda row: row["time"])
    snapshot = snapshot_row["id"]
    listing = run(["exec", "-T", "backup", "restic", "ls", snapshot, "--json"]).splitlines()
    files = [json.loads(line) for line in listing if line.strip()]
    dump = next((row["path"] for row in files
                 if row.get("type") == "file" and row.get("path", "").endswith("/pdv2.dump")), None)
    if not dump:
        raise RuntimeError("O snapshot não contém o dump PostgreSQL esperado")

    created = False
    try:
        run(["exec", "-T", "db", "createdb", "-U", "pdv2", name])
        created = True
        reader = subprocess.Popen(compose + ["exec", "-T", "backup", "restic", "dump", snapshot, dump],
                                  cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        restore = subprocess.run(compose + ["exec", "-T", "db", "pg_restore", "-U", "pdv2",
                                             "--dbname", name, "--no-owner", "--no-privileges",
                                             "--exit-on-error"], cwd=ROOT, stdin=reader.stdout,
                                  capture_output=True)
        reader.stdout.close()
        reader.wait()
        if reader.returncode or restore.returncode:
            details = (restore.stderr or b"").decode(errors="replace")[-1000:]
            raise RuntimeError(f"Restauração falhou; banco principal preservado. {details}")
        result = run(["exec", "-T", "db", "psql", "-U", "pdv2", "-d", name, "-tA", "-c",
                      "SELECT json_build_object('tenants',(SELECT count(*) FROM core_tenant),"
                      "'products',(SELECT count(*) FROM core_product),'sales',(SELECT count(*) FROM core_sale),"
                      "'migrations',(SELECT count(*) FROM django_migrations))"]).strip()
        counts = json.loads(result)
        if counts["migrations"] < 20:
            raise RuntimeError("Schema restaurado incompleto")
        report = {"repository": repository or "compose-configured", "snapshot": snapshot,
                  "snapshot_time": snapshot_row.get("time"),
                  "restored_database": "temporary database removed after verification",
                  "counts": counts, "result": "PASS"}
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2) + "\n")
        args.report.chmod(0o600)
        print("Backup criptografado restaurado em banco isolado: PASS")
        print(json.dumps(counts, ensure_ascii=False))
    finally:
        if created:
            run(["exec", "-T", "db", "dropdb", "-U", "pdv2", name])


if __name__ == "__main__":
    main()
