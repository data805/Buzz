# Front desk agent (inbound): DRAFT

**Status: draft.** `workflow.json` follows the Dograh v1.49.0 schema as read from the source, and it passes `tests/lint/lint_workflows.py --strict`. It has **not** been imported into a running Dograh yet. Check the items under "Verify on staging" before relying on it.

| File | What it is |
|---|---|
| `global_prompt.md` | Source of truth for the Global node prompt (persona, hard rules, facts). |
| `knowledge_base.md` | Knowledge block, filled from bongohive.co.zm on 21 Oct 2026; run `build_prompt.py` after editing it. Original note: placeholder knowledge block (hours, programmes, plans, events). It gets appended to the Global prompt once it's filled. |
| `tools/opt_out.json` | HTTP tool: `POST http://suppression:8080/v1/opt-out`, authenticated with `X-API-Key`. |
| `tools/transfer_to_staff.json` | Transfer tool to `Local/2000@buzz-transfer/n`, the reception ring group (PLAN D5). Timeout 60 s, as `infra/asterisk/README.md` requires. |
| `tools/end_call.json` | End-call tool, with a reason. |
| `workflow.json` | `{name, workflow_definition, workflow_configurations}`. |

## Why `http://suppression:8080` and not `127.0.0.1:8090`

The tool runs inside the Dograh `api` container. There, `127.0.0.1` is the container itself, so it can't reach the host's loopback port 8090. Both containers sit on the `buzz-app` network, so the tool uses the suppression service's Docker DNS name and its container port. This needs the compose service to be named `suppression` and to listen on 8080 inside the container. Dograh allows private and internal URLs in `DEPLOYMENT_MODE=oss` (`api/utils/url_security.py:21-22`, called from `api/services/workflow/tools/custom_tool.py:461`).

## Import steps (manual until a script exists)

1. **Credential.** Create it with `POST /api/v1/credentials/` and the body `{"name": "suppression-api-key", "credential_type": "api_key", "credential_data": {"header_name": "X-API-Key", "api_key": "<SUPPRESSION_API_KEY>"}}` (`api/routes/credentials.py`, `api/db/models.py:1134-1139`). Then put its `uuid` into `tools/opt_out.json` → `definition.config.credential_uuid`. Don't commit the key.
2. **Tools.** For each `tools/*.json`, call `POST /api/v1/tools/` with `name`, `description`, `category`, `icon`, `icon_color` and `definition` (`api/schemas/tool.py:648-708`). Leave out the repo-only keys `credential_name` and `tool_uuid`. Record each returned `tool_uuid` back into its file as `"tool_uuid"`.
3. **Placeholders.** In `workflow.json`, replace each `tool:<name>` in `tool_uuids` with the real UUID. The linter accepts either form.
4. **Upload.** Upload in the UI with **Upload Agent Definition**, or call `POST /api/v1/workflow/create/definition` with `{name, workflow_definition}`.
5. **Workflow settings.** `workflow_configurations` is not part of the upload. Apply it with `PUT /api/v1/workflow/{id}` and `{"workflow_configurations": ...}` (`api/routes/workflow.py:325-332`), or set it in the UI.
6. **Inbound number.** Route the DID to this workflow under Telephony → `buzz-asterisk` → phone numbers (`/api/v1/organizations/telephony-configs/{id}/phone-numbers`).
7. **Re-export.** Export from `GET /api/v1/workflow/fetch/{id}`, keep `name`, `workflow_definition` and `workflow_configurations`, then lint again.

## Verify on staging

- The upload is accepted with `tool:` placeholders still present, or it needs the real UUIDs first (`tool_name_validation.py:52` looks the tools up).
- `credential_uuid` on an `http_api` tool must exist at creation time, or creation fails.
- `{{initial_context.caller_number}}` resolves on ARI inbound calls (`ari_manager.py:988-994`). It's `phone_number` on outbound calls, so an outbound agent needs its own opt_out tool. `{{gathered_context.call_id}}` is the ARI channel id.
- The transfer tool works on ARI (the enum comment says "Twilio only", `api/enums.py`). A destination with a channel technology is dialled as written (`ari/dial_string.py:31-41`, `ari/provider.py:509`).
- **The Start greeting can be interrupted.** While a greeting is pending, the caller is never muted, and two spoken words interrupt it, whatever `allow_interrupt` says (`pipecat_engine.py:1595-1596`, `pipecat/greeting.py:62-71`). The legal disclosure must therefore be played by Asterisk (`Playback()` before `Stasis()`), as PLAN section 7 (R8) proposes. The text greeting here repeats it as a courtesy.
- QA with `qa_use_workflow_llm=true` produces valid JSON on Qwen2.5-7B.
- The opt-out flow works end to end: the tool returns 201, the agent confirms, and `end_call` hangs up.
