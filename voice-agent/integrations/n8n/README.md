# n8n flows

There are three flows. They are not exported yet: build them in n8n `2.43.6` (PLAN section 2), export them as JSON into this folder, and **remove credentials from the export before committing**.

Where n8n runs is still an open question (PLAN section 9 Q6). The URLs below assume that n8n runs as a container on the app host's `buzz-app` network.
- If it does, it reaches the suppression service at `http://suppression:8080`, and Dograh at `http://api:8000`, with the Dograh service name to be confirmed in `infra/app`.
- If n8n runs anywhere else, the suppression service has to be exposed to it through nginx with TLS and an IP allow-list, which needs a decision and an INTERFACES change.

## Credentials (n8n credential store, never in node parameters)

| Name | Type | Used for |
|---|---|---|
| `buzz-suppression` | Header Auth, `X-API-Key: ${SUPPRESSION_API_KEY}` | every call to the suppression service |
| `buzz-dograh-api` | Header Auth, `X-API-Key: <Dograh API key>` | starting calls |
| `buzz-dograh-webhook` | Header Auth on the Webhook node, `X-API-Key: <N8N_WEBHOOK_KEY>` | authenticates Dograh to n8n. Use a **separate** secret from the suppression key. In Dograh, create a credential of type `api_key` (header `X-API-Key`) and select it on the agent's Webhook node. |
| `buzz-hubspot` | HubSpot private app token | the scopes listed under "HubSpot scopes" below |
| `buzz-sheets` | Google service account | one sheet, shared with the service account only |

## Redaction: use `POST /v1/redact` (recommended)

n8n sends the summary and transcript to the suppression service's `POST /v1/redact`. It does not redact in a Code node. The reasons:
- **One implementation.** The rules live in Python ([`../redaction`](../redaction)), with table-driven tests covering both what must be redacted and what must be left alone. A JavaScript copy in a Code node would drift and has no tests.
- **Metrics.** Redaction counts appear in Prometheus (`suppression_redactions_total`).
- **Same placement.** n8n already holds the suppression key and already calls this service for opt-out checks, so no extra service or secret is needed.

What it costs:
- the transcript makes one extra hop on the internal network;
- the service gains a second responsibility. It is small and stateless: it never logs or stores the body.

Request and response:

```http
POST /v1/redact
X-API-Key: …
Content-Type: application/json

{"value": {"summary": "…", "transcript": "…"}, "phones": false}
```

```json
{"value": {"summary": "…", "transcript": "…"}, "counts": {"pin": 1}, "total": 1}
```

- **Phone numbers:** set `"phones": true` for anything that goes to Google Sheets. For HubSpot, phones can stay `false`, because the contact's number is already on the record (decide with the DPIA).
- **On failure:** if the redact call fails, **stop**. Never forward an unredacted transcript. n8n then returns 5xx and Dograh retries.

## Flow 1: outbound trigger (consent check)

Trigger: manual, or a schedule inside permitted hours. The dialplan also enforces calling hours, so the schedule is a convenience, not the control.

1. **HubSpot, search contacts.**
   - Filter: `buzz_voice_call_consent = granted`, plus the campaign's list or criteria. The property name is a placeholder; agree it with the HubSpot admin (**verify**).
   - Return only `hs_object_id`, `firstname`, `phone`, and the consent property with its timestamp.
2. **Stop if consent is missing.** This is a defensive repeat of the filter: an IF node that drops any contact whose consent is not `granted`.
3. **Check the number.** `GET http://suppression:8080/v1/check?number={{phone}}`.
   - Continue only if the response is HTTP 200 and `allow === true`.
   - Count `reason` values into a run summary.
   - Treat any other status as deny.
4. **Start the call.** `POST http://api:8000/api/v1/public/agent/{agent_uuid}` (`api/routes/public_agent.py`, `TriggerCallRequest`) with:

   ```json
   {"phone_number": "{{e164 from step 3}}",
    "initial_context": {"hubspot_contact_id": "{{hs_object_id}}", "first_name": "{{firstname}}"}}
   ```

   - Dograh merges this into `initial_context`, and our webhook template echoes `hubspot_contact_id` back.
   - Dograh ignores the reserved keys `workflow_run_id`, `call_id` and `provider`.
   - **Do not** put anything else personal into `initial_context`.
5. **Throttle.** Use Wait or Split in Batches so that the concurrency is no higher than `DEFAULT_ORG_CONCURRENCY_LIMIT`.

Asterisk checks the suppression list again just before dialling (`buzz-outbound`). Step 3 only avoids wasted call attempts.

For large campaigns, PLAN section 7 has our export script generate a Dograh campaign CSV from the same consent filter. The same rule applies to it: a contact only goes in after consent and `/v1/check` both pass.

## Flow 2: post-call (redaction to HubSpot and Sheets)

Contracts: [`../webhook-schemas`](../webhook-schemas). The mapping is implemented in Python as `to_post_call()` in `../webhook-schemas/tests/test_schemas.py`. Copy that logic into the Code node, and keep the two in sync.

1. **Webhook node.**
   - `POST /webhook/buzz-post-call`, Header Auth `buzz-dograh-webhook`.
   - **Respond: when the last node finishes.** An error then becomes a 5xx, and Dograh retries up to 5 times (30 s → 600 s). Dograh's timeout is 30 s.
