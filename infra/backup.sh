#!/bin/sh
set -eu
umask 077
# Secrets come from files written by secrets-init (/secrets). A variable that
# is set (old installs) wins over its file.
if [ -z "${PGPASSWORD:-}" ] && [ -n "${PGPASSWORD_FILE:-}" ]; then
  PGPASSWORD=$(cat "$PGPASSWORD_FILE") || { echo "PGPASSWORD_FILE ilegível: $PGPASSWORD_FILE" >&2; exit 1; }
  export PGPASSWORD
fi
unset PGPASSWORD_FILE
if [ -n "${RESTIC_PASSWORD:-}" ]; then
  unset RESTIC_PASSWORD_FILE
else
  unset RESTIC_PASSWORD
  if [ -n "${RESTIC_PASSWORD_FILE:-}" ] && [ ! -r "$RESTIC_PASSWORD_FILE" ]; then
    echo "RESTIC_PASSWORD_FILE ilegível: $RESTIC_PASSWORD_FILE" >&2; exit 1
  fi
fi
: "${PGHOST:?PGHOST required}" "${PGDATABASE:?PGDATABASE required}" "${PGUSER:?PGUSER required}" "${PGPASSWORD:?PGPASSWORD or PGPASSWORD_FILE required}"
: "${RESTIC_REPOSITORY:?RESTIC_REPOSITORY required}"
if [ -z "${RESTIC_PASSWORD:-}" ] && [ -z "${RESTIC_PASSWORD_FILE:-}" ]; then
  echo "RESTIC_PASSWORD or RESTIC_PASSWORD_FILE required" >&2; exit 1
