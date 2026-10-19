# App host: Dograh stack runbook

Our own compose file for Dograh v1.49.0 and its services (D3). It replaces Dograh's `docker-compose.yaml`, `dograh-init`, `setup_remote.sh` and `start_docker.sh`, none of which we use. Hosts, ports and the `buzz-app` network are defined in [docs/INTERFACES.md](../../docs/INTERFACES.md).

| Path | Contents |
|---|---|
| `docker-compose.yml` | postgres, redis, minio, api, ui, coturn, nginx, suppression |
| `.env.example` | every variable, with comments; `scripts/gen-secrets.sh` turns it into `.env` |
| `ui-build/` | `build-ui.sh` and `patch-ui.sh`: builds `buzz/dograh-ui:1.49.0-buzz1` from source |
| `nginx/` | TLS server template, security headers, staff network list |
| `coturn/` | TURN config template and the small init that renders it |
| `versions.lock` | pinned tags; image digests are recorded here at deploy |
| `../firewall/` | default-deny egress and inbound allow-lists (F5) |

## Images

| Service | Image | Notes |
|---|---|---|
| postgres | `pgvector/pgvector:0.8.7-pg17-bookworm` | pgvector 0.8.7, Postgres 17 |
| redis | `redis:7.4.11-alpine` | `requirepass`, AOF on. Licence RSALv2/SSPLv1 from 7.4 (LICENCES.md) |
| minio | `dograhai/minio:2026.09.22` | Dograh's fork, as upstream |
| api | `dograhai/dograh-api:1.49.0` | |
| ui | `buzz/dograh-ui:1.49.0-buzz1` | built locally, never pulled |
| coturn | `coturn/coturn:4.8.0` | as PLAN; 4.18.0 is current upstream |
| nginx | `nginx:1.30.5-alpine` | 1.28 is no longer maintained (last image March 2026); 1.30 is the stable branch |
| suppression | `buzz/suppression-service:local` | built from `integrations/suppression-service` |

## What we changed from Dograh's defaults, and why

Outbound calls found in Dograh v1.49.0 (commit `b86e8ed`) and how each is stopped. The egress firewall (`../firewall/`) is the backstop for all of them.

