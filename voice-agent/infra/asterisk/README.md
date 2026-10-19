# Asterisk for Buzz

Asterisk 22.11.0 connects the SIP trunk and staff phones to Dograh. It also enforces the telephony compliance rules that no workflow or API call can bypass (PLAN.md sections 6 and 7, Deviations D4 and D5). This folder holds the image, the config templates and this runbook.

Items marked **(verify)** have not been tested on a running system yet. They are listed at the end.

## Contents

| Path | What |
|---|---|
| `Dockerfile` | Multi-stage build from the official tarball (SHA-256 pinned). Builds only the modules in `modules.list`. Slim Debian runtime, non-root user `asterisk` (uid 2000). |
| `modules.list` | The modules compiled in. Everything else is disabled in menuselect. |
| `docker-compose.yml` | `network_mode: host`, volumes, ulimits, health check. |
| `entrypoint.sh` | Validates `.env`, renders `config/*.conf` with `envsubst` and an explicit variable list, generates the calling-hours and trunk-auth includes, refuses to start without the disclosure prompts, waits for `172.30.0.1`, then runs Asterisk. |
| `healthcheck.sh` | Checks that Asterisk is fully booted, the key modules are running, the trunk endpoint exists and the dialplan is loaded. |
| `config/` | Templates. `env.list` is the list of variables `envsubst` may replace, with their defaults and validation rules. |
| `local.example/` | Site files to copy into `local/` (git-ignored): DID table, staff phones, transfer target overrides. |
| `sounds/` | Prompt list, format, and a Speaches script that makes placeholder prompts for staging. |
| `tests/` | Offline tests: template rendering and a static check of the dialplan. |

## How calls flow

```
Inbound   carrier -> [from-trunk] -> DID table (local/extensions_dids.conf)
          -> [buzz-inbound]: Answer, Playback(disclosure-inbound), Dial(Local/<agent number>@buzz-stasis/n)
          -> [buzz-stasis]: Stasis(dograh_xxx)   (this Local half is still in state Ring)

Outbound  Dograh originates Local/<number>@buzz-outbound/n into its Stasis app
          -> [buzz-outbound]: CURL suppression check (ALLOW/DENY, fail closed)
             -> calling hours -> trunk channel limit
             -> Dial(PJSIP/<number>@zm-trunk, 18 s, U(disclosure to callee))
          Dograh's agent starts when the callee answers and the disclosure has finished.

Transfer  Dograh originates Local/<ext>[*<topic>]@buzz-transfer/n into its Stasis app
          -> [buzz-transfer]: Dial(PJSIP/<ext>, 20 s, U(screen: intro, topic, "press 1"))
             accept -> bridged -> Dograh bridges the caller in and drops off
             decline / no answer -> [buzz-take-message]: Answer, prompt, Record, webhook to n8n
```

### What we found in the Dograh source (v1.49.0)

These findings shaped the design. File paths are in the Dograh repository.

- **Media WebSocket.** The route is `/api/v1/telephony/ws/ari` (`api/routes/telephony.py`, `websocket_ari_endpoint`; prefix `/api/v1` from `api/app.py`). It accepts the `media` subprotocol. For each call, Dograh calls `POST /ari/channels/externalMedia` with these parameters (`api/services/telephony/ari_manager.py`, `_create_external_media`):
  - `external_host=<ws_client_name>`, `transport=websocket`, `encapsulation=none`, `connection_type=client`, `format=ulaw`, `direction=both`;
  - `transport_data=v(workflow_id=…,organization_id=…,workflow_run_id=…[,token=…])`.

  So the `uri` in `websocket_client.conf` must have no query string. Dograh's own guide does not set `connection_type` in `websocket_client.conf`, and chan_websocket does not check it in 22.11.0 (`channels/chan_websocket.c`). We set `per_call_config`, as PLAN.md section 6 says and as Asterisk's sample does for chan_websocket.
