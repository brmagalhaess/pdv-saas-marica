"""Platform backup status and safe restore validation controls.

The cloud process only writes small request markers. The dedicated backup
container owns Restic credentials and consumes those markers, so the web
application never receives a repository password or a raw backup archive.
Restore requests always target a temporary database and never overwrite the
live database.
"""
import json
import os
import re
import time
from datetime import datetime, timezone as dt_timezone
from pathlib import Path

from django.http import JsonResponse

from core.api_common import APIError, audit, endpoint, fields, method, payload, platform_user, text


SNAPSHOT_ID = re.compile(r"^[0-9a-f]{8,64}$")


def status_dir():
    return Path(os.environ.get("PDV_BACKUP_STATUS_DIR", "/backup-status"))


def read_text(name):
    try:
        return (status_dir() / name).read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return ""


def read_json(name, default):
    try:
        value = json.loads((status_dir() / name).read_text(encoding="utf-8"))
        return value
    except (OSError, UnicodeError, ValueError, TypeError):
        return default


def instant(value):
    try:
        return datetime.fromtimestamp(float(value), tz=dt_timezone.utc).isoformat()
    except (TypeError, ValueError, OverflowError):
        return None


def snapshot_rows():
    raw = read_json("snapshots.json", [])
    if not isinstance(raw, list):
        return []
    rows = []
    for row in raw:
        if not isinstance(row, dict) or not SNAPSHOT_ID.fullmatch(str(row.get("id", ""))):
            continue
        rows.append({
            "id": row["id"],
            "short_id": row.get("short_id") or str(row["id"])[:8],
            "time": row.get("time"),
            "hostname": row.get("hostname") or "pdv2",
            "tags": row.get("tags") if isinstance(row.get("tags"), list) else [],
            "paths": row.get("paths") if isinstance(row.get("paths"), list) else [],
        })
    return rows[:30]


def backup_payload():
    success = read_text("last-success")
    failure = read_text("last-failure")
    try:
        interval = max(60, int(os.environ.get("BACKUP_INTERVAL_SECONDS", "3600")))
    except ValueError:
        interval = 3600
    now = time.time()
    success_value = float(success) if success.replace(".", "", 1).isdigit() else None
    failure_value = float(failure) if failure.replace(".", "", 1).isdigit() else None
    if not success_value:
        state = "unknown"
    elif failure_value and failure_value > success_value:
        state = "error"
    elif now - success_value > interval * 2:
        state = "stale"
    else:
        state = "healthy"
    restore = read_json("restore-report.json", {})
    if not isinstance(restore, dict):
        restore = {}
    pending = ""
    if read_text("restore-request"):
        pending = "restore"
    elif read_text("request-now"):
        pending = "backup"
    return {
        "state": state,
        "last_success_at": instant(success_value),
        "last_failure_at": instant(failure_value),
        "last_failure": read_text("last-error"),
        "interval_seconds": interval,
        "pending_action": pending,
        "external_configured": os.environ.get("PDV_BACKUP_OFFSITE") == "1",
        "snapshots": snapshot_rows(),
        "restore": restore,
    }


def _write_marker(name, value):
    directory = status_dir()
    try:
        directory.mkdir(parents=True, exist_ok=True)
        temporary = directory / ("." + name + ".tmp")
        temporary.write_text(value, encoding="ascii")
        temporary.replace(directory / name)
    except OSError as exc:
        raise APIError("O serviço de backup não está disponível para receber a solicitação", 503) from exc


@endpoint
def backups(request):
    method(request, "GET", "POST")
    user = platform_user(request, roles=("platform_ops",))
    if request.method == "GET":
        return JsonResponse(backup_payload())

    data = payload(request)
    fields(data, ("action", "snapshot_id", "confirm"), ("action",))
    action = text(data["action"], "Ação", 30)
    if action == "run":
        if backup_payload()["pending_action"]:
            raise APIError("Já existe uma ação de backup aguardando execução", 409)
        _write_marker("request-now", str(int(time.time())))
        audit(None, user, "backup.requested", detail={"action": "run"})
        return JsonResponse({"accepted": True, "action": "run"}, status=202)
    if action != "restore_test":
        raise APIError("Ação de backup inválida")
    if data.get("confirm") is not True:
        raise APIError("Confirme a restauração isolada antes de continuar")
    snapshot_id = text(data.get("snapshot_id", ""), "Snapshot", 64)
    if not SNAPSHOT_ID.fullmatch(snapshot_id):
        raise APIError("Snapshot inválido")
    if snapshot_id not in {row["id"] for row in snapshot_rows()}:
        raise APIError("Snapshot não encontrado no inventário atual", 404)
    if backup_payload()["pending_action"]:
        raise APIError("Já existe uma ação de backup aguardando execução", 409)
    _write_marker("restore-request", snapshot_id + "\n")
    audit(None, user, "backup.restore_requested", detail={"snapshot_id": snapshot_id})
    return JsonResponse({"accepted": True, "action": "restore_test", "snapshot_id": snapshot_id}, status=202)
