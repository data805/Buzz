# Webhook schemas

| File | What |
|---|---|
| `dograh-payload-template.json` | The `payload_template` to paste into the **Webhook** node of every Buzz agent. |
| `dograh-webhook.schema.json` | JSON Schema (2020-12) for the body Dograh v1.49.0 sends when it renders that template. |
| `buzz-post-call.schema.json` | The cleaned, typed, redacted record that n8n writes to HubSpot or Sheets. |
| `examples/*.example.json` | One example of each schema. `tests/` validates both, renders the template the way Dograh does, and checks the reference mapping from one schema to the other. |

## How Dograh v1.49.0 sends webhooks

Paths below are in the Dograh source at tag `dograh-v1.49.0`.

- **Trigger.** After each run, `api/tasks/run_integrations.py` (`run_integrations_post_workflow_run`) runs the QA nodes, then each `webhook` node. This includes calls that never connected (`api/services/telephony/status_processor.py`).
- **The body is a template.** It is `render_template(payload_template, context)`, and Dograh adds `call_disposition` if the template does not set it (`run_integrations.py:426-443`). The context (`run_integrations.py:367-423`) offers:
  - `workflow_run_id`, `workflow_id`, `workflow_name`, `campaign_id`, `call_time` (the run's `created_at` in ISO format);
  - `initial_context`, `gathered_context`, `cost_info` (= `usage_info`), `annotations`, `extra`;
  - `recording_url`, `transcript_url`, `user_recording_url`, `bot_recording_url`.
- **Every substituted value is a string** (`api/utils/template_renderer.py:248-253`):
  - `None` becomes `""`;
  - a dict or list becomes `json.dumps(...)`;
  - a boolean becomes `"True"` or `"False"`;
  - a number becomes its string form.

  The renderer is regex substitution, not Jinja.
- **There is no inline transcript.** `transcript_url` is `{BACKEND_API_ENDPOINT}/api/v1/public/download/workflow/{token}/transcript`, which redirects (302) to a signed storage URL valid for 1 hour (`api/routes/public_download.py`). The text has one line per turn, `[<ISO time>] user: …` or `[<ISO time>] assistant: …` (`api/utils/transcript.py`).
- **Delivery** (`api/tasks/webhook_delivery.py`):
  - POST with a JSON body;
  - headers `Content-Type`, the optional credential header (`api_key` credential type: defaults to `X-API-Key`), `X-Dograh-Delivery-Id`, `X-Dograh-Workflow-Run-Id` and `X-Dograh-Delivery-Attempt`;
  - no signature;
  - up to 5 attempts with backoff from 30 s to 600 s, retrying on 408, 425, 429, 5xx and network errors (`api/constants.py:225-230`). Any other non-2xx status goes to dead-letter.
  - Delivery is at least once, so receivers must dedupe on `X-Dograh-Delivery-Id`.
- **Dispositions are free strings.**
  - Telephony failures use `failed`, `busy`, `no-answer`, `canceled` and `error` (`api/enums.py`).
  - Engine end reasons include `user_hangup`, `end_call` and `voicemail_detected`.
  - User codes must match `^[A-Za-z][A-Za-z0-9_-]{0,63}$` (`docs/developer/call-dispositions.mdx`).
- **Upstream docs.** `docs/developer/webhooks.mdx` is out of date: it says there are no retries.

## Our template choices

- **Data minimisation.** There is no recording URL; audio stays on the app server. We don't send the whole `gathered_context`, only named fields.
- **Agent extraction variables.** Every agent must define two:
  - `call_summary` (string), a short neutral summary that does not repeat numbers;
  - `opt_out_requested` (boolean).

  Agents builder: please add these to the agent checklist.
- **`hubspot_contact_id`.** The n8n trigger flow passes it in `initial_context` when it starts an outbound call. It is `""` for inbound calls.
- **`schema`** is a literal string, so it reaches n8n unchanged and versions the contract.
