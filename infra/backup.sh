#!/bin/sh
set -eu
umask 077
: "${PGHOST:?PGHOST required}" "${PGDATABASE:?PGDATABASE required}" "${PGUSER:?PGUSER required}" "${PGPASSWORD:?PGPASSWORD required}"
: "${RESTIC_REPOSITORY:?RESTIC_REPOSITORY required}" "${RESTIC_PASSWORD:?RESTIC_PASSWORD required}"
mkdir -p /status
backup_once() {
  workdir=$(mktemp -d)
  trap 'rm -rf "$workdir"' EXIT HUP INT TERM
  pg_dump --format=custom --no-owner --file="$workdir/pdv2.dump"
  pg_restore --list "$workdir/pdv2.dump" >/dev/null
  if ! restic cat config >/dev/null 2>&1; then restic init; fi
  restic backup "$workdir/pdv2.dump" --tag pdv2-database --host pdv2 --json > /status/latest.json.tmp
  mv /status/latest.json.tmp /status/latest.json
  date -u +%s > /status/last-success
  chmod 644 /status/last-success
  if [ "${BACKUP_PRUNE:-0}" = "1" ]; then
    restic forget --tag pdv2-database --group-by host,tags --keep-daily 7 --keep-weekly 4 --keep-monthly 12 --prune
  fi
  rm -rf "$workdir"
  trap - EXIT HUP INT TERM
}
if [ "${1:-}" = "once" ]; then backup_once; exit 0; fi
while true; do
  if ! (backup_once); then date -u +%s > /status/last-failure; chmod 644 /status/last-failure; echo 'Backup failed; see preceding error' >&2; fi
  sleep "${BACKUP_INTERVAL_SECONDS:-3600}"
done