| What | Where in source | How it is neutralised |
|---|---|---|
| PostHog analytics (api) | compose sets `POSTHOG_API_KEY` and `POSTHOG_HOST=https://us.i.posthog.com`; `api/services/posthog_client.py` creates a client whenever the key is set and ignores `ENABLE_TELEMETRY` | `POSTHOG_API_KEY=""`, `POSTHOG_HOST` blackholed, `ENABLE_TELEMETRY=false` |
| PostHog analytics (browser) | `ui/src/instrumentation-client.ts` (build key or `/api/config/posthog`); `/ingest/*` rewrite to `us.i.posthog.com` in `ui/next.config.ts` | ui `ENABLE_TELEMETRY=false`, `POSTHOG_KEY=""`, build `NEXT_PUBLIC_POSTHOG_KEY=""`; nginx returns 404 on `/ingest`; CSP `connect-src 'self'` |
| Sentry (api) | `api/app.py`: `sentry_sdk.init` when `SENTRY_DSN` is set | `SENTRY_DSN=""` |
| Sentry (browser) | `ui/src/instrumentation-client.ts`, `/api/config/sentry`, tunnel route `/monitoring` | ui `ENABLE_TELEMETRY=false`, `SENTRY_DSN=""`, build `NEXT_PUBLIC_SENTRY_DSN=""`; nginx 404 on `/monitoring`; CSP |
| Dograh cloud (MPS) `https://services.dograh.com` | `api/constants.py` `MPS_API_URL`; `services/mps_service_key_client.py` (org bootstrap service key, Cloudonix domain, usage, billing), `services/pipecat/service_factory.py` ("dograh" LLM/STT/TTS), `tasks/knowledge_base_processing.py` (F1), recording transcription | `MPS_API_URL=http://127.0.0.1:9` (connection refused inside the container); don't use the "dograh" provider, KB, template creation or upload transcription (PLAN F1, F4) |
| Chatwoot widget `https://chat.dograh.com` | `ui/Dockerfile` `NEXT_PUBLIC_CHATWOOT_URL`/`_TOKEN`; `ui/src/components/ChatwootWidget.tsx` | blanked in our UI build; the widget returns early when either is empty |
| Leads / onboarding forms `https://api-leads.dograh.com` | `ui/src/components/lead-forms/onboardingServiceClient.ts` falls back to it when `NEXT_PUBLIC_ONBOARDING_API_URL` is **empty** | build sets it to `/_buzz/disabled`; nginx answers 404 there |
| Version check on `ghcr.io` (UI server) | `ui/src/app/api/config/latest-version/route.ts` | nginx answers `/api/config/latest-version` with `{"latest":null}`; egress firewall |
| GitHub star count `api.github.com` (browser) | `ui/src/components/layout/GitHubStarBadge.tsx` (overview page in OSS mode) | CSP `connect-src 'self'` blocks it in the browser |
| Google Tag Manager, Meta Pixel, Reo (browser) | `ui/src/app/layout.tsx`, only when `NEXT_PUBLIC_GTM_ID` / `_META_PIXEL_ID` / `_REO_CLIENT_ID` are set | set empty in the build; CSP |
| Google STUN `stun:stun.l.google.com:19302` | `api/routes/webrtc_signaling.py` `get_ice_servers`; `ui/.../useWebSocketRTC.tsx`; `ui/public/embed/dograh-widget.js` | `FORCE_TURN_RELAY=true` removes it on both sides; our coturn relays |
| Cloudflare quick tunnel | compose `cloudflared` service, `tunnel` profile; `api/utils/tunnel.py` polls `http://cloudflared:2000` when the backend URL is localhost or a private IP | no cloudflared service; `BACKEND_API_ENDPOINT` is our hostname, which `is_local_or_private_url` treats as public, so the tunnel lookup never runs |
| OpenAI defaults | provider registry defaults `https://api.openai.com`; QA node defaults to the `openai` provider | every provider set to our vLLM/Speaches `base_url` (PLAN section 5); QA "Use Workflow's LLM"; agent-export lint in `tests/` |
| Other provider SDK endpoints (Deepgram, ElevenLabs, Cartesia, Groq, Hugging Face router, Twilio, Telnyx, Plivo, Exotel, Vonage, Cloudonix, ...) | `api/services/configuration/registry.py`, `services/pipecat/service_factory.py`, `services/telephony/providers/` | only used if configured; we configure none of them |
| Org integrations: Tuner `api.usetuner.ai`, Noveum `api.noveum.ai`, Paygent, BigQuery | `api/services/integrations/` | off unless an org enables them; `TUNER_BASE_URL` blackholed as well |
| Langfuse | `LANGFUSE_*` env or per-org settings | blank until our self-hosted Langfuse exists (Q8) |
| GenderAPI `gender-api.com` | `api/services/gender/gender_service.py` | not called anywhere outside its own test; needs `GENDERAPI_API_KEY`, unset |
| Hugging Face Hub downloads | pipecat smart-turn / VAD models | `HF_HUB_OFFLINE=1`; models are expected inside the image (verify) |

Not an outbound call, but found while reading the source and fixed here:

- **MinIO bucket is world-readable and writable.** On every start the api sets an anonymous `GetObject`, `PutObject`, `DeleteObject` and `ListBucket` policy on `voice-audio` (`api/services/filesystem/minio.py`) and hands browsers plain unsigned URLs. Call recordings are personal data, so nginx allows `/voice-audio/` only from `nginx/staff-cidrs.conf`, only `GET`, `HEAD` and `PUT`, and refuses bucket listing.
- **TURN as an open relay into the LAN.** Anonymous web-widget sessions also receive TURN credentials. coturn denies relaying to private, loopback and link-local ranges, except our own external address.

## Runbook

