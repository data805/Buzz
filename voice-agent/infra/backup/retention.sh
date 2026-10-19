#!/usr/bin/env bash
# Buzz retention job (docs/PLAN.md section 7). Run nightly by buzz-retention.timer.
#
# DRY RUN BY DEFAULT: prints what it would do and rolls the SQL back.
# Pass --apply to change anything.
#
# Where Dograh v1.49.0 keeps call data (cited in README.md):
#   MinIO bucket voice-audio
#     recordings/{run_id}.wav, recordings/{run_id}/user.wav, recordings/{run_id}/bot.wav
#     transcripts/{run_id}.txt
#     BUT ALSO recordings/{org_id}/{recording_id}/{file}: the pre-recorded prompt
#     audio (workflow_recordings, e.g. the disclosure). Same prefix, so a plain
#     prefix rule on recordings/ would delete the disclosure after 90 days.
#   Postgres workflow_runs: logs.realtime_feedback_events (the transcript turns),
#     gathered_context (extracted variables), initial_context (phone number and
#     contact fields), annotations (QA), recording_url/transcript_url/extra.recordings.
#   workflow_run_text_sessions, webhook_deliveries.payload, embed_sessions.client_ip,
#   queued_runs.context_variables (campaign contact rows).
#
# What it does:
#   1. MinIO lifecycle (mc ilm rule import, fixed rule IDs, idempotent):
#        expire transcripts/            after RETENTION_DAYS
#        expire voicemail_detections/   after RETENTION_DAYS
#        expire objects tagged buzz-class=call-recording after RETENTION_DAYS
#   2. Tag the call recordings of runs from the last TAG_WINDOW_DAYS with
#      buzz-class=call-recording (prompt recordings are never tagged).
#   3. Sweep: for runs older than RETENTION_DAYS not yet anonymised, delete their
#      artefacts explicitly (belt and braces: untagged or pre-tagging objects),
#      then anonymise the database rows (SQL below) in one transaction.
#   4. Asterisk: delete recordings/voicemail older than RETENTION_DAYS.
#   5. Write buzz_retention_last_success_timestamp_seconds (textfile collector).
#
# Usage: retention.sh [--apply] [--backfill-tags] [--batch N]
set -Eeuo pipefail
umask 077

# shellcheck source=lib.sh
source "$(dirname "$0")/lib.sh"
load_env
need docker python3

apply=false backfill=false batch="${RETENTION_BATCH:-2000}"
TAG_WINDOW_DAYS="${TAG_WINDOW_DAYS:-3}"
# gathered_context keys kept after anonymisation (operational, no personal data;
# campaign_client.py and reports_client.py read call_disposition/call_status).
KEEP_CONTEXT_KEYS="${KEEP_CONTEXT_KEYS:-call_id,call_disposition,call_status,call_tags,call_initiation_uncertain}"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --apply) apply=true; shift ;;
    --backfill-tags) backfill=true; shift ;;
    --batch) batch="$2"; shift 2 ;;
    -h|--help) sed -n '2,36p' "$0"; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
done
[[ "$RETENTION_DAYS" =~ ^[0-9]+$ && "$RETENTION_DAYS" -ge 30 ]] || die "RETENTION_DAYS must be an integer >= 30"
[[ "$batch" =~ ^[0-9]+$ ]] || die "--batch must be an integer"
if $apply; then log "MODE: APPLY"; else log "MODE: DRY RUN (nothing will change; pass --apply)"; fi

tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT
pg="$(ctr "$PG_SERVICE")"
psql_q() { docker exec -i "$pg" psql -U "$PG_USER" -d "$PG_DB" -v ON_ERROR_STOP=1 -X -q "$@"; }

# Keys that must never be deleted: prompt/disclosure audio.
protected_sql="SELECT storage_key FROM workflow_recordings"
# Call-artefact key shapes written by api/services/workflow_run_artifacts.py.
CALL_KEY_RE='^(recordings/[0-9]+(\.wav|/(user|bot)\.wav)|transcripts/[0-9]+\.txt)$'

