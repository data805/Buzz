# Interfaces between components

The single source of truth for hosts, ports, networks, shared environment variables and internal APIs. If you change one of these, change it here first and record why in `DECISIONS.md`.

## Hosts

| Host | Role | Runs |
|---|---|---|
| **app** | Orchestration and telephony | Dograh api/ui, Postgres, Redis, MinIO, coturn, nginx, Asterisk, suppression service, Prometheus, Grafana, node-exporter |
| **inference** | GPU | vLLM, Speaches, DCGM exporter, node-exporter |

Both are on the same private LAN. `INFERENCE_HOST` is the inference server's LAN IP or hostname; `APP_LAN_IP` is the app server's LAN IP.

## Ports

| Port | Host | Bound to | Service | Reached by |
|---|---|---|---|---|
| 443/tcp, 80/tcp | app | public | nginx (TLS; 80 only redirects) | staff browsers, web widget |
| 8000/tcp | app | `127.0.0.1` | Dograh api | nginx (Docker network), Asterisk (loopback) |
| 8088/tcp | app | `172.30.0.1` (the `buzz-app` bridge gateway) | Asterisk ARI/HTTP and `res_prometheus` `/metrics` (Basic Auth) | Dograh api (as `http://asterisk:8088`, an alias for 172.30.0.1, or the IP itself) and Prometheus. Loopback would not be reachable from inside a container. |
| 5060/udp, 10000–20000/udp | app | public, carrier IPs only | Asterisk PJSIP and RTP | SIP trunk |
| 3478/udp+tcp, 49152–49200/udp | app | public | coturn (TLS off: Dograh never hands out `turns:` URLs, so 5349 is not published; relays to private addresses are refused) | web widget / Test Audio |
| 8090/tcp | app | `127.0.0.1` | suppression service (container port 8080) | Asterisk (`CURL()`, loopback). Containers (Dograh `opt_out` tool, n8n, Prometheus) use **`http://suppression:8080`** on `buzz-app`. |
| 9090/tcp | app | `127.0.0.1` | Prometheus | Grafana, SSH tunnel |
| 9093/tcp | app | `127.0.0.1` | Alertmanager | Prometheus, SSH tunnel |
| 3000/tcp | app | `127.0.0.1` | Grafana | nginx at `/grafana/`, as `grafana:3000` on `buzz-app` |
| (none) | app | `buzz-app` only | blackbox exporter | Prometheus (probes Speaches `/health`, which has no metrics endpoint) |
| 8000/tcp | inference | `${INFERENCE_LAN_IP}` | vLLM (OpenAI API, `--api-key`) | Dograh api |
| 8001/tcp | inference | `${INFERENCE_LAN_IP}` | Speaches (OpenAI-compatible STT/TTS, `API_KEY`) | Dograh api |
| 9400/tcp | inference | `${INFERENCE_LAN_IP}` | DCGM exporter | Prometheus |
| 9100/tcp | app | `172.30.0.1` | node-exporter | Prometheus (on `buzz-app`) |
| 9100/tcp | inference | `${INFERENCE_LAN_IP}` | node-exporter | Prometheus |

Host firewalls allow each LAN port only from the hosts in the "Reached by" column.

## Docker networks (app host)

- `buzz-app`: bridge, subnet **`172.30.0.0/24`**. All app containers.
- nginx has the fixed address **`172.30.0.10`**, so `FORWARDED_ALLOW_IPS=172.30.0.10`.
- Postgres, Redis and MinIO publish **no** ports.
- Asterisk uses `network_mode: host` (RTP port range) and reaches Dograh at `127.0.0.1:8000` and the suppression service at `127.0.0.1:8090`.
- Dograh reaches Asterisk ARI at `http://172.30.0.1:8088`. Asterisk must start after the `buzz-app` network exists, so that the gateway address is bound.

## Shared environment variables

| Variable | Used by | Meaning |
|---|---|---|
| `INFERENCE_HOST` | app `.env`, provider bootstrap, Prometheus | inference server LAN address |
| `INFERENCE_LAN_IP` | inference `.env` | interface vLLM/Speaches bind to |
| `VLLM_API_KEY` | inference, app bootstrap | Bearer key for vLLM |
| `SPEACHES_API_KEY` | inference, app bootstrap | Bearer key for Speaches |
| `LLM_MODEL_NAME` | both | served model name, default `buzz-llm` |
| `STT_MODEL` | both | `deepdml/faster-whisper-large-v3-turbo-ct2` |
| `TTS_MODEL` | both | `speaches-ai/Kokoro-82M-v1.0-ONNX` |
| `TTS_VOICE` | app bootstrap, inference warm-up, benchmark | default `bf_emma` (to be chosen with staff in Phase 3) |
| `SUPPRESSION_API_KEY` | suppression service, Asterisk, Dograh `opt_out` tool, n8n | `X-API-Key` header value |
| `ARI_PASSWORD` | Asterisk, Dograh telephony config | ARI user `dograh` |
| `TELEPHONY_WS_TOKEN_SECRET` | Dograh api | signs the chan_websocket URL tokens |
| `N8N_WEBHOOK_KEY` | Dograh webhook node, n8n | authenticates Dograh → n8n post-call webhooks; separate from the suppression key |
| `SUPPRESSION_LOG_HASH_KEY` | suppression service | optional HMAC key for hashed numbers in the decision log; derived from the API key if unset |
| `PUBLIC_HOST` / `PUBLIC_BASE_URL` | Dograh, nginx | e.g. `voice.bongohive.co.zm` (TBC) |
| `DOGRAH_DEVOPS_SECRET` | Dograh api, Prometheus | header `X-Dograh-Devops-Secret` required by `/api/v1/metrics` |
| `DOGRAH_STASIS_APP` | Asterisk | `dograh_<12 hex>`, printed by `tools/dograh/configure_providers.py` |
| `DOGRAH_ADMIN_EMAIL` / `DOGRAH_ADMIN_PASSWORD` (or `DOGRAH_API_KEY`) | provider bootstrap | Dograh admin login |
| `TURN_SECRET`, `SERVER_IP` | Dograh api, coturn | TURN REST shared secret; the app host's public IP |
| `ASTERISK_METRICS_USER` / `ASTERISK_METRICS_PASSWORD` | Asterisk, Prometheus | Basic Auth on Asterisk `/metrics` |
| `MESSAGE_WEBHOOK_URL` / `MESSAGE_WEBHOOK_SECRET` | Asterisk, n8n | take-a-message webhook; header `X-Buzz-Webhook-Secret` |
| `ALERT_WEBHOOK_URL` | Alertmanager | where alerts go (channel TBD) |
| `GRAFANA_ADMIN_PASSWORD` / `GRAFANA_SECRET_KEY` | Grafana | |

