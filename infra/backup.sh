#!/bin/sh
set -eu
umask 077
: "${PGHOST:?PGHOST required}" "${PGDATABASE:?PGDATABASE required}" "${PGUSER:?PGUSER required}" "${PGPASSWORD:?PGPASSWORD required}"
: "${RESTIC_REPOSITORY:?RESTIC_REPOSITORY required}" "${RESTIC_PASSWORD:?RESTIC_PASSWORD required}"
mkdir -p /status
# The cloud service writes short-lived request markers in this shared volume.
# No credentials or backup contents are stored there.
chmod 0777 /status 2>/dev/null || true
write_error() {
  printf '%s' "$1" > /status/last-error.tmp
  mv /status/last-error.tmp /status/last-error
  chmod 644 /status/last-error
}
write_restore_report() {
  status="$1"
  snapshot="$2"
  message="${3:-}"
  completed=$(date -u +%s)
  printf '{"status":"%s","snapshot_id":"%s","completed_at":%s,"message":"%s"}\n' \
    "$status" "$snapshot" "$completed" "$message" > /status/restore-report.json.tmp
  mv /status/restore-report.json.tmp /status/restore-report.json
  chmod 644 /status/restore-report.json
}
write_snapshots() {
  restic snapshots --json --tag pdv2-database > /status/snapshots.json.tmp
  mv /status/snapshots.json.tmp /status/snapshots.json
  chmod 644 /status/snapshots.json
}
backup_once() {
  workdir=$(mktemp -d)
  trap 'rm -rf "$workdir"' EXIT HUP INT TERM
  pg_dump --format=custom --no-owner --file="$workdir/pdv2.dump"
  pg_restore --list "$workdir/pdv2.dump" >/dev/null
  if ! restic cat config >/dev/null 2>&1; then restic init; fi
  restic backup "$workdir/pdv2.dump" --tag pdv2-database --host pdv2 --json > /status/latest.json.tmp
  mv /status/latest.json.tmp /status/latest.json
  write_snapshots
  date -u +%s > /status/last-success
  chmod 644 /status/last-success
  rm -f /status/last-failure /status/last-error
  if [ "${BACKUP_PRUNE:-0}" = "1" ]; then
    restic forget --tag pdv2-database --group-by host,tags --keep-daily 7 --keep-weekly 4 --keep-monthly 12 --prune
  fi
  rm -rf "$workdir"
  trap - EXIT HUP INT TERM
}
restore_test() {
  snapshot=$(cat /status/restore-request 2>/dev/null || true)
  rm -f /status/restore-request
  case "$snapshot" in
    ''|*[!0-9a-f]*) write_restore_report "failed" "$snapshot" "Snapshot inválido"; return 1 ;;
  esac
  workdir=$(mktemp -d)
  trap 'rm -rf "$workdir"' EXIT HUP INT TERM
  dump="$workdir/pdv2.dump"
  dump_path=$(restic ls "$snapshot" --json | sed -n 's/.*"path":"\([^"]*\/pdv2\.dump\)".*/\1/p' | head -n 1)
  if [ -z "$dump_path" ]; then
    write_restore_report "failed" "$snapshot" "Snapshot sem dump PostgreSQL"
    write_error "Snapshot de restauração não contém o dump PostgreSQL esperado"
    return 1
  fi
  if ! restic dump "$snapshot" "$dump_path" > "$dump" 2>"$workdir/error"; then
    write_restore_report "failed" "$snapshot" "Não foi possível extrair o snapshot"
    write_error "Falha ao extrair snapshot de restauração"
    return 1
  fi
  if ! pg_restore --list "$dump" >/dev/null 2>"$workdir/error"; then
    write_restore_report "failed" "$snapshot" "Dump inválido"
    write_error "Dump restaurado não passou na validação pg_restore"
    return 1
  fi
  restore_db="pdv2_restore_$(date -u +%s)_$$"
  if ! createdb "$restore_db" 2>"$workdir/error"; then
    write_restore_report "failed" "$snapshot" "Banco temporário não pôde ser criado"
    write_error "Banco temporário de restauração não pôde ser criado"
    return 1
  fi
  if ! pg_restore --exit-on-error --no-owner --no-privileges --dbname="$restore_db" "$dump" 2>"$workdir/error"; then
    dropdb --if-exists "$restore_db" >/dev/null 2>&1 || true
    write_restore_report "failed" "$snapshot" "Restauração no banco temporário falhou"
    write_error "Restauração isolada falhou"
    return 1
  fi
  dropdb --if-exists "$restore_db"
  write_restore_report "passed" "$snapshot" "Restauração isolada validada; banco temporário removido"
  rm -rf "$workdir"
  trap - EXIT HUP INT TERM
}
wait_until_due() {
  remaining="${BACKUP_INTERVAL_SECONDS:-3600}"
  while [ "$remaining" -gt 0 ]; do
    if [ -f /status/request-now ] || [ -f /status/restore-request ]; then return; fi
    sleep 1
    remaining=$((remaining - 1))
  done
}
if [ "${1:-}" = "once" ]; then backup_once; exit 0; fi
while true; do
  if [ -f /status/restore-request ]; then
    if ! (restore_test); then date -u +%s > /status/last-failure; chmod 644 /status/last-failure; echo 'Restore validation failed; see status files' >&2; fi
  else
    rm -f /status/request-now
    if ! (backup_once); then date -u +%s > /status/last-failure; chmod 644 /status/last-failure; write_error "Backup failed"; echo 'Backup failed; see preceding error' >&2; fi
  fi
  wait_until_due
done
