#!/usr/bin/env bash
# Bring Buzz up on this Mac: secrets, model, containers, speech models, Dograh setup.
# Safe to re-run; every step is idempotent.
#   BASE_MODEL=qwen2.5:7b-instruct ./scripts/up.sh   # 7B needs a 32 GB Mac
set -euo pipefail
cd "$(dirname "$0")/.."
REPO=$(cd ../.. && pwd)
BASE_MODEL="${BASE_MODEL:-qwen2.5:3b-instruct}"

say() { printf '\n== %s\n' "$*"; }
die() { echo "error: $*" >&2; exit 1; }

command -v docker >/dev/null || die "docker not found (start Colima or Docker Desktop)"
docker info >/dev/null 2>&1 || die "docker is not running"
command -v ollama >/dev/null || die "ollama not found (brew install ollama)"

say "Memory check"
# On a 16 GB Mac, heavy swap makes speech synthesis 10-20x slower and Dograh drops the audio
# ("TTS context completed with no audio"). Warn before starting rather than after a bad call.
swap_used=$(sysctl -n vm.swapusage | sed -E 's/.*used = ([0-9.]+)M.*/\1/')
if (( ${swap_used%.*} > 2500 )); then
  echo "warning: ${swap_used%.*} MB of swap in use. Quit Chrome and other heavy apps before testing calls,"
  echo "         or reboot, otherwise replies will lag and audio may drop."
fi

say "Secrets"
./scripts/gen-env.sh
set -a; . ./.env; set +a

say "Language model (Ollama, Metal GPU)"
curl -fsS http://127.0.0.1:11434/api/version >/dev/null 2>&1 \
  || { echo "starting ollama serve"; nohup ollama serve >/dev/null 2>&1 & sleep 3; }
ollama list | grep -q "^${BASE_MODEL}" || ollama pull "$BASE_MODEL"
# buzz-llm = the base model with room for the Buzz prompt plus a long call (8k context).
mf=$(mktemp); printf 'FROM %s\nPARAMETER num_ctx 8192\n' "$BASE_MODEL" > "$mf"
ollama create "$LLM_MODEL_NAME" -f "$mf" >/dev/null; rm -f "$mf"
# Load it now and keep it resident, so the first call is not a 15 s cold start.
curl -fsS http://127.0.0.1:11434/api/generate -d "{\"model\":\"$LLM_MODEL_NAME\",\"prompt\":\"hi\",\"stream\":false,\"keep_alive\":-1,\"options\":{\"num_predict\":1}}" >/dev/null

say "Containers"
docker compose up -d --build
echo -n "waiting for the api"
for _ in $(seq 1 60); do
  curl -fsS http://127.0.0.1:8000/api/v1/health >/dev/null 2>&1 && break
  echo -n .; sleep 3
done
curl -fsS http://127.0.0.1:8000/api/v1/health >/dev/null || die "api did not become healthy: docker compose logs api"
echo " ok"

say "Speech models (Speaches, CPU)"
for m in "$STT_MODEL" "$TTS_MODEL"; do
  curl -fsS -X POST "http://127.0.0.1:8001/v1/models/$m" \
    -H "Authorization: Bearer $SPEACHES_API_KEY" >/dev/null || echo "could not fetch $m (needs internet once)"
done

say "Warm up speech models"
# First use loads each model (seconds on a laptop); do it now, not on the caller's greeting.
curl -fsS -X POST localhost:8001/v1/audio/speech -H "Authorization: Bearer $SPEACHES_API_KEY" -H 'Content-Type: application/json' \
  -d "{\"model\":\"$TTS_MODEL\",\"voice\":\"$TTS_VOICE\",\"input\":\"Hello.\",\"response_format\":\"pcm\"}" -o /dev/null || echo "TTS warm-up failed"
python3 - <<PY || echo "STT warm-up failed"
import io,wave,httpx
b=io.BytesIO(); w=wave.open(b,"wb"); w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000); w.writeframes(b"\0\0"*16000); w.close()
httpx.post("http://127.0.0.1:8001/v1/audio/transcriptions",headers={"Authorization":"Bearer $SPEACHES_API_KEY"},
  files={"file":("a.wav",b.getvalue(),"audio/wav")},data={"model":"$STT_MODEL"},timeout=180).raise_for_status()
PY

say "Admin user"
# Local auth with signup enabled. The route is not verified against the 1.49.0 source:
# if this fails, create the user in the UI at http://localhost:3010 with the .env credentials.
curl -fsS -X POST http://127.0.0.1:8000/api/v1/auth/signup \
  -H 'Content-Type: application/json' \
  -d "{\"email\":\"$DOGRAH_ADMIN_EMAIL\",\"password\":\"$DOGRAH_ADMIN_PASSWORD\",\"name\":\"Buzz Admin\"}" \
  >/dev/null 2>&1 || echo "signup skipped (user may already exist)"

say "Connect Dograh to the local models"
python3 "$REPO/tools/dograh/configure_providers.py" --env-file .env --skip-telephony
python3 "$REPO/tools/dograh/configure_providers.py" --env-file .env --skip-telephony --check

say "Front-desk agent"
python3 "$REPO/tools/dograh/import_workflow.py" "$REPO/agents/front-desk/workflow.json" --env-file .env

cat <<MSG

Buzz is up: http://localhost:3010
Login:  $DOGRAH_ADMIN_EMAIL  (password in infra/local/.env)
Next:   open the 'Buzz front desk' agent and use Test Audio in the browser.
Note:   the UI's "Create agent" button needs Dograh's cloud and will fail by design;
        add agents with tools/dograh/import_workflow.py <workflow.json>.
Stop:   docker compose down        Wipe: docker compose down -v
MSG
