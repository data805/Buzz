#!/usr/bin/env bash
# Buzz nightly backup (app host). Run by buzz-backup.timer as root.
#
#   1. Dograh Postgres      pg_dump -Fc inside the postgres container
#   2. Suppression SQLite   sqlite3 online backup API (Python stdlib) inside its container
#   3. Asterisk             tar of config, voicemail and recordings inside its container
#   4. MinIO bucket         mc mirror: full on WEEKLY_DAY (or when no full exists), else
#                           objects changed in the last MINIO_INCREMENTAL_WINDOW
#
# Every artefact is streamed straight into `age` (public-key encryption): no
# plaintext copy touches the backup disk except the MinIO staging mirror, which
# lives under BACKUP_ROOT/.staging and is deleted as soon as it is archived.
# The server holds only the public recipients; the private key is offline.
#
# Rotation: daily/<stamp> keeps KEEP_DAILY (14); on WEEKLY_DAY the run is also
# hard-linked to weekly/<ISO week>, keeping KEEP_WEEKLY (8).
# Metrics (node-exporter textfile): buzz_backup_last_success_timestamp_seconds etc.
#
# Usage: backup.sh [--only postgres|suppression|asterisk|minio] [--full]
set -Eeuo pipefail
umask 077

# shellcheck source=lib.sh
source "$(dirname "$0")/lib.sh"
load_env
need docker age sha256sum

only="" force_full=false
while [[ $# -gt 0 ]]; do
  case "$1" in
    --only) only="$2"; shift 2 ;;
    --full) force_full=true; shift ;;
    -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
done
want() { [[ -z "$only" || "$only" == "$1" ]]; }

[[ -s "$AGE_RECIPIENTS_FILE" ]] || die "no age recipients in $AGE_RECIPIENTS_FILE (see README)"

start_ts="$(date +%s)"
stamp="$(date -u +%Y-%m-%dT%H%M%SZ)"
mkdir -p "$BACKUP_ROOT"/{daily,weekly,.staging}
exec 9>"$BACKUP_ROOT/.lock"
flock -n 9 || die "another backup is running"

work="$BACKUP_ROOT/.inprogress-$stamp"
mkdir -p "$work"

declare -A sizes=()
last_success="$(cat "$BACKUP_ROOT/.last_success" 2>/dev/null || echo 0)"

metrics() { # metrics <success 0|1>
  local now dur
  now="$(date +%s)"; dur=$(( now - start_ts ))
  {
    echo "# HELP buzz_backup_last_success_timestamp_seconds Unix time of the last fully successful backup."
    echo "# TYPE buzz_backup_last_success_timestamp_seconds gauge"
    echo "buzz_backup_last_success_timestamp_seconds $last_success"
    echo "# HELP buzz_backup_last_run_timestamp_seconds Unix time the last backup run finished."
    echo "# TYPE buzz_backup_last_run_timestamp_seconds gauge"
    echo "buzz_backup_last_run_timestamp_seconds $now"
    echo "# HELP buzz_backup_last_run_success 1 if the last backup run succeeded."
    echo "# TYPE buzz_backup_last_run_success gauge"
    echo "buzz_backup_last_run_success $1"
    echo "# HELP buzz_backup_last_duration_seconds Duration of the last backup run."
    echo "# TYPE buzz_backup_last_duration_seconds gauge"
    echo "buzz_backup_last_duration_seconds $dur"
    echo "# HELP buzz_backup_last_size_bytes Encrypted size of each component in the last run."
    echo "# TYPE buzz_backup_last_size_bytes gauge"
    for k in "${!sizes[@]}"; do echo "buzz_backup_last_size_bytes{component=\"$k\"} ${sizes[$k]}"; done
  } | write_metrics buzz_backup.prom
}

on_error() {
  local rc=$? line=$1
  log "backup FAILED (exit $rc at line $line); partial run left in $work.failed"
  mv -f "$work" "$work.failed" 2>/dev/null || true
  rm -rf "$BACKUP_ROOT/.staging/"* 2>/dev/null || true
  [[ -n "$only" ]] || metrics 0
  exit "$rc"
}
trap 'on_error $LINENO' ERR

record() { sizes[$1]="$(stat -c %s "$work/$2")"; log "  $2: ${sizes[$1]} bytes"; }

# --- 1. Postgres -------------------------------------------------------------
if want postgres; then
  log "postgres: pg_dump $PG_DB"
  pg="$(ctr "$PG_SERVICE")"
  docker exec "$pg" pg_dump -U "$PG_USER" -d "$PG_DB" -Fc --compress=6 | encrypt "$work/postgres.dump.age"
  docker exec "$pg" pg_dumpall -U "$PG_USER" --globals-only | encrypt "$work/postgres-globals.sql.age"
  record postgres postgres.dump.age
  # Schema revision, for the manifest and restore --verify (not personal data).
  docker exec "$pg" psql -U "$PG_USER" -d "$PG_DB" -Atc 'SELECT version_num FROM alembic_version' \
    >"$work/alembic_version.txt" || true
fi