# SQL: object keys of a set of runs (one per line).
keys_sql() { # keys_sql <where clause>
  cat <<SQL
WITH runs AS (SELECT id, recording_url, transcript_url, extra::jsonb AS x FROM workflow_runs WHERE $1)
SELECT k FROM (
  SELECT recording_url AS k FROM runs
  UNION SELECT transcript_url FROM runs
  UNION SELECT CASE jsonb_typeof(x->'recordings'->t) WHEN 'string' THEN x->'recordings'->>t
                                                     ELSE x->'recordings'->t->>'storage_key' END
        FROM runs, unnest(ARRAY['mixed','user','bot']) AS t
) s
WHERE k IS NOT NULL AND k <> '' AND k NOT IN ($protected_sql);
SQL
}

filter_call_keys() { python3 -I -c '
import re, sys
rx = re.compile(sys.argv[1])
for line in sys.stdin:
    k = line.strip()
    if not k: continue
    if rx.match(k): print(k)
    else: print("skip (unexpected key shape): " + k, file=sys.stderr)
' "$CALL_KEY_RE"; }

# --- 1. MinIO lifecycle rules ---------------------------------------------------
cat >"$tmp/lifecycle.json" <<JSON
{"Rules": [
  {"ID": "buzz-expire-transcripts", "Status": "Enabled",
   "Filter": {"Prefix": "transcripts/"}, "Expiration": {"Days": $RETENTION_DAYS}},
  {"ID": "buzz-expire-voicemail-detections", "Status": "Enabled",
   "Filter": {"Prefix": "voicemail_detections/"}, "Expiration": {"Days": $RETENTION_DAYS}},
  {"ID": "buzz-expire-call-recordings", "Status": "Enabled",
   "Filter": {"Tag": {"Key": "buzz-class", "Value": "call-recording"}}, "Expiration": {"Days": $RETENTION_DAYS}}
]}
JSON
log "lifecycle: current rules on $MINIO_BUCKET:"
mc_run ilm rule ls buzz/"$MINIO_BUCKET" >&2 || log "  (none or not readable)"
if $apply; then
  mc_run ilm rule import buzz/"$MINIO_BUCKET" <"$tmp/lifecycle.json" >&2
  mc_run ilm rule ls buzz/"$MINIO_BUCKET" >&2
else
  log "lifecycle: would import:"; cat "$tmp/lifecycle.json" >&2
fi

# --- 2. Tag recent call recordings --------------------------------------------------
if $backfill; then where="true"; else where="created_at >= now() - interval '$TAG_WINDOW_DAYS days'"; fi
keys_sql "$where" | psql_q -At >"$tmp/recent_keys.raw"
grep '^recordings/' "$tmp/recent_keys.raw" | filter_call_keys >"$tmp/recent_keys" || true
log "tagging: $(wc -l <"$tmp/recent_keys" | tr -d ' ') call recordings"
if $apply; then
  # One container for the whole list; a missing object is logged, not fatal.
  # shellcheck disable=SC2016
  mc_sh 'while read -r k; do mc tag set "buzz/$1/$k" "buzz-class=call-recording" >/dev/null 2>&1 || echo "  tag failed (missing?): $k" >&2; done' \
    "$MINIO_BUCKET" <"$tmp/recent_keys"
fi

# --- 3. Sweep expired runs -----------------------------------------------------------
expired_where="created_at < now() - interval '$RETENTION_DAYS days' AND (extra::jsonb -> 'buzz_retention') IS NULL"
psql_q -At -c "SELECT id FROM workflow_runs WHERE $expired_where ORDER BY id LIMIT $batch" >"$tmp/ids"
n_ids="$(grep -c . "$tmp/ids" || true)"
log "sweep: $n_ids runs older than $RETENTION_DAYS days to anonymise (batch limit $batch)"

if [[ "$n_ids" -gt 0 ]]; then
  ids="$(paste -sd, "$tmp/ids")"
  keys_sql "id = ANY('{$ids}'::int[])" | psql_q -At | filter_call_keys >"$tmp/expired_keys"
  log "sweep: $(wc -l <"$tmp/expired_keys" | tr -d ' ') objects to delete"
  if $apply; then
    # Abort before touching the database if any delete fails, so that no
    # object loses its only reference. (rm --force succeeds on missing objects.)
    # shellcheck disable=SC2016
    mc_sh 'rc=0; while read -r k; do mc rm --force "buzz/$1/$k" >/dev/null || { echo "  rm failed: $k" >&2; rc=1; }; done; exit $rc' \
      "$MINIO_BUCKET" <"$tmp/expired_keys" || die "object deletion failed; database left unchanged"
  else
    head -n 20 "$tmp/expired_keys" | sed 's/^/  would delete: /' >&2
  fi

  # Anonymise in one transaction; dry run rolls back after showing counts.
  psql_q -v ids="{$ids}" -v days="$RETENTION_DAYS" -v keep="$KEEP_CONTEXT_KEYS" \
         -v commit="$($apply && echo true || echo false)" <<'SQL'
BEGIN;
CREATE TEMP TABLE buzz_expired ON COMMIT DROP AS
  SELECT unnest(:'ids'::int[]) AS id;

-- Transcript turns, QA output, contact fields, extracted variables, artefact links.
UPDATE workflow_runs r SET
  logs = '{}'::json,
  annotations = '{}'::json,
  initial_context = '{}'::json,
  gathered_context = coalesce(
      (SELECT jsonb_object_agg(key, value) FROM jsonb_each(r.gathered_context::jsonb)
        WHERE key = ANY (string_to_array(:'keep', ','))),
      '{}'::jsonb)::json,
  recording_url = NULL,
  transcript_url = NULL,
  public_access_token = NULL,
  extra = ((r.extra::jsonb - 'recordings')
           || jsonb_build_object('buzz_retention',
                jsonb_build_object('anonymised_at', now(), 'policy_days', :days)))::json
FROM buzz_expired e WHERE r.id = e.id;
\echo workflow_runs anonymised: :ROW_COUNT

DELETE FROM workflow_run_text_sessions WHERE workflow_run_id IN (SELECT id FROM buzz_expired);
\echo workflow_run_text_sessions deleted: :ROW_COUNT

UPDATE webhook_deliveries SET payload = '{}'::json, custom_headers = NULL
 WHERE workflow_run_id IN (SELECT id FROM buzz_expired);
\echo webhook_deliveries cleared: :ROW_COUNT

UPDATE embed_sessions SET client_ip = NULL
 WHERE workflow_run_id IN (SELECT id FROM buzz_expired) AND client_ip IS NOT NULL;
\echo embed_sessions client_ip cleared: :ROW_COUNT

-- Campaign contact rows that have been dialled or have failed.
UPDATE queued_runs SET context_variables = '{}'::json
 WHERE created_at < now() - make_interval(days => :days)
   AND state IN ('processed', 'failed')
   AND context_variables::text <> '{}';
\echo queued_runs cleared: :ROW_COUNT

\if :commit
COMMIT;
\echo committed
\else
ROLLBACK;
\echo dry run: rolled back
\endif
SQL
fi

# --- 4. Asterisk recordings/voicemail ----------------------------------------------------
if ast="$(ctr "$ASTERISK_SERVICE" 2>/dev/null)"; then
  action="-print"; $apply && action="-print -delete"
  # shellcheck disable=SC2016
  docker exec "$ast" sh -c 'for d in /var/spool/asterisk/monitor /var/spool/asterisk/recording /var/spool/asterisk/voicemail; do
      [ -d "$d" ] && find "$d" -type f -mtime +"$1" '"$action"'; done; true' sh "$RETENTION_DAYS" \
    | sed 's/^/  asterisk expired: /' >&2
else
  log "asterisk: container not found, skipped"
fi

# --- 5. Metric --------------------------------------------------------------------------------
if $apply; then
  printf '# HELP buzz_retention_last_success_timestamp_seconds Unix time of the last successful retention run (--apply).\n# TYPE buzz_retention_last_success_timestamp_seconds gauge\nbuzz_retention_last_success_timestamp_seconds %s\n' \
    "$(date +%s)" | write_metrics buzz_retention.prom
fi
log "retention done"
