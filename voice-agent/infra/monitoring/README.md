# Monitoring (app host)

Prometheus, Alertmanager, Grafana, blackbox exporter and node-exporter for Buzz. Hosts, ports and networks follow [docs/INTERFACES.md](../../docs/INTERFACES.md).

| Component | Image | Listens | Reached by |
|---|---|---|---|
| Prometheus | `prom/prometheus:v3.15.0` | `127.0.0.1:9090`, `prometheus:9090` on `buzz-app` | Grafana, SSH tunnel |
| Alertmanager | `prom/alertmanager:v0.34.1` | `127.0.0.1:9093` | Prometheus, SSH tunnel |
| Grafana | `grafana/grafana:13.2.4` | `127.0.0.1:3000`, `grafana:3000` on `buzz-app` | nginx at `/grafana/` |
| Blackbox exporter | `prom/blackbox-exporter:v0.28.0` | `blackbox:9115` on `buzz-app` only | Prometheus |
| node-exporter (app) | `prom/node-exporter:v1.12.1` | `172.30.0.1:9100` (host network) | Prometheus |
| node-exporter, DCGM exporter (inference) | defined in `infra/inference/docker-compose.yml` | `${INFERENCE_LAN_IP}:9100`, `:9400` | Prometheus (firewall: app host only) |

Blackbox v0.29.0 was released on 7 October 2026. It is less than two weeks old, so we stay on v0.28.0 for now.

## Files

| Path | Purpose |
|---|---|
| `docker-compose.yml` | the stack (network `buzz-app`, external) |
| `.env.example` | copy to `.env`; Grafana settings, scrape addresses, secrets |
| `render.sh` | turns `.env` into `prometheus/targets/*.json` (file_sd) and `secrets/*` |
| `prometheus/prometheus.yml` | scrape jobs (static; no addresses) |
| `prometheus/rules/buzz.rules.yml` | alert and recording rules |
| `prometheus/rules/tests/` | `promtool test rules` unit tests |
| `blackbox/blackbox.yml` | `http_2xx` probe module |
| `alertmanager/alertmanager.yml` | routes and a webhook receiver (email/Slack TBD) |
| `grafana/provisioning/` | Prometheus data source (uid `buzz-prometheus`) and dashboard provider |
| `grafana/dashboards/buzz-overview.json` | the "Buzz overview" dashboard |

Prometheus does not expand environment variables in its config file. For that reason, every address that depends on the deployment goes into a file_sd target file. Prometheus re-reads these files on its own, so changing a target needs only `sudo ./render.sh`, with no restart.

## First start

```bash
cd /opt/buzz/voice-agent/infra/monitoring
cp .env.example .env && chmod 600 .env       # fill in every empty value
sudo ./render.sh                             # writes targets + secrets (secrets owned by uid 65534)
docker compose up -d
docker compose ps
curl -fsS http://127.0.0.1:9090/-/ready
curl -fsS http://127.0.0.1:9090/api/v1/targets | jq -r '.data.activeTargets[] | "\(.labels.job)\t\(.health)\t\(.lastError)"'
```

Before the first start, the other stacks need these settings:

