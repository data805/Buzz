#!/usr/bin/env bash
# Render Prometheus file_sd targets and secret files from .env.
#
# Prometheus does not substitute environment variables in prometheus.yml, so
# every address that depends on the deployment is written here into
# prometheus/targets/*.json (file_sd; Prometheus re-reads them without a
# restart). Secrets go into secrets/ (git-ignored), readable only by the
# Prometheus/Alertmanager container user (nobody, uid 65534).
#
# Usage:  sudo ./render.sh [path/to/.env]      (default: ./.env)
#         ./render.sh --check [path/to/.env]   validate .env, write nothing
set -euo pipefail

cd "$(dirname "$0")"

check_only=false
if [[ "${1:-}" == "--check" ]]; then check_only=true; shift; fi
env_file="${1:-.env}"
[[ -f "$env_file" ]] || { echo "render.sh: $env_file not found (copy .env.example)" >&2; exit 1; }

set -a
# shellcheck disable=SC1090
source "$env_file"
set +a

: "${INFERENCE_HOST:?INFERENCE_HOST must be set}"
: "${DOGRAH_DEVOPS_SECRET:?DOGRAH_DEVOPS_SECRET must be set (same value as in infra/app/.env)}"
: "${ASTERISK_METRICS_PASSWORD:?ASTERISK_METRICS_PASSWORD must be set (same value as Asterisk prometheus.conf)}"
DOGRAH_API_SERVICE="${DOGRAH_API_SERVICE:-api}"
FASTAPI_WORKERS="${FASTAPI_WORKERS:-1}"
APP_NODE_EXPORTER_LISTEN="${APP_NODE_EXPORTER_LISTEN:-172.30.0.1:9100}"
ASTERISK_HTTP="${ASTERISK_HTTP:-172.30.0.1:8088}"
SUPPRESSION_INTERNAL="${SUPPRESSION_INTERNAL:-suppression:8080}"
VLLM_PORT="${VLLM_PORT:-8000}"
SPEACHES_PORT="${SPEACHES_PORT:-8001}"
DCGM_PORT="${DCGM_PORT:-9400}"
INFERENCE_NODE_EXPORTER_PORT="${INFERENCE_NODE_EXPORTER_PORT:-9100}"
ALERT_WEBHOOK_URL="${ALERT_WEBHOOK_URL:-}"

[[ "$FASTAPI_WORKERS" =~ ^[1-9][0-9]*$ ]] || { echo "FASTAPI_WORKERS must be a positive integer" >&2; exit 1; }
for v in INFERENCE_HOST DOGRAH_API_SERVICE APP_NODE_EXPORTER_LISTEN ASTERISK_HTTP SUPPRESSION_INTERNAL; do
  [[ "${!v}" =~ ^[A-Za-z0-9._:-]+$ ]] || { echo "$v has unexpected characters: ${!v}" >&2; exit 1; }
done

if $check_only; then echo "render.sh: $env_file looks complete"; exit 0; fi

umask 022
mkdir -p prometheus/targets secrets

write() { # write <file> ; content on stdin; atomic replace
  local tmp
  tmp="$(mktemp "$1.XXXXXX")"
  cat >"$tmp"
  chmod 0644 "$tmp"
  mv -f "$tmp" "$1"
}

# Dograh: one target per uvicorn worker (ports 8000, 8001, ...).
dograh_targets=""
for ((i = 0; i < FASTAPI_WORKERS; i++)); do
  dograh_targets+="${dograh_targets:+, }\"${DOGRAH_API_SERVICE}:$((8000 + i))\""
done

write prometheus/targets/dograh.json <<EOF
[{"targets": [${dograh_targets}], "labels": {"host": "app", "service": "dograh"}}]
EOF
write prometheus/targets/vllm.json <<EOF
[{"targets": ["${INFERENCE_HOST}:${VLLM_PORT}"], "labels": {"host": "inference", "service": "vllm"}}]
EOF
write prometheus/targets/dcgm.json <<EOF
[{"targets": ["${INFERENCE_HOST}:${DCGM_PORT}"], "labels": {"host": "inference", "service": "dcgm"}}]
EOF
write prometheus/targets/node.json <<EOF
[
  {"targets": ["${APP_NODE_EXPORTER_LISTEN}"], "labels": {"host": "app"}},
  {"targets": ["${INFERENCE_HOST}:${INFERENCE_NODE_EXPORTER_PORT}"], "labels": {"host": "inference"}}
]
EOF
write prometheus/targets/asterisk.json <<EOF
[{"targets": ["${ASTERISK_HTTP}"], "labels": {"host": "app", "service": "asterisk"}}]
EOF
write prometheus/targets/suppression.json <<EOF
[{"targets": ["${SUPPRESSION_INTERNAL}"], "labels": {"host": "app", "service": "suppression"}}]
EOF
write prometheus/targets/blackbox_http.json <<EOF
[
  {"targets": ["http://${INFERENCE_HOST}:${SPEACHES_PORT}/health"], "labels": {"host": "inference", "service": "speaches"}},
  {"targets": ["http://${INFERENCE_HOST}:${VLLM_PORT}/health"], "labels": {"host": "inference", "service": "vllm"}},
  {"targets": ["http://${DOGRAH_API_SERVICE}:8000/api/v1/health"], "labels": {"host": "app", "service": "dograh"}},
  {"targets": ["http://${SUPPRESSION_INTERNAL}/health"], "labels": {"host": "app", "service": "suppression"}},
  {"targets": ["http://grafana:3000/api/health"], "labels": {"host": "app", "service": "grafana"}}
]
EOF

# Secrets: no trailing newline (Prometheus header/password files are used verbatim
# apart from trailing whitespace trimming; keep them exact anyway).
secret() { # secret <name> <value>
  local f="secrets/$1"
  ( umask 077; printf '%s' "$2" >"$f.tmp" )
  if [[ $EUID -eq 0 ]]; then
    chown 65534:65534 "$f.tmp"; chmod 0400 "$f.tmp"
  else
    echo "render.sh: not root; $f left 0600 for $(id -un). The containers run as uid 65534 and" >&2
    echo "           will not be able to read it. Re-run with sudo on the server." >&2
  fi
  mv -f "$f.tmp" "$f"
}
secret dograh_devops_secret "$DOGRAH_DEVOPS_SECRET"
secret asterisk_metrics_password "$ASTERISK_METRICS_PASSWORD"
if [[ -z "$ALERT_WEBHOOK_URL" ]]; then
  echo "render.sh: ALERT_WEBHOOK_URL is empty; alerts will show in Alertmanager (:9093) but go nowhere." >&2
  ALERT_WEBHOOK_URL="http://127.0.0.1:9/alert-webhook-not-configured"
fi
secret alertmanager_webhook_url "$ALERT_WEBHOOK_URL"

echo "render.sh: wrote prometheus/targets/*.json and secrets/*"
echo "           apply with: docker compose up -d && curl -fsS -X POST http://127.0.0.1:9090/-/reload"