All commands run in `infra/app/` on the app host unless stated.

### 1. Prerequisites

- Docker Engine with the compose plugin, iptables backend (default).
- The firewall installed in trial mode first (`../firewall/README.md`).
- DNS for `PUBLIC_HOST` (Q2) and a TLS certificate:
  - **Internal CA**: put `fullchain.pem` (leaf then intermediates) and `privkey.pem` in `infra/app/certs/` (git-ignored). Keep `TLS_CERT_DIR=./certs` and the default `TLS_*_FILE`.
  - **Let's Encrypt**: needs public DNS and port 80 reachable. Set `ALLOW_LETSENCRYPT=yes` in the firewall config. Start nginx once with a temporary self-signed certificate, then `certbot certonly --webroot -w infra/app/nginx/acme -d <PUBLIC_HOST>`, and set `TLS_CERT_DIR=/etc/letsencrypt`, `TLS_CERT_FILE=/etc/nginx/certs/live/<PUBLIC_HOST>/fullchain.pem`, `TLS_KEY_FILE=/etc/nginx/certs/live/<PUBLIC_HOST>/privkey.pem`. Deploy hook: `docker compose -f <path>/docker-compose.yml exec nginx nginx -s reload`.

### 2. Build the UI image

On a build machine with internet access (or in an open-egress window, `../firewall/README.md`):

```
./ui-build/build-ui.sh --dry-run          # clone, verify commit, prove every patch target exists
./ui-build/build-ui.sh --save /tmp/buzz-ui.tar.gz
```

Copy the file to the app host and `gunzip -c buzz-ui.tar.gz | docker load`. The script clones `dograh-v1.49.0`, checks it is commit `b86e8edddf4e4c9ba174792b5ceaba40c394a9bb`, runs `patch-ui.sh`, builds with the repository root as context, and scans the built image for the Chatwoot URL and token.

`patch-ui.sh` changes only build variables, logo/favicon files, the background wordmark, and the product name in the page title, logo alt text, mobile header and welcome headings. Every step must find its target or the script exits non-zero. Dograh's BSD 2-Clause notice is kept in the source and also served at `/licences/dograh-LICENSE.txt`.

### 3. Secrets and configuration

```
./scripts/gen-secrets.sh                  # writes .env (mode 600); refuses to overwrite
$EDITOR .env                              # SERVER_IP, PUBLIC_*, TLS_*, INFERENCE_HOST, VLLM/SPEACHES keys
$EDITOR nginx/staff-cidrs.conf            # networks allowed to play recordings and open Grafana
```

`gen-secrets.sh` also creates `nginx/staff-cidrs.conf` from the example. Back up `.env` to the password manager: the Postgres password is baked into its volume on first start.

`ARI_PASSWORD` and `SUPPRESSION_API_KEY` in this `.env` are the values Asterisk and the suppression service must use too.

### 4. Start

```
docker compose config --quiet             # fails on any missing required variable
docker compose build suppression
docker compose up -d
docker compose ps                         # all healthy within about 2 minutes
```

Asterisk must start after the `buzz-app` network exists (its ARI listens on `172.30.0.1`).

### 5. First admin accounts, then close sign-up

1. In `.env` set `ENABLE_SIGNUP=true`, then `docker compose up -d api`.
2. Open `https://<PUBLIC_HOST>/`, sign up the admin accounts.
3. Set `ENABLE_SIGNUP=false`, then `docker compose up -d api`.
4. Check: signing up a test address must now fail.

Then follow PLAN section 5: add the BYOK provider connections (vLLM, Speaches), create the `buzz-local` configuration and make it the organisation default, and archive the "Dograh" connection that org bootstrap created. In the telephony configuration use ARI endpoint `http://asterisk:8088` (or `http://172.30.0.1:8088`), user `dograh`, password `ARI_PASSWORD`.

### 6. Smoke tests

