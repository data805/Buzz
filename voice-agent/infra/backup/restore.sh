#!/usr/bin/env bash
# Buzz restore tooling. Needs the age PRIVATE key (AGE_IDENTITY_FILE), which is
# kept offline: bring it in for the drill or the restore, then remove it.
#
#   restore.sh --verify [BACKUP_DIR]
#       Restore drill (Phase acceptance test). Checks SHA256SUMS, restores the
#       Postgres dump into a throwaway container (no network) and runs sanity
#       queries, checks the SQLite copy with integrity_check, lists the Asterisk
#       and MinIO archives. Touches nothing live. Default BACKUP_DIR: newest daily.
#
#   restore.sh --extract BACKUP_DIR OUT_DIR
#       Decrypt every artefact into OUT_DIR (0700) for a manual restore; see
#       README.md "Full restore" for the order of operations.
#
#   restore.sh --restore-postgres BACKUP_DIR --i-understand-this-overwrites-live-data
#       Drop and recreate the live Dograh database from the dump. Stop the Dograh
#       api/worker containers first.
set -Eeuo pipefail
umask 077

# shellcheck source=lib.sh
source "$(dirname "$0")/lib.sh"
load_env
need docker age sha256sum python3

: "${AGE_IDENTITY_FILE:=}"
latest_daily() { ls -1d "$BACKUP_ROOT"/daily/*/ 2>/dev/null | sort | tail -n1 | sed 's:/$::'; }

decrypt() { # decrypt <file.age>  -> stdout
  [[ -n "$AGE_IDENTITY_FILE" && -r "$AGE_IDENTITY_FILE" ]] \
    || die "set AGE_IDENTITY_FILE to the offline age private key"
  age -d -i "$AGE_IDENTITY_FILE" "$1"
}

check_sums() {
  log "checking SHA256SUMS in $1"
  (cd "$1" && sha256sum --quiet -c SHA256SUMS) || die "checksum mismatch in $1"
}

verify() {
  local dir="${1:-$(latest_daily)}" ctrname tmp fail=0
  [[ -d "$dir" ]] || die "backup dir not found: $dir"
  log "restore drill on $dir"
  cat "$dir/manifest.txt" >&2 || true
  check_sums "$dir"

  tmp="$(mktemp -d)"
  ctrname="buzz-restore-verify-$$"
  cleanup() { docker rm -f "$ctrname" >/dev/null 2>&1 || true; rm -rf "$tmp"; }
  trap cleanup EXIT

  # --- Postgres into a throwaway container ---
  if [[ -f "$dir/postgres.dump.age" ]]; then
    log "postgres: starting $VERIFY_PG_IMAGE (network none)"
    docker run -d --rm --name "$ctrname" --network none \
      -e POSTGRES_PASSWORD=verify -e POSTGRES_DB=restorecheck "$VERIFY_PG_IMAGE" >/dev/null
    for _ in $(seq 1 60); do
      docker exec "$ctrname" pg_isready -U postgres -d restorecheck >/dev/null 2>&1 && break
      sleep 1
    done
    docker exec "$ctrname" pg_isready -U postgres -d restorecheck >/dev/null || die "verify postgres did not start"
    log "postgres: pg_restore"
    decrypt "$dir/postgres.dump.age" \
      | docker exec -i "$ctrname" pg_restore -U postgres -d restorecheck --no-owner --exit-on-error
    log "postgres: sanity queries"
    docker exec -i "$ctrname" psql -U postgres -d restorecheck -v ON_ERROR_STOP=1 -At -F ' | ' <<'SQL' | tee "$tmp/pg.txt" >&2
SELECT 'alembic_version', version_num FROM alembic_version;
SELECT 'extension_vector', count(*) FROM pg_extension WHERE extname = 'vector';
SELECT 'organizations', count(*) FROM organizations;
SELECT 'users', count(*) FROM users;
SELECT 'workflows', count(*) FROM workflows;
SELECT 'workflow_runs', count(*) FROM workflow_runs;
SELECT 'workflow_runs_latest', coalesce(max(created_at)::text, 'none') FROM workflow_runs;
SELECT 'workflow_recordings', count(*) FROM workflow_recordings WHERE is_active;
SELECT 'provider_connections', count(*) FROM provider_connections;
SELECT 'telephony_configurations', count(*) FROM telephony_configurations;
SQL
    grep -q '^alembic_version | .\+' "$tmp/pg.txt" || { log "FAIL: no alembic_version row"; fail=1; }
    grep -q '^extension_vector | 1$' "$tmp/pg.txt"  || { log "FAIL: pgvector extension missing"; fail=1; }
    grep -q '^users | [1-9]' "$tmp/pg.txt"          || { log "FAIL: no users"; fail=1; }
    grep -q '^organizations | [1-9]' "$tmp/pg.txt"  || { log "FAIL: no organizations"; fail=1; }
    if grep -q '^alembic_version=' "$dir/manifest.txt"; then
      want="$(sed -n 's/^alembic_version=//p' "$dir/manifest.txt")"
      grep -q "^alembic_version | $want$" "$tmp/pg.txt" || { log "FAIL: alembic version differs from manifest ($want)"; fail=1; }
    fi
    docker rm -f "$ctrname" >/dev/null
  else
    log "FAIL: no postgres.dump.age"; fail=1
  fi

  # --- Suppression SQLite ---
  if [[ -f "$dir/suppression.sqlite.age" ]]; then
    decrypt "$dir/suppression.sqlite.age" >"$tmp/suppression.db"
    python3 -I - "$tmp/suppression.db" <<'PY' >&2 || fail=1
import sqlite3, sys
db = sqlite3.connect(sys.argv[1])
ok = db.execute("PRAGMA integrity_check").fetchone()[0]
print("suppression integrity_check:", ok)
for (t,) in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"):
    print(f"suppression table {t}: {db.execute(f'SELECT count(*) FROM \"{t}\"').fetchone()[0]} rows")
sys.exit(0 if ok == "ok" else 1)
PY
  else
    log "WARN: no suppression.sqlite.age"
  fi

  # --- Archives: decrypt and list (proves key + integrity) ---
  if [[ -f "$dir/asterisk.tar.gz.age" ]]; then
    n="$(decrypt "$dir/asterisk.tar.gz.age" | tar -tzf - | wc -l)"; log "asterisk archive: $n entries"
  fi
  for f in "$dir"/minio-*.tar.age; do
    [[ -f "$f" ]] || continue
    n="$(decrypt "$f" | tar -tf - | grep -c -v '/$' || true)"; log "$(basename "$f"): $n objects"
  done

  if [[ $fail -ne 0 ]]; then
    log "restore drill FAILED"; exit 1
  fi
  printf '# HELP buzz_restore_verify_last_success_timestamp_seconds Unix time of the last passing restore drill.\n# TYPE buzz_restore_verify_last_success_timestamp_seconds gauge\nbuzz_restore_verify_last_success_timestamp_seconds %s\n' \
    "$(date +%s)" | write_metrics buzz_restore_verify.prom || true
  log "restore drill PASSED for $dir"
}

extract() {
  local dir="$1" out="$2"
  [[ -d "$dir" ]] || die "backup dir not found: $dir"
  check_sums "$dir"
  mkdir -p "$out"; chmod 0700 "$out"
  for f in "$dir"/*.age; do
    log "decrypting $(basename "$f")"
    decrypt "$f" >"$out/$(basename "${f%.age}")"
  done
  log "extracted into $out (plaintext personal data: delete when done)"
}

restore_postgres() {
  local dir="$1" pg
  [[ -f "$dir/postgres.dump.age" ]] || die "no postgres.dump.age in $dir"
  check_sums "$dir"
  pg="$(ctr "$PG_SERVICE")"
  log "restoring $PG_DB in container $pg from $dir"
  docker exec "$pg" psql -U "$PG_USER" -d template1 -v ON_ERROR_STOP=1 -c \
    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '$PG_DB' AND pid <> pg_backend_pid();" >/dev/null
  if [[ "$PG_DB" == "postgres" ]]; then
    # Cannot drop the maintenance DB we are connected through: clean-restore instead.
    decrypt "$dir/postgres.dump.age" \
      | docker exec -i "$pg" pg_restore -U "$PG_USER" -d "$PG_DB" --clean --if-exists --exit-on-error
  else
    docker exec "$pg" dropdb -U "$PG_USER" --if-exists "$PG_DB"
    docker exec "$pg" createdb -U "$PG_USER" "$PG_DB"
    decrypt "$dir/postgres.dump.age" \
      | docker exec -i "$pg" pg_restore -U "$PG_USER" -d "$PG_DB" --exit-on-error
  fi
  log "postgres restored; start Dograh and check /api/v1/health"
}

case "${1:-}" in
  --verify) verify "${2:-}" ;;
  --extract) [[ $# -eq 3 ]] || die "usage: --extract BACKUP_DIR OUT_DIR"; extract "$2" "$3" ;;
  --restore-postgres)
    [[ "${3:-}" == "--i-understand-this-overwrites-live-data" ]] || die "add --i-understand-this-overwrites-live-data"
    restore_postgres "$2" ;;
  *) sed -n '2,19p' "$0"; exit 2 ;;
esac