- **ARI events.** Dograh connects to `ws://<ARI endpoint host>/ari/events?api_key=<user>:<password>&app=<stasis app>&subscribeAll=true` (`ari_manager.py`, `ws_url`). The Stasis app name is generated as `dograh_` plus 12 hex characters (`providers/ari/__init__.py`, `_generate_stasis_app_name`). Copy it into `DOGRAH_STASIS_APP`.
- **Inbound needs the channel un-answered.** Dograh handles a `StasisStart` as an inbound call only when `channel.state == "Ring"`. Any other state is treated as an outbound leg it originated, and the call is dropped if no `workflow_run_id` is present (`ari_manager.py`, `_handle_event`). This is why `buzz-inbound` cannot `Answer()` and then `Playback()` before `Stasis()` on the same channel. Instead it answers, plays the disclosure, and then dials a Local channel whose `;2` half enters Stasis while still ringing. Dograh takes the called number from `channel.dialplan.exten` and the caller from `channel.caller.number` (`_handle_inbound_stasis_start`). The DID table sets that extension to the number registered in Dograh.
- **Outbound.** Dograh calls `POST /ari/channels` with these parameters (`providers/ari/provider.py`, `initiate_call`):
  - `endpoint` = the template applied to the number (`providers/ari/dial_string.py`), and `app` = the Stasis app;
  - `appArgs = workflow_run_id=…,workflow_id=…`;
  - `callerId` = the From number, if one is set.

  It sends **no `timeout`**, so Asterisk's default of 30 s applies (`rest-api/api-docs/channels.json`). Ringing plus the outbound disclosure must fit inside those 30 s.
- **Retries.** If an outbound channel dies before Stasis, Dograh maps the hangup cause as follows (`ari_manager.py`, `_release_destroyed_channel`): 17 → busy, 18 or 19 → no answer, anything else → failed. Campaigns retry only busy, no answer and voicemail (`api/services/campaign/campaign_retry.py`, `api/constants.py`). Our refusals therefore use 21, 29, 34 and 41, so they are never retried.
- **Transfers.** The destination goes through **the same Dial String Template** unless it already names a channel technology (`provider.py`, `transfer_call`; `dial_string.py`, `build_dial_string`). Dograh originates it into the Stasis app with:
  - `appArgs=transfer,<transfer_id>`;
  - `timeout` = the tool's timeout (default 30 s, `pipecat_engine_custom_tools.py`).

  It sends **nothing else**: no channel variables and no reason. Dograh counts the transfer as successful when that channel is answered and enters Stasis (`ari_manager.py`, `_handle_destination_answered`). It counts it as failed if the channel is destroyed first (`_map_hangup_cause_to_message`).

  The dynamic resolver's `custom_message` (`api/services/workflow/tools/transfer_resolver.py`) is spoken **to the caller** before the transfer starts (`queue_speech(resolved_transfer.message)` in `pipecat_engine_custom_tools.py`). It never reaches Asterisk. The "transfer introduction" works only with providers that support it (`supports_transfer_introduction`, Twilio).

  So the transfer reason reaches staff only through the destination string. We add an optional topic token, `Local/1001*events@buzz-transfer/n`, which plays the recorded prompt `transfer-topic-events`. This resolves the PLAN.md section 1 "verify" item and risk R7: the free-text `custom_message` cannot reach Asterisk, so staff hear a fixed intro plus a topic prompt.

## Build

```
cd infra/asterisk
docker compose build
```

