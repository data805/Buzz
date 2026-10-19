# Inference host runbook

Runs the GPU side of Buzz: **vLLM** (LLM, `buzz-llm`) and **Speaches** (STT faster-whisper large-v3-turbo, TTS Kokoro), plus the DCGM and node exporters. The design is in `docs/PLAN.md` sections 2–4. Ports, addresses and variable names follow `docs/INTERFACES.md`.

| Port | Service | Auth |
|---|---|---|
| `${INFERENCE_LAN_IP}:8000` | vLLM, OpenAI API at `/v1` | `Authorization: Bearer ${VLLM_API_KEY}`; `/health` and `/metrics` are open |
| `${INFERENCE_LAN_IP}:8001` | Speaches, OpenAI-compatible `/v1/audio/*` | `Authorization: Bearer ${SPEACHES_API_KEY}`; `/health` is open |
| `${INFERENCE_LAN_IP}:9400` | DCGM exporter `/metrics` | none; firewall to Prometheus |
| `${INFERENCE_LAN_IP}:9100` | node-exporter `/metrics` | none; firewall to Prometheus |

## Files

| Path | Purpose |
|---|---|
| `docker-compose.yml` | The stack. Start-up order: `speaches` → `speaches-warmup` (loads both models, then exits) → `vllm`. |
| `.env.example` | Every variable, with comments. Copy to `.env`. |
| `presets/gpu-{16,24,48}gb.env.preset` | GPU sizing from PLAN section 4. 24 GB is the default in `.env.example`. |
| `scripts/download-models.sh` | Downloads the models into the volumes during maintenance and records their revisions. |
| `scripts/lock-digests.sh` | Records image digests in `versions.lock`; `--check` verifies them. |
| `versions.lock` | Pinned images (digests) and models (Hugging Face revisions). |

## Why the stack starts in this order

- vLLM reserves `--gpu-memory-utilization` × **total** GPU memory. It refuses to start if that much is not free. While it profiles, it also asserts that no other process changes GPU memory (`vllm/v1/worker/gpu_worker.py`, v0.31.0).
- Speaches loads a model only on first use, and `/health` is green before any model is loaded. The `speaches-warmup` one-shot service therefore loads both models with `POST /api/ps/{model}`, then runs one TTS and one transcription. Only after it exits 0 does vLLM start.
- `STT_MODEL_TTL=-1` and `TTS_MODEL_TTL=-1` mean the models are never unloaded.
- If you restart Speaches on its own, run `docker compose up speaches-warmup` afterwards. Otherwise the first call pays the model-load time.

## Prerequisites