- **Dograh (`infra/app/.env`):** `ENABLE_PROMETHEUS_METRICS=true` and `DOGRAH_DEVOPS_SECRET=<random>`, with the same secret in this `.env`. If the secret is unset, `/api/v1/metrics` returns 503; if it does not match, 403; if metrics are disabled, 404 (`api/routes/main.py`).
- **Asterisk (`prometheus.conf`):** `enabled=yes`, `uri=metrics`, `auth_username=prometheus` (`ASTERISK_METRICS_USER` in `infra/asterisk/config/env.list`), `auth_password=<ASTERISK_METRICS_PASSWORD>`. Load `res_prometheus.so`. The metrics share the ARI HTTP server on `172.30.0.1:8088`.
- **Inference host:** the DCGM exporter (`:9400`) and node-exporter (`:9100`) come from `infra/inference/docker-compose.yml`. The firewall must allow 8000, 8001, 9100 and 9400 from the app host.
- **nginx:** proxy `/grafana/` without stripping the prefix, because Grafana serves from the sub-path:

  ```nginx
  location /grafana/ {
      proxy_set_header Host $host;
      proxy_pass http://grafana:3000;
  }
  location /grafana/api/live/ {
      proxy_http_version 1.1;
      proxy_set_header Upgrade $http_upgrade;
      proxy_set_header Connection "upgrade";
      proxy_set_header Host $host;
      proxy_pass http://grafana:3000;
  }
  ```

  nginx should also **deny** `/api/v1/metrics` and `/api/v1/health/active-calls` on the public server. They are secret-protected, but nobody outside needs them. (`infra/app/nginx/templates/buzz.conf.template` already does both.)

## What is scraped

| Job | Target | Path / auth | Source of truth |
|---|---|---|---|
| `dograh` | `api:8000` (+1 per extra uvicorn worker) | `/api/v1/metrics`, header `X-Dograh-Devops-Secret` | Dograh `api/routes/main.py` (route + `_verify_devops_secret`), `api/app.py` (`API_PREFIX="/api/v1"`), `deploy/observability/prometheus/prometheus.yml` (same header-from-file pattern, one target per worker) |
| `vllm` | `${INFERENCE_HOST}:8000` | `/metrics`, no auth | vLLM v0.31.0 `vllm/v1/metrics/loggers.py` |
| `blackbox_http` | Speaches `:8001/health`, vLLM `/health`, Dograh `/api/v1/health`, suppression `/health`, Grafana `/api/health` | via `blackbox:9115/probe` | Speaches has **no `/metrics`**: it only pushes OTLP when `OTEL_EXPORTER_OTLP_ENDPOINT` is set (`src/speaches/config.py`); `/health` is public (`src/speaches/routers/misc.py`) |
| `dcgm` | `${INFERENCE_HOST}:9400` | `/metrics` | dcgm-exporter 4.8.4 `etc/default-counters.csv` |
| `node` | `172.30.0.1:9100` (app), `${INFERENCE_HOST}:9100` | `/metrics` | label `host=app\|inference` |
| `asterisk` | `172.30.0.1:8088` | `/metrics`, basic auth `prometheus` | Asterisk `configs/samples/prometheus.conf.sample` (`uri = metrics`, default), `res/prometheus/*.c` |
| `suppression` | `suppression:8080` (Docker network) | `/metrics`, no auth | INTERFACES.md |

### Metric names the rules and dashboard rely on