2. **Dedupe.**
   - Keep the `X-Dograh-Delivery-Id` header in an n8n Data Table, or in workflow static data trimmed to the last 30 days.
   - If the ID has been seen, respond 200 and stop.
   - Delivery is at least once.
3. **Check the body.** Verify `body.schema === "buzz.dograh-webhook.v1"`. Otherwise send an alert and respond 400. Dograh does not retry a 400; it dead-letters the delivery.
4. **Fetch the transcript** if `transcript_url` is not empty.
   - HTTP Request, GET, **follow redirects**.
   - It answers 302 to a signed MinIO URL that is valid for 1 hour. n8n must be able to resolve and reach the MinIO host in that URL (**verify** in Phase 2).
   - Response format: text.
5. **Opt-out status.**
   - Contact number: `called_number` if `direction` is `outbound`, otherwise `caller_number`.
   - Call `GET /v1/check?number=…`.
   - `opt_out = (reason === "suppressed")`. The suppression list is the source of truth; `opt_out_requested` is only a hint.
   - If `opt_out_requested === "True"` but the number is not suppressed, call `POST /v1/opt-out` with `{"phone_number", "source": "n8n_post_call", "call_id": workflow_run_id}` and alert. This is a belt-and-braces check in case the agent's tool call failed.
6. **Redact.**
   - `POST /v1/redact` with `{"value": {"summary": body.summary, "transcript": transcriptText}, "phones": false}`.
   - On failure, stop with an error.
7. **Map (Code node).** Build the `buzz.post-call.v1` record:
   - convert the strings to numbers or booleans;
   - `tags` = `JSON.parse(call_tags || "[]")`;
   - `disposition` = `mapped_call_disposition || call_disposition || call_status`;
   - consent `withdrawn` if `opt_out`, otherwise `unchanged`.

   Validate it against `buzz-post-call.schema.json`, either with a JSON-schema check in the Code node or by checking the required fields by hand.
8. **HubSpot.**
   - Create a **Call** engagement (or a Note) on `hubspot_contact_id`, holding the disposition, duration, summary and redacted transcript. If the ID is empty, search by `contact_e164`, and leave the contact unchanged if there is no unique match.
   - If `opt_out`, set `buzz_voice_call_consent = withdrawn` on the contact.
9. **Google Sheets.** Append a row: `delivery_id`, `workflow_run_id`, `call_started_at`, `direction`, `disposition`, `duration_seconds`, `opt_out`, `summary`. No transcript and no phone number, unless the DPIA allows them; if it does, redact with `phones: true`.

## Flow 3: opt-out sync (both directions)

**A. Suppression list to HubSpot** (every 15 minutes):

1. Call `GET /v1/suppressions?since={{last_run_iso}}`. Store `last_run_iso` in static data, and take it as the start time of this run, not its end.
2. For each `e164`, search HubSpot contacts by `phone` and `mobilephone`. HubSpot stores free-text numbers, so search both the `+260…` and `0…` forms.
3. Set `buzz_voice_call_consent = withdrawn` on every match. The update is idempotent.

**B. HubSpot to the suppression list:**

1. Trigger it from either:
   - a HubSpot workflow, when consent is withdrawn or the contact unsubscribes, calling an n8n webhook (Header Auth); or
   - a poll of recently modified contacts with consent `withdrawn`.
2. Call `POST /v1/opt-out` with `{"phone_number": phone, "source": "hubspot"}` for each number on the contact.
3. Ignore a `422` for an unusable number, but log the HubSpot contact ID. Never log the number.

Opt-outs are never removed by any flow. Removal is the manual, audited database operation in [`../suppression-service/README.md`](../suppression-service/README.md).

## The Dograh `opt_out` tool (for the agents builder)

Add an HTTP API tool on the Global node:

| Setting | Value |
|---|---|
| Method | `POST` |
| URL | `http://suppression:8080/v1/opt-out` (Docker network name, not `127.0.0.1`) |
| Credential | a Dograh credential of type `api_key`, with header `X-API-Key` set to `${SUPPRESSION_API_KEY}`. Not a static header. |
| Body template | outbound agents: `{"phone_number": "{{initial_context.called_number}}", "source": "dograh_opt_out", "call_id": "{{initial_context.workflow_run_id}}"}`. Inbound agents: use `caller_number`. |
| Parameters | none |
| Timeout | 5000 ms (the default) |

- **Why a body template:** the LLM never supplies the number, so it cannot get it wrong. Dograh renders the body template with `initial_context` (`api/services/workflow/tools/custom_tool.py`, `render_body_template`). A whole-value placeholder keeps its native type, so `call_id` arrives as an integer; `/v1/opt-out` accepts that and stores it as a string.
- **After the call to the tool:** the prompt says to confirm the opt-out to the caller and end the call. If the tool returns an error, the agent still promises the opt-out. Flow 2 step 5 catches the failure, sees `opt_out_requested`, and retries the opt-out.

## HubSpot scopes

The private app token needs:
- `crm.objects.contacts.read`
- `crm.objects.contacts.write`
- the `crm.objects.calls` read and write scopes, or the notes scopes if you log Notes instead of Calls.

Nothing else.
