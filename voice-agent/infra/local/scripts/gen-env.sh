#!/usr/bin/env bash
# Write infra/local/.env with fresh random secrets (once; never overwrites).
set -euo pipefail
cd "$(dirname "$0")/.."
[[ -e .env ]] && { echo ".env already exists, leaving it alone"; exit 0; }
r() { openssl rand -hex 24; }
umask 077
cat > .env <<ENV
POSTGRES_PASSWORD=$(r)
REDIS_PASSWORD=$(r)
MINIO_ROOT_USER=buzz$(openssl rand -hex 4)
MINIO_ROOT_PASSWORD=$(r)
OSS_JWT_SECRET=$(openssl rand -hex 32)
TELEPHONY_WS_TOKEN_SECRET=$(r)
TURN_SECRET=$(r)
DOGRAH_DEVOPS_SECRET=$(r)
SUPPRESSION_API_KEY=$(r)
SPEACHES_API_KEY=$(r)
# Local model settings (Piper + Whisper base: Kokoro was too slow on a laptop CPU)
INFERENCE_HOST=host.docker.internal
INFERENCE_LLM_PORT=11434
INFERENCE_SPEACHES_PORT=8001
VLLM_API_KEY=ollama
LLM_MODEL_NAME=buzz-llm
STT_MODEL=Systran/faster-whisper-base.en
TTS_MODEL=speaches-ai/piper-en_GB-alba-medium
TTS_VOICE=alba
DOGRAH_API_URL=http://127.0.0.1:8000
DOGRAH_ADMIN_EMAIL=admin@bongohive.co.zm
DOGRAH_ADMIN_PASSWORD=$(r)
ENV
echo "wrote .env (git-ignored)"