Host-only variables (GPU sizing, image tags and so on) are documented in each folder's `.env.example`.

## Compose names

- Project `buzz-app`; volumes `buzz-app_postgres_data`, `buzz-app_minio_data`, `buzz-app_redis_data`, `buzz-app_suppression_data` (the backup scripts depend on these).
- Bridge interface `br-buzz-app` (the firewall script depends on it).
- Service names on `buzz-app`: `api`, `ui`, `postgres`, `redis`, `minio`, `nginx`, `coturn`, `suppression`, `prometheus`, `grafana`, `alertmanager`, `blackbox`.

## Suppression service API (`integrations/suppression-service`)

The compliance gate for every outbound dial. Number rules (normalisation to Zambian E.164, rejection of premium, short-code and international numbers) live **here**, not in the dialplan, so that they are unit-tested in Python. The dialplan enforces only calling hours and the channel limit.

All endpoints except `/health` and `/metrics` require `X-API-Key: ${SUPPRESSION_API_KEY}`.

| Method and path | Caller | Request | Response |
|---|---|---|---|
| `GET /v1/dialplan/check?number=<raw>` | Asterisk `CURL()` | raw dialled number | `text/plain`, one line: `ALLOW <e164>` or `DENY <reason>`. Reasons: `invalid`, `international`, `premium`, `short_code`, `suppressed`. No trailing newline. The dialplan accepts only `ALLOW +260…` or `DENY [a-z_]+`; anything else, including 4xx/5xx and timeouts, hangs up with cause 41. |
| `GET /v1/check?number=<raw>` | n8n, scripts | as above | JSON `{"allow": bool, "e164": str\|null, "reason": str}` |
| `POST /v1/opt-out` | Dograh `opt_out` HTTP tool, staff | JSON `{"phone_number": str, "source": str, "call_id": str\|int?}` | `201` JSON `{"e164": str, "suppressed": true}`; idempotent |
| `GET /v1/suppressions` | staff, export script | `?since=` optional | JSON list |
| `POST /v1/suppressions/import` | staff | CSV with a `phone_number` column | JSON counts |
| `POST /v1/redact` | n8n | JSON `{"value": str\|list\|object, "phones": bool}` (max 2 MB) | JSON `{"value", "counts", "total"}` with PINs, cards, NRC, passport-like IDs (and optionally phone numbers) masked |
| `GET /health` | Compose, Prometheus | | `200 ok` |
| `GET /metrics` | Prometheus | | Prometheus text. Main series: `suppression_checks_total{endpoint,decision,reason}`, `suppression_check_errors_total`, `suppression_opt_outs_total`, `suppression_listed_numbers`. |

Errors: a database fault returns `503` with body `ERROR` (never `ALLOW`), so the dialplan fails closed. `POST /v1/opt-out` with an unparseable number or short code returns `422`.

Every agent must define the extraction variables `call_summary` and `opt_out_requested`; the post-call webhook template in `integrations/webhook-schemas` depends on them.

Opt-outs are never deleted through the API. Removal is a manual, audited database operation.

## Dial path

Dograh's Dial String Template is **`Local/{number}@buzz-outbound/n`** (`/n` stops Asterisk optimising away the Local channel Dograh tracks).

`Dograh → Local/{number}@buzz-outbound/n → CURL(/v1/dialplan/check) → GotoIfTime (Africa/Lusaka) → GROUP_COUNT → PJSIP/${E164}@zm-trunk → disclosure on answer`

Dograh originates with no timeout, so Asterisk's 30 s default applies: ringing (`OUTBOUND_RING_TIMEOUT`, 18 s) plus the disclosure (8 s or less) must fit inside it. Refusals use hangup causes 21, 29, 34 and 41, which Dograh campaigns never retry.

## Inbound path

`Carrier → from-trunk → Answer → Playback(disclosure) → Dial(Local/<agent number>@buzz-stasis/n) → Stasis(${DOGRAH_STASIS_APP})`

Dograh only treats a channel as inbound while it is still ringing, so the disclosure plays on the caller's channel and a fresh Local channel enters Stasis.

## Transfer path

Transfer tools must use the full destination **`Local/<ext>[*topic]@buzz-transfer/n`** with a **60 s** timeout. A bare extension would go through the dial template into `buzz-outbound` and be refused.

`Dograh transfer → Local/<ext>@buzz-transfer/n → fixed announcement (+ optional topic prompt) to staff only → press 1 bridges | decline/no answer → take a message → MESSAGE_WEBHOOK_URL`

Dograh sends Asterisk nothing but the destination: its `custom_message` is spoken to the caller only.