- The tarball comes from `downloads.asterisk.org` and is checked against the SHA-256 in the Dockerfile. That value was taken from the official `.sha256` file and matched a fresh download on 19 Oct 2026.
- `--build-arg VERIFY_GPG=1` also checks the detached signature against key `F2FC93DB7587BD1FB49E045A5D984BE337191CE7`, fetched from keys.openpgp.org (**verify** the key's owner first).
- Bundled pjproject 2.17 is downloaded by `configure` and checked against the md5 file inside the verified tarball. The core sounds (en, μ-law, needed for the record beep) are checked against their published `.sha1`.
- menuselect fails the build on an unknown module name (`--enable` of a missing member) or an unmet dependency (`--check-deps`).
- The runtime packages are worked out with `ldd` from what the binaries link against, so the runtime image has no compiler or `-dev` packages.
- After the first good build, pin the base image by digest (`DEBIAN_IMAGE=debian:trixie-slim@sha256:…`) and record it in `infra/asterisk/versions.lock` (PLAN.md section 2).

## Configure

1. **Environment.** Run `cp .env.example .env`, then fill it in. `config/env.list` holds the rules, which the entrypoint enforces at start:
   - Secrets are 16 or more characters from `[A-Za-z0-9._~+=-]`, because `;` starts a comment in Asterisk config. Generate them with `openssl rand -hex 32`.
   - `ARI_PASSWORD` and `SUPPRESSION_API_KEY` are the shared values in INTERFACES.md.
2. **Site files.** Run `mkdir -p local && cp local.example/* local/`, then edit:
   - `extensions_dids.conf`: one block per DID, in the form the carrier sends it. Check this with `pjsip set logger on` on the first test call. Each block sets `BUZZ_AGENT_NUMBER` to the number registered in Dograh.
   - `pjsip_staff.conf`: staff phones, with real random passwords.
   - `extensions_transfer_targets.conf`: optional ring groups. Never point it at the trunk.
3. **Prompts.** Put `disclosure-inbound` and `disclosure-outbound` in `sounds/buzz/` (see `sounds/README.md`). Asterisk will not start without them.
4. **Trunk.** Set the following, then re-check them once the carrier confirms (PLAN.md section 9 Q4):
   - `ZM_TRUNK_HOST`, and `ZM_TRUNK_MATCH` (every carrier signalling and media IP);
   - `ZM_TRUNK_NUMBER_FORMAT`: `e164`, `international` (no `+`) or `national` (leading 0);
   - `ZM_TRUNK_MAX_CHANNELS`;
   - `ZM_TRUNK_USERNAME`, `ZM_TRUNK_PASSWORD` and `ZM_TRUNK_REGISTER` only for a credentials-based trunk.

   Also ask the carrier to block international and premium destinations on their side.
5. **NAT.** Set `ASTERISK_PUBLIC_IP` to the public address the carrier sees, and `ASTERISK_LAN_CIDR` to the local network(s).
6. **Start.** Start the app stack first, so the `buzz-app` network and 172.30.0.1 exist. Then run `docker compose up -d` and `docker compose logs -f asterisk`.

Config is rendered when the container starts. After you change `.env`, a template or `local/`, run `docker compose up -d --force-recreate`. A `dialplan reload` alone does not re-render anything.

## Firewall (app host)

Asterisk uses host networking, so the host firewall applies to it directly. Docker port publishing is not involved. Example with ufw:

```
# SIP and RTP from the carrier only (repeat for each IP in ZM_TRUNK_MATCH)
ufw allow proto udp from 198.51.100.10 to any port 5060
ufw allow proto udp from 198.51.100.10 to any port 10000:20000
# Staff phones on the LAN
ufw allow proto udp from 10.0.0.0/24 to any port 5060
ufw allow proto udp from 10.0.0.0/24 to any port 10000:20000
# ARI/HTTP only from containers on buzz-app (Dograh api, Prometheus)
ufw allow proto tcp from 172.30.0.0/24 to 172.30.0.1 port 8088
ufw deny 8088/tcp
```

- Asterisk also blocks SIP from anywhere else (`acl-sip` in `pjsip.conf`) and ARI from outside `ARI_PERMIT_CIDRS` (`ari.conf`).
- Do not open 5061/tcp: TLS SIP is not configured.
- Outbound: Asterisk needs only the carrier and loopback (Dograh 127.0.0.1:8000, suppression service 127.0.0.1:8090). Add these to the egress allow-list (PLAN.md F5).

## Configure Dograh (Telephony configurations → Add configuration → Asterisk ARI)

| Dograh field (source: `providers/ari/__init__.py`) | Value |
|---|---|
| ARI Endpoint | `http://172.30.0.1:8088` |
| ARI Username | `dograh` |
| ARI Password | `ARI_PASSWORD` |
| websocket_client.conf Name | `dograh` |
| Dial String Template | `Local/{number}@buzz-outbound/n` (D4; see the note below) |
| From Extensions | optional. If `OUTBOUND_CLI` is set, the dialplan overrides the caller ID. |
| Stasis App Name | generated on save. Copy it into `DOGRAH_STASIS_APP`, then `docker compose up -d --force-recreate`. |

- **Template suffix `/n`.** It stops Asterisk from optimising the Local channel pair away. Without it, the `;1` channel Dograh tracks (`dograh-call-…`) could leave the bridge, and Dograh would tear down the call. PLAN.md and INTERFACES.md show the template without `/n`. **(verify** on staging that both forms work, and keep `/n` unless it causes a problem.)
- **Phone numbers.** Add each `BUZZ_AGENT_NUMBER` from the DID table as a phone number, and set its inbound workflow.
- **Transfer tools.** The destination must name the channel itself, for example `Local/1001@buzz-transfer/n` or `Local/1001*events@buzz-transfer/n`. A plain `1001` would go through the Dial String Template into `buzz-outbound`, which would deny it as `invalid`. With the dynamic resolver, return the same form in `transfer_context.destination`.
- **Transfer timeout: 60 s.** Dograh uses the tool timeout both as its wait and as the ARI originate timeout. Ringing (`TRANSFER_RING_TIMEOUT`, 20 s) plus screening (about 15 s) plus the start of take-a-message must fit inside it. With the default 30 s, a slow decision by staff ends the transfer from Dograh's side.
- **Token enforcement.** `TELEPHONY_WS_TOKEN_ENFORCE=true` works with this setup: Dograh adds the token through `v()` on every externalMedia channel.

## Operations

- **Logs.**
  - `docker compose logs asterisk` shows the console at verbose level 3.
  - `/var/log/asterisk/messages.log` and `security.log` are on the `asterisk-logs` volume. Rotate them monthly with `docker exec buzz-asterisk asterisk -rx "logger rotate"` and delete files older than 90 days.
  - Every refusal is logged at NOTICE with the reason (`Buzz outbound: … denied by suppression service: suppressed`, `outside permitted calling hours`, `trunk limit reached`). Look up the matching hangup cause in the comment at the top of `extensions.conf`.
- **CLI.** `docker exec -it buzz-asterisk asterisk -rvvv`. Useful commands (22.11.0 has no CLI command for `websocket_client` objects; a broken media URI shows up as chan_websocket errors in the log):
  - `pjsip show endpoints`
  - `pjsip set logger on`
  - `ari show apps`
  - `ari show status`
  - `group show channels`
- **Metrics.** `res_prometheus` serves `http://172.30.0.1:8088/metrics` with Basic Auth. Prometheus scrape job:
  ```yaml
  - job_name: asterisk
    static_configs: [{ targets: ["172.30.0.1:8088"] }]
    basic_auth: { username: prometheus, password_file: /etc/prometheus/asterisk_password }
  ```
- **Messages.** Take-a-message recordings are written to `/var/spool/asterisk/buzz-messages/<date>-<ext>-<uniqueid>.wav` on the `asterisk-spool` volume. They are personal data: add them to the DPIA and delete them after 90 days, for example with a nightly `find … -name '*.wav' -mtime +90 -delete`.
- **Message webhook.** If `MESSAGE_WEBHOOK_URL` is set, a hangup handler POSTs form fields to n8n with the header `X-Buzz-Webhook-Secret`: `event=buzz.message`, `message_id`, `extension`, `topic`, `file`, `size_bytes`, `record_status`, `connected_number`, `at`. This is a stub. n8n must fetch the file itself, or a later step uploads it to MinIO. `connected_number` is a best guess at the caller **(verify)**, because Dograh does not pass the caller to the transfer leg. Match messages to the Dograh run by time and extension.
- **Calling hours.**
  - `CALL_HOURS` and `CALL_CLOSED_DAYS` generate `extensions_hours.conf`, using GotoIfTime with the time zone `Africa/Lusaka` (tzdata is in the image).
  - The end minute is inclusive: `17:59` means calls stop at 18:00.
  - Holidays that move each year (Easter, for example) must be added to `CALL_CLOSED_DAYS` each year.
  - Hours apply to outbound calls only. Inbound calls are always answered.
- **Channel limit.**
  - Inbound and outbound calls both join `GROUP(buzz)=trunk`.
  - An outbound call is refused (cause 34) when the count, including itself, would exceed `ZM_TRUNK_MAX_CHANNELS`.
  - Inbound calls are never refused by this check; the carrier enforces its own limit.
  - Two outbound calls racing for the last channel may both be refused. That is the safe side.

## Tests (offline)

```
python3 -m venv .venv && .venv/bin/pip install pytest
.venv/bin/pytest -q infra/asterisk/tests
```

The tests need `bash` and `envsubst`. They render every template through `entrypoint.sh` and check the following:

- no environment placeholder is left;
- every `${…}` left is a dialplan variable;
- bad environments are refused;
- every Goto, Gosub, include, `U()` and hangup-handler target exists;
- every application and function used is in `modules.list`;
- the compliance order holds: disclosure before Stasis, and suppression, then hours, then limit, then dial, with fail-closed paths and no retryable refusal causes.

## Test calls (staging, then production)

| # | Call | Expected |
|---|---|---|
| 1 | Inbound to each DID | The disclosure plays in full; pressing keys does not skip it. Then the right Dograh agent answers. Dograh shows `caller_number` = your mobile and `called_number` = `BUZZ_AGENT_NUMBER`. |
| 2 | Inbound with Dograh stopped | The disclosure plays, then `service-unavailable`, then hang-up. |
| 3 | Inbound to an unknown DID | Rejected (cause 1), with a NOTICE in the log. |
| 4 | Outbound to an allowed mobile, within hours | The phone rings, the callee hears the disclosure, and the agent speaks only after it ends. Caller ID is `OUTBOUND_CLI`. |
| 5 | Outbound to a suppressed number | No dial; cause 21. The log says `denied … suppressed`. The Dograh run is "failed" and is not retried. |
| 6 | Outbound to `+44…` / a premium number / a short code / `abc` | No dial; cause 21. Reasons: `international`, `premium`, `short_code`, `invalid`. |
| 7 | Outbound with the suppression service stopped, or a wrong API key | No dial; cause 41 (fail closed). |
| 8 | Outbound outside hours (temporarily set `CALL_HOURS` to a past window) | No dial; cause 29. |
| 9 | Outbound with `ZM_TRUNK_MAX_CHANNELS=1` and one call up | The second call is refused with cause 34. |
| 10 | Outbound, callee does not answer / is busy | Dograh shows no-answer (19) / busy (17), and the campaign retries. |
| 11 | Transfer, staff press 1 | Staff hear the intro, the topic and the menu; the caller hears hold music. After 1, they are connected and Dograh drops off. |
| 12 | Transfer, staff press 2 / hang up / do not answer within 20 s | The caller hears `transfer-leave-message`, records, and hears thanks. The WAV is in `buzz-messages/` and n8n receives the webhook. |
| 13 | Transfer with destination `1001` (no `Local/`) | Denied in buzz-outbound as `invalid`. This confirms the tool must use the full dial string. |
| 14 | Packet capture during calls | SIP/RTP only to and from the carrier IPs. Nothing leaves the host except to the carrier. |

## Phase 4 acceptance

- The image builds from the pinned tarball and the health check is green. `module show like chan_websocket` and `module show like res_websocket_client` show **Running**.
- Dograh shows the ARI configuration connected, with no reconnect loop in the logs.
- Test calls 1 to 14 pass on staging, and 1, 4, 5, 11 and 12 also pass in production.
- Disclosure: in 10 recorded test calls (5 inbound, 5 outbound), the full disclosure is heard before any agent speech.
- Compliance: no dial attempt reaches the carrier for a DENY, an error, out-of-hours or over-limit call. Check this against the carrier CDRs as well as our logs.
- Firewall: from an outside host, nmap shows 5060/udp and 8088/tcp filtered unless the scan comes from a carrier IP or the Dograh network.
- Offline tests pass in CI.

## Open items (verify)

1. Owner of GPG key `F2FC93DB7587BD1FB49E045A5D984BE337191CE7` (read from the signature, not yet matched to the Asterisk release team). The build uses the SHA-256 check by default.
2. `debian:trixie-slim` builds cleanly. Pin it by digest after the first build.
3. The inbound Local-channel handover:
   - Dograh accepts the `;2` channel (state Ring), and `caller.number` is the real caller (the Local channel copies caller ID from Dial's connected line, in `main/core_unreal.c`);
   - the caller hears ringback, not silence, while Dograh sets up.
4. Use `/n` on the Dial String Template and on transfer destinations. Check whether calls work without it.
5. The carrier's number format and DID format, and whether it sends early media and ringback.
6. `connected_number` in the message webhook carries the caller after Dograh bridges (connected-line updates through a Stasis bridge).
7. Speaches returns WAV for `response_format: "wav"` (`sounds/generate_placeholders.sh`).
8. Behaviour when Dograh's transfer timeout fires while staff are still being screened. ARI hangs up the `;1` leg; check that no orphan channel or stray recording is left.