```
curl -fsS http://127.0.0.1:8000/api/v1/health                                # api on loopback
curl -fsS -o /dev/null -w '%{http_code}\n' https://<PUBLIC_HOST>/             # 200 (UI)
curl -fsS https://<PUBLIC_HOST>/api/v1/health                                 # api via nginx
curl -s -o /dev/null -w '%{http_code}\n' https://<PUBLIC_HOST>/api/v1/metrics # 404
curl -s -o /dev/null -w '%{http_code}\n' http://<PUBLIC_HOST>/                # 301
curl -s https://<PUBLIC_HOST>/api/config/latest-version                       # {"latest":null}
curl -fsS -H "X-Dograh-Devops-Secret: $(grep ^DOGRAH_DEVOPS_SECRET= .env | cut -d= -f2)" \
     http://127.0.0.1:8000/api/v1/metrics | head -3                           # metrics text
curl -fsS http://127.0.0.1:8090/health                                        # suppression
docker compose logs coturn | grep 'coturn init: rendered'
```

From a machine outside `staff-cidrs.conf`: `https://<PUBLIC_HOST>/voice-audio/` and any recording URL return 403. From a staff machine, `https://<PUBLIC_HOST>/voice-audio/` returns 403 (no listing) and a recording URL from the UI plays.

### 7. Phase 2 acceptance checks (this component)

| Check | How | Pass |
|---|---|---|
| No egress | packet capture during a full session (`../firewall/README.md`, Verifying step 4) | capture empty |
| Egress blocked, not just unused | `docker compose exec api python -c "import urllib.request; urllib.request.urlopen('https://us.i.posthog.com', timeout=5)"` | fails; `BUZZ-EGRESS-DROP` in `journalctl -k` |
| App starts with MPS blackholed (F4) | first sign-in after a clean install; api logs | sign-in works; MPS errors logged as non-fatal |
| No PostHog/Sentry configured | `curl -s https://<host>/api/config/posthog` and `/api/config/sentry` | `"enabled":false` and empty key/DSN |
| No third-party requests in the browser | browser dev tools, Network tab, filter "-domain:<PUBLIC_HOST>", on every main page | no requests; no CSP violations in the console |
| No exposed data ports | `nmap -Pn -p 5432,6379,8000,8090,9000,9001 <app-ip>` from the LAN | all closed or filtered |
| Proxy trust | `docker compose exec api env \| grep FORWARDED_ALLOW_IPS` | `172.30.0.10` |
| Sign-up closed | sign-up attempt | refused |
| Recordings protected | step 6 | 403 outside staff networks, no listing |
| Test Audio over TURN (relay only) | run Test Audio from a staff browser; `chrome://webrtc-internals` | connected; selected candidate pair is `relay` |
| Branding | UI shows the Buzz logo, title "Buzz", Buzz favicon | yes |

### 8. Upgrade procedure (R4, R5)

Dograh releases about every two weeks. For each upgrade, in staging first:

1. Read the Dograh changelog and diff `docker-compose.yaml`, `api/constants.py`, `ui/Dockerfile` and `ui/next.config.ts` between the old and new tags. Add any new environment variable to our compose file and `.env.example`; look for new outbound URLs (`grep -rhoE "https?://[a-zA-Z0-9._-]+" api ui/src`) and update the table above.
2. In `ui-build/build-ui.sh` change `DOGRAH_TAG`, `DOGRAH_COMMIT` and `IMAGE` (`<version>-buzz1`). Run `--dry-run`. Any target that moved fails loudly: fix `patch-ui.sh`, never skip a step.
3. Build the UI, change the api tag in `docker-compose.yml`, update `versions.lock`.
4. Back up Postgres and MinIO (`infra/backup`). Migrations run automatically when the api starts (`alembic upgrade head`); there is no automatic downgrade.
5. `docker compose up -d`, then repeat the smoke tests and every Phase 2 acceptance check above, including the packet capture.
6. Record the change in `docs/CHANGELOG.md` and, if behaviour changed, `docs/DECISIONS.md`. Then repeat in production.

Rollback: restore the previous tags and the pre-upgrade database backup.