| Metric | From | Notes |
|---|---|---|
| `dograh_active_calls` | Dograh `api/services/observability/metrics.py` | gauge, per worker; sum over workers |
| `dograh_response_latency_seconds_bucket{kind="user_to_bot"}` | same, recorded in `pipeline_metrics.py` | the OTel exporter adds `_seconds` (Dograh's own dashboard uses this name) |
| `dograh_ai_latency_seconds_bucket{service,model,kind,conversation_type}` | same | `service` is the Pipecat class name, e.g. `…STTService`/`…TTSService`; `kind` values come from Pipecat **(verify)** |
| `vllm:time_to_first_token_seconds_bucket` | vLLM `loggers.py` | labels `model_name`, `engine` |
| `vllm:num_requests_waiting`, `vllm:num_requests_running` | same | gauges |
| `vllm:kv_cache_usage_perc` | same | 0–1 |
| `vllm:generation_tokens_total`, `vllm:prompt_tokens_total`, `vllm:prompt_tokens_cached_total` | same | counters (`_total` added by prometheus_client) |
| `vllm:e2e_request_latency_seconds_bucket`, `vllm:request_queue_time_seconds_bucket` | same | |
| `DCGM_FI_DEV_GPU_UTIL`, `DCGM_FI_DEV_FB_USED`, `DCGM_FI_DEV_FB_FREE`, `DCGM_FI_DEV_GPU_TEMP`, `DCGM_FI_DEV_XID_ERRORS` | dcgm-exporter default counters | memory in MiB; % = used / (used + free) |
| `asterisk_pjsip_outbound_registration_status` | Asterisk `res/prometheus/pjsip_outbound_registrations.c` | 0 Unregistered, 1 Registered, 2 Rejected; **absent** for IP-authenticated trunks |
| `asterisk_channels_count` | `res/prometheus/channels.c` | |
| `suppression_checks_total{endpoint,decision,reason}` | `integrations/suppression-service/suppression/app.py` | `decision` = `allow`/`deny`; `reason` = INTERFACES.md reasons |
| `buzz_backup_last_success_timestamp_seconds`, `buzz_backup_last_run_success` | `infra/backup/backup.sh` (textfile collector) | |
| `buzz_retention_last_success_timestamp_seconds` | `infra/backup/retention.sh` | |

## Alerts

| Alert | Fires when | Severity | First steps |
|---|---|---|---|
| `DograhApiDown` / `DograhHealthProbeFailing` | scrape or `/api/v1/health` fails for 2 m | critical | `docker compose -f infra/app/... ps`; `docker logs api`; for a 403/503 scrape error, check `DOGRAH_DEVOPS_SECRET` in both `.env` files and re-run `render.sh` |
| `VllmDown` | vLLM scrape fails for 1 m | critical | on inference: `docker logs vllm`, `nvidia-smi`; a restart reloads the model (about 1–2 min) |
| `SpeachesDown` | `/health` fails for 1 m | critical | `docker logs speaches`; check GPU memory |
| `VllmRequestsQueued` / `VllmQueueLong` | requests waiting for 2 m / ≥5 for 1 m | warning / critical | too many concurrent calls for the GPU: lower `DEFAULT_ORG_CONCURRENCY_LIMIT`; check KV cache |
| `VllmTtftP95High` | TTFT p95 > 0.5 s for 5 m with traffic | warning | prompt grew? prefix cache hits falling? GPU shared with Speaches? |
| `VllmKvCacheNearlyFull` | KV cache > 90% for 5 m | warning | preemptions follow; reduce `--max-model-len` or concurrency |
| `DograhResponseLatencyHigh` | user→bot p95 > 1.2 s for 10 m | warning | compare the STT/TTS/LLM panels to find the slow stage |
| `GpuMemoryHigh` | > 95% for 5 m | warning | `nvidia-smi`; Speaches loading another model? `*_MODEL_TTL` |
| `GpuTemperatureHigh` / `Critical` | > 83 °C 5 m / > 90 °C 2 m | warning / critical | airflow, fans, room temperature; thresholds depend on the card **(verify with `nvidia-smi -q -d TEMPERATURE`)** |
| `GpuXidError` | XID reported | warning | `dmesg | grep -i xid`; look up the code |
| `AsteriskDown` | `/metrics` fails 1 m | critical | `docker logs asterisk`; did it start before `buzz-app` existed (172.30.0.1 not bound)? |
| `AsteriskTrunkNotRegistered` | status ≠ 1 for 2 m | critical | `asterisk -rx "pjsip show registrations"`; carrier credentials and IPs |
| `SuppressionServiceDown` | scrape fails 1 m | critical | outbound calls **stop** (fail closed). `docker logs suppression` |
| `SuppressionDenySpike` | > 50% of ≥20 checks denied in 10 m | warning | wrong campaign CSV or number-normalisation bug; pause the campaign |
| `DiskSpaceHigh` / `Critical` | > 85% / > 95% | warning / critical | `du -xh / | sort -h | tail`; MinIO volume, backups, Docker images |
| `HostMemoryHigh`, `HostCpuHigh` | > 90% | warning | |
| `NodeExporterDown` | host unreachable | critical | inhibits warnings for that host |
| `BackupTooOld` | last success > 26 h ago | critical | `journalctl -u buzz-backup.service -n 200`; see `infra/backup/README.md` |
| `BackupLastRunFailed`, `BackupMetricMissing` | last run failed / never succeeded | warning | as above |
| `RetentionJobTooOld` | retention not applied for 26 h | warning | `journalctl -u buzz-retention.service` |
| `AlertmanagerNotificationsFailing` | webhook delivery failing | warning | check `ALERT_WEBHOOK_URL` and the egress allow-list |

Run the rule tests after any change:

```bash
promtool check config prometheus/prometheus.yml   # inside the container: docker compose exec prometheus promtool check config /etc/prometheus/prometheus.yml
promtool check rules prometheus/rules/buzz.rules.yml
promtool test rules prometheus/rules/tests/buzz.rules.test.yml
curl -X POST http://127.0.0.1:9090/-/reload      # apply
```

## Alert routing

The only receiver for now is a webhook, set by `ALERT_WEBHOOK_URL` in `.env` and written to `secrets/alertmanager_webhook_url`. Alertmanager reads it with `url_file`. Email and Slack receivers are present but commented out in `alertmanager/alertmanager.yml`. To route a class of alert elsewhere, add a matcher under `route.routes`, for example `component="compliance"`. Then validate the config with `amtool check-config` and reload with `curl -X POST http://127.0.0.1:9093/-/reload`. The receiver host must be on the egress allow-list.

## Grafana

- URL: `https://${PUBLIC_HOST}/grafana/`. Set by `GF_SERVER_ROOT_URL` and `GF_SERVER_SERVE_FROM_SUB_PATH=true`.
- Access: anonymous access, sign-up and basic API auth are all off. The admin password comes from `GRAFANA_ADMIN_PASSWORD`, and Grafana uses it only on first start. To change it later, run `docker compose exec grafana grafana cli admin reset-admin-password …`.
- **No outbound calls.** The following settings come from `conf/defaults.ini` at v13.2.4:
  - `GF_ANALYTICS_REPORTING_ENABLED`, `GF_ANALYTICS_CHECK_FOR_UPDATES`, `GF_ANALYTICS_CHECK_FOR_PLUGIN_UPDATES` and `GF_ANALYTICS_FEEDBACK_LINKS_ENABLED` are `false`.
  - `GF_NEWS_NEWS_FEED_ENABLED=false`.
  - `GF_SECURITY_DISABLE_GRAVATAR=true`.
  - `GF_SNAPSHOTS_EXTERNAL_ENABLED=false`.
  - `GF_PUBLIC_DASHBOARDS_ENABLED=false`.
  - `GF_PLUGINS_PREINSTALL_DISABLED=true` and `GF_PLUGINS_PREINSTALL_AUTO_UPDATE=false`.
  - `GF_PLUGINS_PLUGIN_ADMIN_ENABLED=false` (no catalogue calls to grafana.com).
  - `GF_PLUGINS_PUBLIC_KEY_RETRIEVAL_DISABLED=true`.

  The default-deny egress rules are the backstop. Confirm with the packet capture in the Phase acceptance test.
- The dashboard is provisioned read-only. To change it, edit `grafana/dashboards/buzz-overview.json` (export from the UI, then commit). Panel colours use the BongoHive AI Lab palette:
  - Neural Slate shades `#5D707F`, `#7890A0` and `#B6CCE0` for ordinary series.
  - Warm Innovation Gold `#E8A000` for the highlighted series (p95, waiting, deny) and for threshold breaches.

  Every multi-series panel has a table legend, so identity never depends on colour alone.

## Backup of monitoring data

Prometheus data (30 days or 20 GB) and Grafana state are rebuildable, so they are **not** backed up. All configuration is in git.
