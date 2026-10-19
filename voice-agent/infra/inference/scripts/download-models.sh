#!/usr/bin/env bash
# Download the LLM, STT and TTS models into the Docker volumes, so that the
# runtime stack can stay offline (HF_HUB_OFFLINE=1).
#
# Run in a maintenance window, with outbound HTTPS to Hugging Face allowed for
# the duration (huggingface.co and its download CDN hosts; check the egress
# allow-list against what the run actually contacts).
#
# Mechanism per server, taken from each project's source:
#   vLLM:     huggingface_hub.snapshot_download() inside the vLLM image, into
#             the volume vLLM reads (/root/.cache/huggingface).
#   Speaches: its own download API, POST /v1/models/{model_id}
#             (speaches routers/models.py: 200 downloaded, 201 already present,
#             404 not in its Hugging Face registry). A temporary Speaches
#             container runs with HF_HUB_OFFLINE=0 and no published ports.
#
# The resolved model revisions are written into versions.lock.
#
# Usage: scripts/download-models.sh [--env-file PATH] [--skip-pull]
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
env_file="${here}/.env"
pull=1

while [[ $# -gt 0 ]]; do
  case "$1" in
    --env-file) env_file="$2"; shift 2 ;;
    --skip-pull) pull=0; shift ;;
    -h|--help) sed -n '2,21p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

[[ -f "${env_file}" ]] || { echo "missing ${env_file}; copy .env.example first" >&2; exit 1; }
command -v docker >/dev/null || { echo "docker not found" >&2; exit 1; }

# Read the values we need from the env file without executing it.
env_get() {
  local key="$1" default="${2:-}" line
  line="$(grep -E "^${key}=" "${env_file}" | tail -n 1 || true)"
  line="${line#*=}"
  line="${line%\"}"; line="${line#\"}"
  echo "${line:-${default}}"
}

LLM_MODEL_ID="$(env_get LLM_MODEL_ID Qwen/Qwen2.5-7B-Instruct-AWQ)"
STT_MODEL="$(env_get STT_MODEL deepdml/faster-whisper-large-v3-turbo-ct2)"
TTS_MODEL="$(env_get TTS_MODEL speaches-ai/Kokoro-82M-v1.0-ONNX)"
SPEACHES_API_KEY="$(env_get SPEACHES_API_KEY)"
[[ -n "${SPEACHES_API_KEY}" ]] || { echo "SPEACHES_API_KEY is empty in ${env_file}" >&2; exit 1; }

compose=(docker compose --project-directory "${here}" -f "${here}/docker-compose.yml" --env-file "${env_file}")
lock_file="${here}/versions.lock"
dl_name="buzz-speaches-download"

cleanup() { docker rm -f "${dl_name}" >/dev/null 2>&1 || true; }
trap cleanup EXIT

# Record a model revision in versions.lock (column 4 of the matching line).
lock_model() {
  local name="$1" repo="$2" rev="$3" tmp
  [[ -f "${lock_file}" ]] || return 0
  tmp="$(mktemp)"
  awk -v n="${name}" -v r="${repo}" -v v="${rev}" '
    $1 == "model" && $2 == n { printf "%-6s %-14s %-56s %s\n", "model", n, r, v; next }
    { print }' "${lock_file}" > "${tmp}"
  cat "${tmp}" > "${lock_file}"
  rm -f "${tmp}"
}

if [[ "${pull}" -eq 1 ]]; then
  echo "==> Pulling images"
  "${compose[@]}" pull
fi

# --- LLM ----------------------------------------------------------------------
echo "==> LLM: ${LLM_MODEL_ID}"
# The Python script goes in on stdin and the repo id in the environment, so no
# argument can be mistaken for a Compose option.
llm_path="$("${compose[@]}" run --rm --no-deps -T \
  -e HF_HUB_OFFLINE=0 -e HF_HUB_DISABLE_TELEMETRY=1 -e "REPO_ID=${LLM_MODEL_ID}" \
  --entrypoint python3 vllm - <<'PY' | tail -n 1
import os
from huggingface_hub import snapshot_download
print(snapshot_download(repo_id=os.environ["REPO_ID"]))
PY
)"
echo "    snapshot: ${llm_path}"
lock_model llm "${LLM_MODEL_ID}" "$(basename "${llm_path}")"

# --- STT and TTS (Speaches) -------------------------------------------------------
echo "==> Starting a temporary Speaches container (online, no published ports)"
cleanup
"${compose[@]}" run -d --no-deps --name "${dl_name}" \
  -e HF_HUB_OFFLINE=0 speaches >/dev/null

for _ in $(seq 1 60); do
  if docker exec "${dl_name}" curl -sf -o /dev/null http://127.0.0.1:8000/health; then
    break
  fi
  sleep 2
done
docker exec "${dl_name}" curl -sf -o /dev/null http://127.0.0.1:8000/health \
  || { echo "Speaches did not become healthy" >&2; docker logs --tail 50 "${dl_name}" >&2; exit 1; }

for pair in "stt:${STT_MODEL}" "tts:${TTS_MODEL}"; do
  name="${pair%%:*}"
  model="${pair#*:}"
  echo "==> ${name}: ${model}"
  # The key goes in through the environment, not the docker exec arguments.
  code="$(docker exec -e "K=${SPEACHES_API_KEY}" "${dl_name}" sh -c \
    "curl -s -o /tmp/dl.out -w '%{http_code}' -X POST -H \"Authorization: Bearer \$K\" http://127.0.0.1:8000/v1/models/${model}")"
  body="$(docker exec "${dl_name}" cat /tmp/dl.out || true)"
  echo "    HTTP ${code}: ${body}"
  case "${code}" in
    200|201) ;;
    *) echo "download failed for ${model}" >&2; exit 1 ;;
  esac
  repo_dir="models--${model//\//--}"
  rev="$(docker exec "${dl_name}" cat "/home/ubuntu/.cache/huggingface/hub/${repo_dir}/refs/main" 2>/dev/null || echo unknown)"
  echo "    revision: ${rev}"
  lock_model "${name}" "${model}" "${rev}"
done

echo "==> Models Speaches sees locally"
docker exec -e "K=${SPEACHES_API_KEY}" "${dl_name}" sh -c \
  'curl -sf -H "Authorization: Bearer $K" http://127.0.0.1:8000/v1/models' \
  | python3 -c 'import sys,json; [print("   ", m["id"]) for m in json.load(sys.stdin)["data"]]' \
  || echo "    (could not list models)"

echo "==> Done. Close the Hugging Face egress rule, then: docker compose --env-file .env up -d"
