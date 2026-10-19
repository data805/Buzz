# shellcheck shell=bash
# Shared helpers for backup.sh, restore.sh and retention.sh. Source, don't run.

BACKUP_DIR_SELF="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

log()  { printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >&2; }
die()  { log "ERROR: $*"; exit 1; }

# load_env: infra/backup/.env (or $BUZZ_BACKUP_ENV), then defaults.
load_env() {
  local env_file="${BUZZ_BACKUP_ENV:-$BACKUP_DIR_SELF/.env}"
  if [[ -f "$env_file" ]]; then
    set -a
    # shellcheck disable=SC1090
    source "$env_file"
    set +a
  fi
  # Credentials for Postgres/MinIO come from the app stack's .env.
  if [[ -n "${BUZZ_APP_ENV:-}" && -f "$BUZZ_APP_ENV" ]]; then
    set -a
    # shellcheck disable=SC1090
    source "$BUZZ_APP_ENV"
    set +a
  fi

  BACKUP_ROOT="${BACKUP_ROOT:-/srv/buzz-backups}"
  AGE_RECIPIENTS_FILE="${AGE_RECIPIENTS_FILE:-/etc/buzz/backup/age-recipients.txt}"
  TEXTFILE_DIR="${TEXTFILE_DIR:-/var/lib/node_exporter/textfile_collector}"
  DOCKER_NETWORK="${DOCKER_NETWORK:-buzz-app}"

  PG_SERVICE="${PG_SERVICE:-postgres}"
  PG_USER="${PG_USER:-postgres}"
  PG_DB="${PG_DB:-postgres}"
  MINIO_SERVICE="${MINIO_SERVICE:-minio}"
  MINIO_ENDPOINT="${MINIO_ENDPOINT_INTERNAL:-http://minio:9000}"
  MINIO_BUCKET="${MINIO_BUCKET:-voice-audio}"
  MC_IMAGE="${MC_IMAGE:-buzz/mc:RELEASE.2025-08-13T08-35-41Z}"
  SUPPRESSION_SERVICE="${SUPPRESSION_SERVICE:-suppression}"
  SUPPRESSION_DB_PATH="${SUPPRESSION_DB_PATH:-/data/suppression.db}"
  ASTERISK_SERVICE="${ASTERISK_SERVICE:-asterisk}"
  ASTERISK_PATHS="${ASTERISK_PATHS:-etc/asterisk var/spool/asterisk/voicemail var/spool/asterisk/monitor var/spool/asterisk/recording var/lib/asterisk/sounds/buzz}"

  KEEP_DAILY="${KEEP_DAILY:-14}"
  KEEP_WEEKLY="${KEEP_WEEKLY:-8}"
  WEEKLY_DAY="${WEEKLY_DAY:-7}"            # ISO weekday, 7 = Sunday
  MINIO_INCREMENTAL_WINDOW="${MINIO_INCREMENTAL_WINDOW:-2d}"
  RETENTION_DAYS="${RETENTION_DAYS:-90}"
  VERIFY_PG_IMAGE="${VERIFY_PG_IMAGE:-pgvector/pgvector:0.8.7-pg17-bookworm}"
}

need() { for c in "$@"; do command -v "$c" >/dev/null 2>&1 || die "missing command: $c"; done; }

# ctr <compose service>: container id, by <SERVICE>_CONTAINER override or Compose label.
ctr() {
  local svc="$1" var id
  var="$(tr '[:lower:]-' '[:upper:]_' <<<"$svc")_CONTAINER"
  if [[ -n "${!var:-}" ]]; then printf '%s\n' "${!var}"; return; fi
  id="$(docker ps -q --filter "label=com.docker.compose.service=$svc" | head -n1)"
  [[ -n "$id" ]] || die "no running container for service '$svc' (set $var in .env)"
  printf '%s\n' "$id"
}

# mc_run <args...>: MinIO client in a throwaway container on the app network.
# Credentials go in MC_HOST_buzz; the password must be URL-safe (openssl rand -hex).
mc_run() {
  : "${MINIO_ROOT_USER:?MINIO_ROOT_USER not set (BUZZ_APP_ENV)}"
  : "${MINIO_ROOT_PASSWORD:?MINIO_ROOT_PASSWORD not set (BUZZ_APP_ENV)}"
  local scheme="${MINIO_ENDPOINT%%://*}" hostport="${MINIO_ENDPOINT#*://}"
  docker run --rm -i --network "$DOCKER_NETWORK" \
    -e "MC_HOST_buzz=${scheme}://${MINIO_ROOT_USER}:${MINIO_ROOT_PASSWORD}@${hostport}" \
    ${MC_EXTRA_DOCKER_ARGS:-} "$MC_IMAGE" "$@"
}

# mc_sh '<sh script>' [args...]: run a shell loop around mc in one container
# (stdin is passed through), instead of one `docker run` per object.
mc_sh() {
  local script="$1"; shift
  MC_EXTRA_DOCKER_ARGS="${MC_EXTRA_DOCKER_ARGS:-} --entrypoint sh" mc_run -c "$script" sh "$@"
}

# write_metrics <file basename> ; Prometheus text on stdin, atomic.
write_metrics() {
  local dest="$TEXTFILE_DIR/$1" tmp
  mkdir -p "$TEXTFILE_DIR"
  tmp="$(mktemp "$TEXTFILE_DIR/.$1.XXXXXX")"
  cat >"$tmp"
  chmod 0644 "$tmp"
  mv -f "$tmp" "$dest"
}

encrypt() { age -R "$AGE_RECIPIENTS_FILE" -o "$1"; }   # stdin -> $1