# --- 2. Suppression SQLite ------------------------------------------------------
if want suppression; then
  log "suppression: online backup of $SUPPRESSION_DB_PATH"
  sup="$(ctr "$SUPPRESSION_SERVICE")"
  tmpdb="/tmp/buzz-backup-$stamp.db"
  docker exec "$sup" python3 -c '
import sqlite3, sys
src = sqlite3.connect(sys.argv[1]); dst = sqlite3.connect(sys.argv[2])
with dst: src.backup(dst)
ok = dst.execute("PRAGMA integrity_check").fetchone()[0]
dst.close(); src.close()
sys.exit(0 if ok == "ok" else "integrity_check: " + ok)
' "$SUPPRESSION_DB_PATH" "$tmpdb"
  docker exec "$sup" cat "$tmpdb" | encrypt "$work/suppression.sqlite.age"
  docker exec "$sup" rm -f "$tmpdb"
  record suppression suppression.sqlite.age
fi

# --- 3. Asterisk --------------------------------------------------------------------
if want asterisk; then
  log "asterisk: $ASTERISK_PATHS"
  ast="$(ctr "$ASTERISK_SERVICE")"
  # Only the paths that exist; tar runs inside the container (paths relative to /).
  # shellcheck disable=SC2016
  docker exec "$ast" sh -c 'cd / && p=""; for d in $1; do [ -e "$d" ] && p="$p $d"; done; [ -n "$p" ] || exit 3; exec tar -czf - $p' sh "$ASTERISK_PATHS" \
    | encrypt "$work/asterisk.tar.gz.age"
  record asterisk asterisk.tar.gz.age
fi

# --- 4. MinIO --------------------------------------------------------------------------
if want minio; then
  mode=incremental
  if $force_full || [[ "$(date +%u)" == "$WEEKLY_DAY" ]] \
     || ! compgen -G "$BACKUP_ROOT/daily/*/minio-full.tar.age" >/dev/null; then
    mode=full
  fi
  log "minio: $mode mirror of $MINIO_BUCKET"
  stage="$BACKUP_ROOT/.staging/$stamp"
  mkdir -p "$stage"
  args=(mirror --quiet --preserve buzz/"$MINIO_BUCKET" /mirror/"$MINIO_BUCKET")
  [[ "$mode" == incremental ]] && args+=(--newer-than "$MINIO_INCREMENTAL_WINDOW")
  MC_EXTRA_DOCKER_ARGS="-v $stage:/mirror --user $(id -u):$(id -g)" mc_run "${args[@]}"
  tar -C "$stage" -cf - . | encrypt "$work/minio-$mode.tar.age"
  rm -rf "$stage"
  record minio "minio-$mode.tar.age"
fi

# --- Manifest, publish, rotate ------------------------------------------------------
(
  cd "$work"
  {
    echo "stamp=$stamp"
    echo "host=$(hostname -f 2>/dev/null || hostname)"
    echo "only=${only:-all}"
    [[ -f alembic_version.txt ]] && echo "alembic_version=$(cat alembic_version.txt)"
    echo "age_recipients_sha256=$(sha256sum "$AGE_RECIPIENTS_FILE" | cut -d' ' -f1)"
  } >manifest.txt
  sha256sum -- *.age >SHA256SUMS
)
chmod 0700 "$work"
final="$BACKUP_ROOT/daily/$stamp"
mv "$work" "$final"
log "published $final"

if [[ -z "$only" && "$(date +%u)" == "$WEEKLY_DAY" ]]; then
  week="$(date -u +%G-W%V)"
  rm -rf "$BACKUP_ROOT/weekly/$week"
  cp -al "$final" "$BACKUP_ROOT/weekly/$week"
  log "weekly copy: weekly/$week"
fi

prune() { # prune <dir> <keep>
  local dir="$1" keep="$2"
  ls -1d "$dir"/*/ 2>/dev/null | sort | head -n "-$keep" | while read -r old; do
    log "rotate: removing $old"; rm -rf -- "$old"
  done
}
# KEEP_DAILY (14) > 7 days, so the latest weekly MinIO full that later incrementals
# depend on is always still in daily/ (and in weekly/).
prune "$BACKUP_ROOT/daily" "$KEEP_DAILY"
prune "$BACKUP_ROOT/weekly" "$KEEP_WEEKLY"
find "$BACKUP_ROOT" -maxdepth 1 -name '.inprogress-*.failed' -mtime +7 -exec rm -rf {} +

# Optional off-host copy (encrypted files only).
if [[ -n "${OFFSITE_RSYNC_TARGET:-}" ]]; then
  log "offsite: rsync to $OFFSITE_RSYNC_TARGET"
  rsync -a --delete --exclude '.*' "$BACKUP_ROOT/" "$OFFSITE_RSYNC_TARGET/"
fi

if [[ -z "$only" ]]; then
  last_success="$(date +%s)"
  echo "$last_success" >"$BACKUP_ROOT/.last_success"
  metrics 1
fi
log "backup OK in $(( $(date +%s) - start_ts ))s"