fi
# Server secrets (secrets-init volume, mounted read-only) go into their own
# snapshot, tag pdv2-secrets. They are encrypted by restic like the dump.
SECRETS_DIR="${BACKUP_SECRETS_DIR:-/secrets}"
STATUS_DIR="${BACKUP_STATUS_DIR:-/status}"
REASON_FILE="$STATUS_DIR/.backup-reason.$$"
mkdir -p "$STATUS_DIR"
# The cloud service writes short-lived request markers in this shared volume.
# No credentials or backup contents are stored there.
chmod 0777 "$STATUS_DIR" 2>/dev/null || true
write_error() {
  printf '%s' "$1" > "$STATUS_DIR/last-error.tmp"
  mv "$STATUS_DIR/last-error.tmp" "$STATUS_DIR/last-error"
  chmod 644 "$STATUS_DIR/last-error"
}
write_restore_report() {
  status="$1"
  snapshot="$2"
  message="${3:-}"
  completed=$(date -u +%s)
  printf '{"status":"%s","snapshot_id":"%s","completed_at":%s,"message":"%s"}\n' \
    "$status" "$snapshot" "$completed" "$message" > "$STATUS_DIR/restore-report.json.tmp"
  mv "$STATUS_DIR/restore-report.json.tmp" "$STATUS_DIR/restore-report.json"
  chmod 644 "$STATUS_DIR/restore-report.json"
}
write_snapshots() {
  restic snapshots --json --tag pdv2-database > "$STATUS_DIR/snapshots.json.tmp" || return 1
  mv "$STATUS_DIR/snapshots.json.tmp" "$STATUS_DIR/snapshots.json" || return 1
  chmod 644 "$STATUS_DIR/snapshots.json"
}
# Aborts the backup and keeps the reason for the status file. Every stage calls
# it explicitly: `set -e` is ignored inside anything tested by if/!/&&/||, so
# relying on it alone let failed backups be recorded as successful.
fail() {
  printf '%s' "$1" > "$REASON_FILE" 2>/dev/null || true
  echo "$1" >&2
  exit 1
}
# A database without Django migrations (backup starts with the db, before
# migrate) has nothing to protect; backing it up would only make an empty
# snapshot the newest one, the one a restore picks first.
schema_ready() {
  ready=$(psql -X -q -t -A -v ON_ERROR_STOP=1 -c "SELECT to_regclass('public.django_migrations') IS NOT NULL") \
    || fail "Falha ao consultar o banco antes do backup (psql)"
  if [ "$ready" = "t" ]; then
    ready=$(psql -X -q -t -A -v ON_ERROR_STOP=1 -c "SELECT EXISTS (SELECT 1 FROM django_migrations)") \
      || fail "Falha ao consultar o banco antes do backup (psql)"
  fi
  [ "$ready" = "t" ]
}
backup_once() {
  if ! schema_ready; then
    echo "Banco ainda sem migrações; backup adiado até o próximo ciclo" >&2
    return 0
  fi
  workdir=$(mktemp -d) || fail "Não foi possível criar o diretório temporário do backup"
  trap 'rm -rf "$workdir"' EXIT HUP INT TERM
  pg_dump --format=custom --no-owner --file="$workdir/pdv2.dump" \
    || fail "Falha ao gerar o dump do PostgreSQL (pg_dump)"
  pg_restore --list "$workdir/pdv2.dump" >/dev/null \
    || fail "Dump gerado não passou na validação (pg_restore --list)"
  if ! restic cat config >/dev/null 2>&1; then
    restic init || fail "Repositório Restic inacessível ou não pôde ser inicializado"
  fi
  restic backup "$workdir/pdv2.dump" --tag pdv2-database --host pdv2 --json > "$STATUS_DIR/latest.json.tmp" \
    || fail "Falha ao enviar a cópia para o repositório Restic (restic backup)"
  mv "$STATUS_DIR/latest.json.tmp" "$STATUS_DIR/latest.json" \
    || fail "Não foi possível gravar o estado do backup"
  if [ -d "$SECRETS_DIR" ]; then
    restic backup "$SECRETS_DIR" --tag pdv2-secrets --host pdv2 --quiet >/dev/null \
      || fail "Falha ao enviar os segredos do servidor para o repositório Restic (restic backup $SECRETS_DIR)"
  fi
  if [ "${BACKUP_PRUNE:-0}" = "1" ]; then
    # Repeated --tag is OR; --group-by tags keeps dump and secrets apart.
    restic forget --tag pdv2-database --tag pdv2-secrets --group-by host,tags --keep-daily 7 --keep-weekly 4 --keep-monthly 12 --prune \
      || fail "Cópia enviada, mas a limpeza de snapshots antigos falhou (restic forget)"
  fi
  write_snapshots || fail "Cópia enviada, mas não foi possível listar os snapshots (restic snapshots)"
  # Success is recorded only here, after every stage above returned zero.
  date -u +%s > "$STATUS_DIR/last-success.tmp" || fail "Não foi possível gravar o estado do backup"
  mv "$STATUS_DIR/last-success.tmp" "$STATUS_DIR/last-success" || fail "Não foi possível gravar o estado do backup"
  chmod 644 "$STATUS_DIR/last-success"
  rm -f "$STATUS_DIR/last-failure" "$STATUS_DIR/last-error"
  rm -rf "$workdir"
  trap - EXIT HUP INT TERM
}
# Runs one backup and leaves its exit status in $backup_rc. It always returns
# zero so callers use it as a plain command; never put backup_once itself in
# an if/!/&&/|| context. The subshell re-arms `set -e` as a second safety net
# for commands without an explicit check.
run_backup() {
  rm -f "$REASON_FILE"
  set +e
  ( set -e; backup_once )
  backup_rc=$?
  set -e
  if [ "$backup_rc" -ne 0 ]; then
    reason=$(cat "$REASON_FILE" 2>/dev/null || true)
    rm -f "$REASON_FILE" "$STATUS_DIR/last-success.tmp"
    date -u +%s > "$STATUS_DIR/last-failure"
    chmod 644 "$STATUS_DIR/last-failure"
    write_error "${reason:-Backup interrompido por erro inesperado; consulte o log do serviço de backup}"
    echo 'Backup failed; see preceding error' >&2
  fi
}
restore_test() {
  snapshot=$(cat "$STATUS_DIR/restore-request" 2>/dev/null || true)
  rm -f "$STATUS_DIR/restore-request"
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
  if ! dropdb --if-exists "$restore_db" 2>"$workdir/error"; then
    write_restore_report "failed" "$snapshot" "Banco temporário não pôde ser removido"
    write_error "Banco temporário de restauração não pôde ser removido: $restore_db"
    return 1
  fi
  write_restore_report "passed" "$snapshot" "Restauração isolada validada; banco temporário removido"
  rm -rf "$workdir"
  trap - EXIT HUP INT TERM
}
wait_until_due() {
  remaining="${BACKUP_INTERVAL_SECONDS:-3600}"
  while [ "$remaining" -gt 0 ]; do
    if [ -f "$STATUS_DIR/request-now" ] || [ -f "$STATUS_DIR/restore-request" ]; then return; fi
    sleep 1
    remaining=$((remaining - 1))
  done
}
if [ "${1:-}" = "once" ]; then run_backup; exit "$backup_rc"; fi
while true; do
  if [ -f "$STATUS_DIR/restore-request" ]; then
    if ! (restore_test); then date -u +%s > "$STATUS_DIR/last-failure"; chmod 644 "$STATUS_DIR/last-failure"; echo 'Restore validation failed; see status files' >&2; fi
  else
    rm -f "$STATUS_DIR/request-now"
    if [ "${BACKUP_PAUSADO:-0}" = "1" ]; then
      # Restore on a new VPS: no snapshot and no prune until the owner is done.
      echo "Backup automático pausado (BACKUP_PAUSADO=1); nenhum snapshot enviado" >&2
    else
      run_backup
    fi
  fi
  wait_until_due
done