- Ubuntu LTS server with one NVIDIA GPU (16, 24 or 48 GB).
- **NVIDIA driver, R580 branch.** It supports both CUDA 13.0 (`vllm/vllm-openai:v0.31.0`) and CUDA 12.x (`-cu129`, and Speaches' CUDA 12.6.3 image) containers. Check with `nvidia-smi`; the header shows `Driver Version: 580.x`.
- **NVIDIA Container Toolkit 1.20.1**, configured for Docker:

  ```bash
  sudo nvidia-ctk runtime configure --runtime=docker
  sudo systemctl restart docker
  nvidia-ctk --version          # 1.20.1
  ```

- **Docker Engine with the Compose v2 plugin, version 2.17 or later.** That version is needed to pass `--env-file` more than once.
- About 30 GB free on the Docker data disk: the images plus about 6 GB of LLM and 2 GB of STT/TTS models.
- **Firewall.** Docker port publishing bypasses ufw, so the rules go in the `DOCKER-USER` chain (PLAN F5):
  - 8000 and 8001 only from the app host;
  - 9100 and 9400 only from Prometheus (the app host);
  - default-deny egress.

  During model downloads, temporarily allow HTTPS to Hugging Face. Write down the hosts the download actually contacts and add them to the maintenance allow-list.

## First install

```bash
cd infra/inference
cp .env.example .env
chmod 600 .env
# Edit .env: INFERENCE_LAN_IP, VLLM_API_KEY and SPEACHES_API_KEY (each: openssl rand -hex 32).
# On a CUDA 12-only host, set VLLM_IMAGE=vllm/vllm-openai:v0.31.0-cu129.

docker compose --env-file .env config --quiet     # fails if a required value is missing

# Check that the GPU is visible to containers (uses an image we pull anyway).
docker compose --env-file .env pull
docker run --rm --gpus all --entrypoint nvidia-smi vllm/vllm-openai:v0.31.0
```

## Model download (maintenance window, online)

```bash
# 1. Open egress to Hugging Face.
scripts/download-models.sh            # pulls images, downloads LLM, STT and TTS, writes model revisions to versions.lock
scripts/lock-digests.sh               # writes image digests to versions.lock
# 2. Close egress again.
git diff versions.lock                # review, commit, and note any change in docs/DECISIONS.md
```

The script uses each server's own mechanism:

- **LLM.** `huggingface_hub.snapshot_download` runs inside the vLLM image, into the `buzz-hf-cache-vllm` volume.
- **STT and TTS.** A temporary Speaches container (with `HF_HUB_OFFLINE=0` and no published ports) downloads through Speaches' API, `POST /v1/models/{id}`, into `buzz-hf-cache-speaches`. This puts the files exactly where Speaches looks for them.

`PRELOAD_MODELS` is deliberately **not** used. At startup it calls the Hugging Face API, and Speaches exits if that fails, which it would with egress blocked.

## Start, stop, upgrade

```bash
docker compose --env-file .env up -d                                               # 24 GB card
docker compose --env-file .env --env-file presets/gpu-16gb.env.preset up -d        # 16 GB card
docker compose --env-file .env --env-file presets/gpu-48gb.env.preset up -d        # 48 GB card

docker compose ps -a                  # speaches and vllm "healthy"; speaches-warmup "exited (0)"
docker compose logs speaches-warmup   # shows both models loaded and a test transcript
docker compose logs -f vllm           # first start takes several minutes (kernel compile, CUDA graphs)
docker compose down                   # stop; the volumes and models are kept
```

For an upgrade, change the tag in `.env` and the `versions.lock` reference. Then run `download-models.sh` (or just `docker compose pull`), `lock-digests.sh`, the smoke tests and the benchmark. Record the change in `DECISIONS.md`.

## Smoke tests

Run these from the app host so that the firewall path is tested too.

```bash
set -a; . ./.env; set +a          # or export the three values by hand on the app host
H=${INFERENCE_LAN_IP}
```

**Health (no key)**

```bash
curl -fsS http://$H:8000/health && echo vllm ok
curl -fsS http://$H:8001/health && echo
```

**Keys are enforced** (expect `401` twice)

```bash
curl -s -o /dev/null -w '%{http_code}\n' http://$H:8000/v1/models
curl -s -o /dev/null -w '%{http_code}\n' http://$H:8001/v1/models
```

**Chat completion**

```bash
curl -sS http://$H:8000/v1/chat/completions \
  -H "Authorization: Bearer $VLLM_API_KEY" -H 'Content-Type: application/json' \
  -d '{"model":"buzz-llm","temperature":0.3,"max_tokens":60,
       "messages":[{"role":"system","content":"You are the BongoHive phone receptionist. Answer in one sentence."},
                   {"role":"user","content":"What time do you open on Saturday?"}]}'
```

**Tool call (hermes parser)**

The response should hold `tool_calls` with `opt_out`.

```bash
curl -sS http://$H:8000/v1/chat/completions \
  -H "Authorization: Bearer $VLLM_API_KEY" -H 'Content-Type: application/json' \
  -d '{"model":"buzz-llm","tool_choice":"auto","max_tokens":100,
       "tools":[{"type":"function","function":{"name":"opt_out","description":"Stop all future calls to this caller",
                 "parameters":{"type":"object","properties":{}}}}],
       "messages":[{"role":"user","content":"Please never call me again."}]}'
```

**Speech (TTS)**

Writes a 24 kHz WAV. Listen to it.

```bash
curl -sS http://$H:8001/v1/audio/speech \
  -H "Authorization: Bearer $SPEACHES_API_KEY" -H 'Content-Type: application/json' \
  -d "{\"model\":\"$TTS_MODEL\",\"voice\":\"$TTS_VOICE\",\"response_format\":\"wav\",
       \"input\":\"Good morning, thank you for calling BongoHive.\"}" \
  -o hello.wav
```

**Transcription (STT)**

This uses the file from the speech test, converted to 8 kHz like a phone call. It should return `{"text":"Good morning, thank you for calling BongoHive."}` or very close.

```bash
ffmpeg -loglevel error -y -i hello.wav -ar 8000 -ac 1 -c:a pcm_s16le hello-8k.wav
curl -sS http://$H:8001/v1/audio/transcriptions \
  -H "Authorization: Bearer $SPEACHES_API_KEY" \
  -F "file=@hello-8k.wav;type=audio/wav" -F "model=$STT_MODEL" -F language=en
```

**Models resident and settings as intended** (`/api/ps` should list both models)

```bash
curl -sS -H "Authorization: Bearer $SPEACHES_API_KEY" http://$H:8001/api/ps
docker compose exec vllm env | grep -E '^(HF_HUB_OFFLINE|VLLM_NO_USAGE_STATS)='
```

**Bound to the LAN IP only** (run on the inference host)

Nothing should show `0.0.0.0` or `[::]` for these ports.

```bash
sudo ss -tlnp | grep -E ':(8000|8001|9100|9400)\b'
```

**Metrics**

```bash
curl -s http://$H:8000/metrics | grep -m3 '^vllm:'
curl -s http://$H:9400/metrics | grep -m3 DCGM_FI_DEV_FB_USED
curl -s http://$H:9100/metrics | grep -m1 node_load1
```

Then run the benchmark: `tests/bench/README.md`.

## Phase 1 acceptance criteria

Phase 1 is accepted when all of the following hold on the production inference host:

1. **Clean cold start, offline.**
   - With egress blocked, `reboot`, then `docker compose ps` shows `vllm` and `speaches` healthy and `speaches-warmup` exited 0 without manual steps.
   - No container log shows an attempt to reach `huggingface.co` or a usage-stats endpoint.
   - The egress counters in `DOCKER-USER` show no dropped outbound packets from these containers over 24 h of idle and test traffic.
2. **Pinned.**
   - `versions.lock` has no `PENDING` entries and `scripts/lock-digests.sh --check` exits 0.
   - The versions match `LICENCES.md`.
3. **Exposure.**
   - Every published port is bound to `INFERENCE_LAN_IP` only (`ss -tlnp`).
   - 8000 and 8001 are reachable from the app host and refused from any other LAN host.
   - The `/v1` routes return 401 without a key.
4. **Function.** All smoke tests above pass from the app host:
   - a chat answer;
   - an `opt_out` tool call;
   - intelligible `bf_emma` speech;
   - a correct transcript of the 8 kHz round trip.
5. **Memory measured.** `nvidia-smi` and the DCGM framebuffer figures after warm-up and under the c=10 benchmark are recorded in `docs/` and replace the estimates in PLAN section 4. The chosen preset leaves at least 1.5 GB free at peak.
6. **Latency** (benchmark from the app host, real Global prompt via `--system-prompt-file`, 10 concurrent).
   - Proposed targets, to be confirmed with the team (they add up to the PLAN R1 turn budget of about 1.2 s with VAD stop time):

     | Metric | p95 target |
     |---|---|
     | LLM TTFT | ≤ 400 ms |
     | STT latency on a 3–5 s utterance | ≤ 400 ms |
     | TTS TTFB | ≤ 250 ms |

   - Zero errors at 1, 5 and 10.
   - The JSON results are committed under `docs/` with the GPU model and preset in `--label`.
7. **Accuracy.** WER on consented 8 kHz BongoHive recordings is measured and recorded (target from the brief: ≤ 15%). This is a separate test in `tests/`; this host only has to serve it.
8. **Stability.**
   - After 30 minutes idle, `/api/ps` still lists both models.
   - After `docker compose restart speaches` followed by `docker compose up speaches-warmup`, everything is healthy again without restarting vLLM.

## Troubleshooting

- **vLLM: "Free memory on device … is less than desired GPU memory utilization".** Something else holds GPU memory, or the preset is too large for the card. Check `nvidia-smi`, then use a smaller preset or lower `VLLM_GPU_MEMORY_UTILIZATION`.
- **vLLM: "Error in memory profiling … other processes … release GPU memory".** Speaches changed its memory use while vLLM was starting. Make sure `speaches-warmup` succeeded before vLLM started (`docker compose up -d` handles this).
- **Speaches 404 "Model … is not installed locally".** The model is not in the `buzz-hf-cache-speaches` volume. Run `scripts/download-models.sh` in a maintenance window.
- **Whisper runs out of memory on a 16 GB card.** Use the 16 GB preset (`WHISPER_COMPUTE_TYPE=int8_float16`).
