# Buzz voice-agent platform: Phase 0 plan

Status: **draft for approval**; base code built 19 October 2026 (see section 12). Date: 15 October 2026. Owner: BongoHive AI Lab, Data & AI team.

This plan comes from reading the Dograh source at tag `dograh-v1.49.0` (commit `b86e8ed`, 12 Oct 2026), its docs, and the upstream release pages for vLLM, Speaches, Asterisk, NVIDIA and the monitoring stack. Every key below is quoted from a doc page or source file. Items marked **(verify)** have not been confirmed and will be tested before we rely on them.

The servers have **not** been inspected yet because their details are still placeholders in the brief (see section 9). Section 4 therefore gives GPU budgets for the likely card sizes.

---

## 1. Headline findings that change the brief

Dograh is a good fit, but in its default form it is **not** fully local. Five findings need a decision:

| # | Finding (source) | Impact on constraint 1 (fully local) | Proposal |
|---|---|---|---|
| F1 | Knowledge-base ingestion always sends documents to Dograh cloud: `POST {MPS_API_URL}/api/v1/document/process` in `api/tasks/knowledge_base_processing.py`. This applies to both "Full Document" and "Chunked Search". Embeddings are limited to openai, openrouter, azure and dograh, with a fixed `Vector(1536)` column. | **Breaks it.** The documents themselves are public, but the process fails without internet and creates a cloud dependency. | **Do not use the Dograh knowledge base in v1.** The front-desk knowledge (programmes, plans, events, hours) is small, so put it straight into the Global node prompt as cleaned Markdown, kept under about 3k tokens. vLLM prefix caching keeps that cheap. If it grows, add a small **local KB service** (pgvector plus a local embedding model) called as a Dograh HTTP tool. See D2. |
| F2 | Compose hard-codes `POSTHOG_API_KEY` and `POSTHOG_HOST=https://us.i.posthog.com` on `api`. `posthog_client.py` **ignores `ENABLE_TELEMETRY`**, and `ENABLE_TELEMETRY` defaults to `true` in code, despite the docs. | **Breaks it.** Analytics events leave the server. | Maintain our own compose file with `POSTHOG_API_KEY` unset and `ENABLE_TELEMETRY=false` on both `api` and `ui`. Also block egress (F5). |
| F3 | The prebuilt UI image bakes in a Chatwoot widget (`https://chat.dograh.com`) and a lead-form endpoint (`https://api-leads.dograh.com`). | Browser calls go to third parties. | **Build `dograh-ui` from source** at the pinned tag with those build variables blank. The same build replaces the logo with the **Buzz colour logo** (`branding/svg/buzz-logo-horizontal-color.svg`) (BSD 2-Clause allows this; we keep the copyright notice). See D3. |
| F4 | On first sign-in, org bootstrap calls `MPS_API_URL` (`https://services.dograh.com`) to mint a Dograh service key and provision Cloudonix SIP. No flag disables it; it is non-fatal and retried. Recording "transcribe" and "create agent from template" also use MPS. | Contacts Dograh cloud. | Set `MPS_API_URL` to an unroutable internal address **(verify the app still starts cleanly)**. Archive the "Dograh" connection, set our BYOK config as the org default, and don't use template creation or upload transcription (we type transcripts by hand). |
| F5 | Compose publishes Postgres 5432, Redis 6379 and API 8000 on all interfaces, sets `FORWARDED_ALLOW_IPS: "*"`, and falls back to default passwords. Docker port publishing **bypasses ufw**. | Breaks constraint 5. | Our compose binds data services to the internal Docker network only and the API to `127.0.0.1`; nginx is the only public entry point. Add **default-deny egress** on both servers via the `DOCKER-USER` chain, with allow-lists. That is also the strongest proof for the packet-capture test. |

Two more findings affect how features work:

- **Transfers are blind on Asterisk.** Dograh plays hold music, connects the caller and drops off. On Asterisk the destination must be a SIP endpoint, and the "transfer introduction" is Twilio-only. Dograh has no warm transfer. *(Corrected in section 12: `custom_message` never reaches Asterisk.)* **Proposal:** transfer to `Local/<staff>@buzz-transfer`. That dialplan dials the staff phone with an announcement played only to them ("BongoHive AI desk transferring a caller about …", using the `custom_message` from Dograh's Dynamic HTTP Resolver, **verify** that it reaches the channel), lets them accept or decline, and falls back to take-a-message.
- *(Corrected in section 12: in 1.49.0 QA already defaults to the workflow LLM.)* **QA and voicemail detection default to OpenAI.** Both must be set to **"Use Workflow's LLM"** so they stay local. `use_workflow_llm` already defaults to true for voicemail. QA defaults to the `openai` provider and must be switched on every agent; the agent export tests will check this.

---

## 2. Versions to pin

Every image is pinned by tag. In Phase 1 and 2 we also record the digest in `infra/*/versions.lock`.

| Component | Pin | Released | Notes |
|---|---|---|---|
| Dograh api / ui | `1.49.0` (git tag `dograh-v1.49.0`) | 12 Oct 2026 | `api`: `dograhai/dograh-api:1.49.0`. `ui`: **built locally** from the tag (F3) as `buzz/dograh-ui:1.49.0-buzz1`. If staging finds a regression we fall back to `1.48.0` (2 Oct). |
| Postgres | `pgvector/pgvector:pg17` → pin to a minor, e.g. `0.8.x-pg17` (verify tag) | | Dograh expects pgvector |
| Redis | `redis:7` → pin to `7.4.x` (verify tag) | | with `requirepass` |
| MinIO | `dograhai/minio:2026.09.22` | | Dograh's community fork, as upstream compose |
| coturn | `coturn/coturn:4.8.0` | | only for the web widget and Test Audio over NAT |
| nginx | `nginx:1.30.5-alpine` (1.28 is no longer maintained) | | TLS termination |
| vLLM | `vllm/vllm-openai:v0.31.0` (CUDA 13.0) or `v0.31.0-cu129` | 4–5 Oct 2026 | choose by driver (section 3) |
| LLM | `Qwen/Qwen2.5-7B-Instruct-AWQ` (Apache-2.0, 5.6 GB) | | tool-call parser `hermes`, as documented by vLLM for Qwen2.5 |
| Speaches | `ghcr.io/speaches-ai/speaches:0.9.0-rc.3-cuda-12.6.3` | 27 Dec 2025 | **Risk:** no stable 0.9 and no commits since April 2026. Fallback is `0.8.3`. See R3. |
| STT model | `deepdml/faster-whisper-large-v3-turbo-ct2` (MIT) | | used in Speaches' own docs |
| TTS model | `speaches-ai/Kokoro-82M-v1.0-ONNX` (upstream Kokoro Apache-2.0; the ONNX repo has no licence tag, **verify**). British voices `bf_emma`, `bf_isabella`, `bm_george`, `bm_lewis` | | We choose the voice with staff in Phase 3. |
| TTS fallback | Piper `en_GB-cori-high` (public domain) or `en_GB-alba-medium` (CC-BY-4.0) | | **Excluded:** `semaine` (CC-BY-NC-SA) and `alan` (all rights reserved). Piper runtime is GPL-3.0, which is fine for internal use without distribution. |
| Asterisk | **22.11.0**, built from source in our Dockerfile | 27 Aug 2026 | `chan_websocket` (≥22.6) and `res_websocket_client` (≥22.5) included. There is no official image. |
| NVIDIA driver | R580 branch | | supports CUDA 12.x and 13.x containers (LTSB status **verify**) |
| NVIDIA Container Toolkit | 1.20.1 | 19 Sep 2026 | |
| DCGM exporter | `nvcr.io/nvidia/k8s/dcgm-exporter:4.8.4` | 18 Sep 2026 | |
| Prometheus | `prom/prometheus:v3.15.0` | 25 Sep 2026 | |
| Grafana | `grafana/grafana:13.2.4` | 13 Oct 2026 | |
| n8n | `n8nio/n8n:2.43.6` | stable | if we self-host it (Q6) |
| Langfuse (tracing) | self-hosted v3, tag TBD in Phase 2 | | Dograh's tracing exports OTLP to `{LANGFUSE_HOST}/api/public/otel/v1/traces` |

Because some pins are less than two weeks old (Dograh 1.49.0, vLLM 0.31.0), each one is accepted only after its phase's acceptance tests pass. Any change goes in `DECISIONS.md`.

---

## 3. Inference server (Phase 1 preview)

`infra/inference/docker-compose.yml` runs two services on an internal network. Each is published **only on the LAN interface**, behind an API key.

**vLLM.** Every flag below is verified at v0.31.0.

```
--model Qwen/Qwen2.5-7B-Instruct-AWQ
--served-model-name buzz-llm
--quantization awq_marlin
--max-model-len 8192
--max-num-seqs 16
--gpu-memory-utilization <from section 4>
--enable-auto-tool-choice --tool-call-parser hermes
--enable-prefix-caching          # verify: may already be default
--api-key ${VLLM_API_KEY}
```

- `/health` and `/metrics` are **not** protected by `--api-key`. They stay LAN-only and are scraped by Prometheus.

**Speaches**
- `API_KEY`
- *(Dropped, see section 12: cannot work offline.)* `PRELOAD_MODELS=["deepdml/faster-whisper-large-v3-turbo-ct2","speaches-ai/Kokoro-82M-v1.0-ONNX"]` (JSON list format **verify**)
- `STT_MODEL_TTL=-1`, `TTS_MODEL_TTL=-1` so models never unload
- `WHISPER__COMPUTE_TYPE=float16` (`int8_float16` if VRAM is tight)
- `ENABLE_UI=false`
- Hugging Face cache on a named volume

**Health checks and model downloads.** Compose health checks use `GET /health` on both services. Models are pre-downloaded into volumes during maintenance; at runtime, outbound access to Hugging Face is blocked.

**Benchmark (`tests/bench/`)**

- Async Python against both endpoints at 1, 5 and 10 concurrent requests.
- Reports p50 and p95 for:
  - LLM time to first token, streaming;
  - STT latency on 8 kHz μ-law WAVs resampled the way Dograh sends them (**verify** the rate Dograh posts at);
  - TTS time to first audio byte.

---

## 4. GPU memory budget

These are planning estimates. Phase 1 replaces them with measured `nvidia-smi` and DCGM figures.

Inputs:

- **Qwen2.5-7B AWQ weights:** about 5.6 GB.
- **KV cache:** 28 layers × 4 KV heads × 128 dims × 2 (K and V) × 2 bytes = about **56 KB per token**.
- **Context per call:** about 6k tokens (system prompt plus inline KB about 3.5k, conversation about 2.5k). Ten concurrent calls therefore need about 60k tokens, or about **3.4 GB of KV**.
- **Speaches:**
  - Whisper large-v3-turbo fp16: about 2.5–3 GB (estimated from faster-whisper's large-v2 benchmark, **verify**).
  - Kokoro ONNX: under 1 GB.
  - CUDA context: about 0.5 GB per process.

| Card | vLLM `--gpu-memory-utilization` | vLLM share: weights + activations + KV | Speaches | Headroom | Fit |
|---|---|---|---|---|---|
| 16 GB (e.g. T4, A4000) | 0.55 → 8.8 GB | 5.6 + ~1.5 + **~1.7 GB KV (~30k tokens)** | ~4 GB (Whisper int8_float16) | ~3 GB | **Tight.** About 5 concurrent calls; `--max-model-len 6144`. |
| **24 GB** (e.g. L4, A10, RTX 4090) | 0.50 → 12 GB | 5.6 + ~2 + **~4.4 GB KV (~78k tokens)** | ~5 GB | ~7 GB | **Good.** 10+ calls. |
| 48 GB (e.g. L40S, A6000) | 0.40 → 19 GB | 5.6 + ~2 + ~11 GB KV | ~5 GB | ~24 GB | Room for a larger LLM or a Bemba/Nyanja STT model (Phase 7). |

Start-up order: Speaches starts first, then vLLM (`depends_on: service_healthy`). vLLM sizes itself as a fraction of **total** memory and refuses to start if it can't reserve that much.

---

## 5. Exact Dograh settings for local providers

These are set in **Settings → Model configurations** (API: `/api/v1/model-connections/provider-connections`, `/model-configurations` and `/default`). `tools/dograh/configure_providers.py` does all of this section automatically.

1. **Provider connections** (BYOK). Use the internal LAN address or hostname. **Never `localhost`**: inside the Dograh container, port 8000 is Dograh itself.

   | Purpose | Provider ID | `base_url` | `api_key` | `model` | Other |
   |---|---|---|---|---|---|
   | LLM | `openai` (custom `base_url` is documented for local LLMs; fallback is the `speaches` LLM provider, labelled "Local Models (Speaches)") | `http://<inference-host>:8000/v1` | `${VLLM_API_KEY}` | `buzz-llm` | `temperature` 0.3 |
   | STT | `speaches` | `http://<inference-host>:8001/v1` | `${SPEACHES_API_KEY}` | `deepdml/faster-whisper-large-v3-turbo-ct2` | `language` `en` |
   | TTS | `speaches` | `http://<inference-host>:8001/v1` | `${SPEACHES_API_KEY}` | `speaches-ai/Kokoro-82M-v1.0-ONNX` (**verify** the ID Dograh passes through) | `voice` `bf_emma` (TBC), `speed` 1.0 |
   | Embeddings | none in v1 (D2) | | | | |

2. **Named configuration** `buzz-local`. Mark it as the **Organisation Default** with `PUT /default`.
3. **Archive the "Dograh" connection** that org bootstrap creates.
4. **On every agent:**
   - inherit `buzz-local`;
   - QA node "Use Workflow's LLM" = **on**;
   - voicemail `use_workflow_llm` = **true**.

   A CI check in `tests/` fails any exported workflow JSON that references the `openai` provider without our `base_url`, or the `dograh` provider.

**Dograh environment (app `.env.example`; every key comes from `api/constants.py` or the compose file)**

- Mode and telemetry:
  - `DEPLOYMENT_MODE=oss`
  - `ENVIRONMENT=production`
  - `ENABLE_TELEMETRY=false` on both `api` and `ui`
  - `POSTHOG_API_KEY` **unset**
  - `SENTRY_DSN` unset
- Authentication:
  - `AUTH_PROVIDER=local`
  - `OSS_JWT_SECRET=<64 random bytes>`
  - `ENABLE_SIGNUP=false` once the admin accounts exist
- URLs and proxy:
  - `PUBLIC_BASE_URL=https://voice.bongohive.co.zm` (TBC)
  - `PUBLIC_HOST=voice.bongohive.co.zm` (TBC)
  - `FORWARDED_ALLOW_IPS=<nginx container IP or network>`, not `*`
  - `MPS_API_URL=http://127.0.0.1:9` (blackhole, F4; **verify**)
- Telephony: `TELEPHONY_WS_TOKEN_SECRET=<random>`, `TELEPHONY_WS_TOKEN_ENFORCE=true`
- Data:
  - `POSTGRES_PASSWORD`, `REDIS_PASSWORD`, `MINIO_ROOT_USER`, `MINIO_ROOT_PASSWORD`: all random
  - `ENABLE_CALL_RECORDING_UPLOAD=true` (to our MinIO)
- Operations:
  - `ENABLE_PROMETHEUS_METRICS=true` (scraped at `/api/v1/metrics`, internal only)
  - `DEFAULT_ORG_CONCURRENCY_LIMIT=10`, matched to the GPU capacity from the Phase 6 load test
- Tracing: `LANGFUSE_HOST`, `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY` pointing at our self-hosted Langfuse.
- **Never** use the `tunnel` profile (Cloudflare quick tunnel) or `start_docker.sh`, which enables it.

---

## 6. Telephony design (Phase 4 preview)

- **Asterisk 22.11.0** built from source, with `res_ari*`, `res_http_websocket`, `res_websocket_client`, `chan_websocket` and `res_pjsip*`. Codec `ulaw` only.
- **`ari.conf`:** user `[dograh]` with a strong password. **`http.conf`:** bound to the private interface only; port 8088 is firewalled to the Dograh host.
- **`websocket_client.conf`:**
  - `[dograh] type=websocket_client`
  - `uri=wss://<dograh-internal>/api/v1/telephony/ws/ari`
  - `protocols=media`
  - `connection_type=per_call_config` (required for chan_websocket)
  - `tls_enabled=yes`
  - `ca_list_file=...`
- **Dograh ARI configuration:** ARI endpoint, username and password, `ws_client_name=dograh`, and the generated `stasis_app_name` (`dograh_…`) copied into the dialplan.
- **Dial String Template: `Local/{number}@buzz-outbound`**, deliberately not `PJSIP/{number}@<trunk>` as the brief says. **Deviation D4.** The `buzz-outbound` context runs for every outbound call, whether from the API, a campaign or a test:
  1. normalises to E.164 for Zambia (+260);
  2. **rejects** premium, short-code and international patterns;
  3. checks the **suppression list** over `CURL()` against the local suppression service, and hangs up with no dialling if the number is listed;
  4. enforces **permitted calling hours** with `GotoIfTime`;
  5. enforces the trunk channel limit with `GROUP_COUNT`;
  6. only then dials `PJSIP/${num}@zm-trunk`.

  This puts the compliance guarantees in one place that no workflow or API path can bypass.
- **Inbound:** DIDs and staff extensions are routed with `Stasis(dograh_xxx)`. Dograh maps each called number to one inbound workflow.
- **Transfers:** warm-style handover through `Local/<ext>@buzz-transfer` (section 1).
- **Firewall:**
  - SIP 5060/5061 and RTP 10000–20000/udp accepted only from the carrier's IPs.
  - 8088 only from the Dograh host.
  - No outbound international routes on the trunk, if the carrier supports that restriction.

---

## 7. Compliance design

| Requirement | Mechanism |
|---|---|
| Disclosure at the start of every call, not interruptible | *(Settled in section 12: played by Asterisk before the call reaches Dograh.)* A pre-recorded disclosure on the Start Call node. Dograh's `greeting_override` supports `type:"audio"` with a `recording_id`. Whether it is **non-interruptible** must be checked in source (**verify** in Phase 2). If not, play it in Asterisk with `Playback()` before `Stasis()` for inbound calls, and after answer in `buzz-outbound` for outbound calls. Either way, it does not depend on the LLM. |
| Opted-in contacts only | The n8n trigger flow checks the HubSpot consent property. Campaign CSVs are generated only by our export script, which filters on consent. |
| Instant voice opt-out | A Global-node tool `opt_out` (HTTP tool) posts to the suppression service and then ends the call. `buzz-outbound` checks the list before every dial (section 6). |
| No PINs, cards or national IDs | Prompt rules in the Global node, no extraction variables of those types, and a CI linter on the workflow JSON. A regex redaction pass on transcripts in n8n before anything goes to HubSpot. |
| Retention | Nightly job: delete MinIO recordings older than 90 days (MinIO lifecycle rule) and purge or anonymise transcripts per the DPIA. Documented in `docs/`. |

---

## 8. Risks

| # | Risk | Likelihood / impact | Mitigation |
|---|---|---|---|
| R1 | Speaches STT in Dograh is **segmented HTTP**, not streaming (pipecat `SpeachesSTTService` → `/v1/audio/transcriptions`). This adds turn latency. | Medium / High (1.2 s target) | Large-v3-turbo with fp16 on GPU, short VAD stop time, measured in Phase 1 and 2. Fallback: Dograh's other local-capable path, to be decided with data. |
| R2 | A 7B model makes mistakes in tool calls and facts. | Medium / Medium | Short node prompts, few tools per node, the 50-scenario suite as a gate. The 48 GB card would allow a larger model. |
| R3 | Speaches is slow-moving: on an RC with no commits since April. | Medium / Medium | Pin the digest. The OpenAI-compatible interface makes it replaceable. Fallback: `0.8.3`. |
| R4 | Dograh makes hidden cloud calls (F2–F4). Upgrades may add more. | Medium / High | Default-deny egress, packet capture on every upgrade in staging, a UI rebuild per release. |
| R5 | Dograh releases often (about every 2 weeks); we carry a UI fork. | High / Low | Keep the UI patch to build variables plus logo assets, scripted. Upgrade runbook with staging. |
| R6 | Zambian accents and 8 kHz audio raise WER above 15%. | Medium / High | Measure on real BongoHive recordings in Phase 1. Whisper `initial_prompt` with local names, if Dograh passes it (**verify**). |
| R7 | Warm transfer needs our own dialplan; the Dograh `custom_message` may not reach Asterisk. | Medium / Low | Fall back to a fixed whisper announcement. |
| R8 | Non-interruptible disclosure is not supported natively. | Unknown / High (compliance) | Asterisk-level playback (section 7). |

---

## 9. Open questions (blocking items marked ●)

1. **Server details (updated).** No local servers exist yet. Phases 0-6 are tested on **temporary cloud VMs** (inference: 24 GB GPU, Ubuntu 22.04/24.04; app: 4 vCPU / 8 GB, public IP), or one GPU VM for both early on. Synthetic data only. Migration to local servers later means changing `INFERENCE_HOST`, `APP_LAN_IP`, `SERVER_IP` and the `.env` files. Still needed from the user: cloud host/IP and SSH access once the VMs exist; real server details before go-live.
2. **Domain.** Is `voice.bongohive.co.zm` the internal domain? Is it internal-only, or does it need public DNS for the web widget and TLS (Let's Encrypt)?
3. **Approval of deviations D2 to D5** (section 10).
4. **SIP trunk provider.** Do they support restricting international calls and premium numbers on their side? Credentials can come later.
5. **Knowledge-base source documents.** Where do they live?
6. **n8n.** Do you already have one, and where is it hosted? HubSpot will receive call summaries, which is personal data, so this belongs in the DPIA.
7. **Git remote** for this repo.
8. **Langfuse.** Self-host it on the app server (heavier: ClickHouse) or on a separate small VM?

---

## 10. Deviations requiring approval

| ID | Brief says | Proposal | Why |
|---|---|---|---|
| D1 | (none) | Brand the platform "Buzz", with the colour logo in the UI (see DECISIONS #2) | Your request |
| D2 | Use the Dograh knowledge base | Inline prompt KB in v1; a local KB tool later | Dograh's KB sends documents to Dograh cloud (F1) |
| D3 | Self-host Dograh with Docker | Our own compose file plus a locally built UI image | Removes hard-coded PostHog, Chatwoot and leads endpoints; fixes exposed ports (F2, F3, F5) |
| D4 | Dial String `PJSIP/{number}@<trunk>` | `Local/{number}@buzz-outbound`, a guard context that then dials the trunk | Single enforcement point for suppression, hours, fraud and channel limits |
| D5 | Warm transfer | Asterisk-side announced transfer | Dograh offers only a blind transfer with hold music on Asterisk |

---

## 11. Phase 1 entry criteria

- Section 9 question 1 answered.
- This plan approved, including D2–D5.
- Read-only server inspection done: `lsb_release`, `nvidia-smi`, `lspci`, `df -h`, `ss -tulpn`, `docker version`. Output pasted into `docs/inspection/`.

---

## 12. Build findings (19 October 2026)

The base code for Phases 1 to 5 was written against the Dograh v1.49.0 source and the upstream sources of vLLM, Speaches, Asterisk 22.11.0, Prometheus and Grafana. These findings correct or settle earlier sections. Each is cited in the relevant folder's README.

| # | Finding | Effect |
|---|---|---|
| B1 | Speaches looks up its model list on Hugging Face at start-up and exits if that fails, so `PRELOAD_MODELS` cannot be used offline. | Models are downloaded in maintenance (`infra/inference/scripts/download-models.sh`) and loaded by a one-shot `speaches-warmup` service before vLLM starts. |
| B2 | vLLM sends usage statistics unless told not to. | `VLLM_NO_USAGE_STATS=1` and `DO_NOT_TRACK=1`. Add to the F-list as F6. |
| B3 | Dograh sends STT audio for Asterisk calls as 8 kHz 16-bit PCM WAV plus 0.5 s of silence; TTS is requested as 24 kHz streamed PCM. | The benchmark (`tests/bench`) uses exactly this. |
| B4 | Leaving the leads variable blank is not enough (F3): the UI falls back to `api-leads.dograh.com`. Also found: browser PostHog via an `/ingest` rewrite, Sentry tunnel, ghcr.io version check, GitHub star badge, Google STUN. | All neutralised in `infra/app` (build variables, nginx 404s, CSP, `FORCE_TURN_RELAY`). Full table in `infra/app/README.md`. |
| B5 | **Security:** Dograh gives the MinIO `voice-audio` bucket anonymous read/write/list on every start; TURN credentials go to anonymous widget users; `/api/v1/metrics` needs `DOGRAH_DEVOPS_SECRET`. | nginx limits `/voice-audio/` to staff networks and GET/HEAD/PUT; coturn refuses relays to private addresses; metrics blocked publicly. |
| B6 | **Inbound:** Dograh accepts a call as inbound only while the channel is still ringing, so `Playback()` before `Stasis()` on the same channel would break every inbound call. | Asterisk answers, plays the disclosure, then hands over through a fresh `Local/…@buzz-stasis/n` channel. Settles R8. |
| B7 | The Start-node greeting can always be interrupted, and Global nodes cannot carry tools. | The disclosure lives in Asterisk (B6). `opt_out` is attached to every Start and Agent node; the linter enforces this. |
| B8 | On Asterisk, Dograh's transfer sends only the destination; `custom_message` is spoken to the caller only. | Staff hear a fixed announcement plus an optional recorded topic prompt. Settles R7. Transfer destinations are `Local/<ext>[*topic]@buzz-transfer/n` with a 60 s timeout. |
| B9 | Dograh's ARI originate sends no timeout (Asterisk default 30 s) and campaigns retry only hangup causes 17–19. | Ring timeout 18 s, disclosure 8 s or less; our refusals use causes 21, 29, 34 and 41 so they are never retried. Dial template gains `/n`. |
| B10 | In 1.49.0 the QA node already defaults to the workflow LLM (`qa_use_workflow_llm`); voicemail detection still falls back to `openai` if switched off. | The linter checks both. |
| B11 | Dograh's webhook body is a template whose values all become strings; there is no inline transcript, only a 1-hour signed URL; delivery is at least once, unsigned. | Our template and JSON Schemas are in `integrations/webhook-schemas`; n8n must de-duplicate on `X-Dograh-Delivery-Id`. |
| B12 | Prompt audio (including the disclosure) shares the `recordings/` prefix with call recordings in MinIO. | The 90-day rule uses object tags, not the prefix, so the disclosure is never deleted. |
| B13 | MinIO has archived the `mc` client and withdrawn its binaries and images. | `infra/backup/mc/Dockerfile` builds the last tag from source. |
| B14 | Zambia's numbering plan (ITU/ZICTA, December 2024): 9-digit national numbers; mobile 95/75, 96/76, 97/77/57, 98; fixed 211–218; 8x and 90 special or premium. | Encoded in the suppression service; anything not assigned is refused. |

### New open questions

9. Which networks count as "staff networks" for listening to recordings in the browser?
10. Which alert channel (email, Slack, WhatsApp) should Alertmanager use?
11. Who holds the two offline backup decryption keys?
12. Do we anonymise or delete call records after 90 days (DPIA)?
13. Which test caller numbers can we reserve for staging?
