# Scenario runner: design note

Status: design only. The runner itself is a Phase 3 deliverable. The format and its validator (`validate_scenarios.py`) are fixed now, so we can write scenarios in parallel.

## Does Dograh have an eval or test-run API?

**There is no scenario or eval API in v1.49.0.** `evals/` in the Dograh repository holds two things:

- `evals/stt/`, a WebSocket STT benchmark for Deepgram and Speechmatics. Both are cloud services, so it's of no use to us.
- `evals/visualizer/`, a Next.js viewer.

The QA node analyses calls *after* they end (`api/services/workflow/dto.py:932-944`). It is useful as a second signal, but it doesn't drive a conversation.

What we **can** use is the **text-chat API**. It runs the real workflow graph, with prompts, edges, tools and extraction, and swaps the voice pipeline for text. All routes are under `/api/v1/workflow` (`api/routes/workflow_text_chat.py:38`):

| Route | Use |
|---|---|
| `POST /{workflow_id}/text-chat/sessions` with `{request_id?, name?, initial_context?, annotations?}` (:41-48, :243) | Start a run. `initial_context` carries `caller_number` and `direction`. |
| `POST /{workflow_id}/text-chat/sessions/{run_id}/messages` with `{text, expected_revision?}` (:51-53, :322) | One caller turn. |
| `POST /{workflow_id}/text-chat/sessions/stream` and `/{run_id}/messages/stream` (:480, :497) | Streaming variants. These also emit `tool_call_started` and `tool_call_result` events (`api/services/workflow/text_chat_runner.py:345-365`). |
| `POST /{workflow_id}/text-chat/sessions/{run_id}/end` (:340) | Close the run. |
| `GET /{workflow_id}/text-chat/sessions/{run_id}` (:260) | Response `WorkflowRunTextSessionResponse`: `state`, `is_completed`, `gathered_context`, `session_data` (turns), `checkpoint` (:65-79). |
| `GET /api/v1/workflow/{workflow_id}/runs/{run_id}` (`api/routes/workflow.py:1446`) | Run record: disposition, tags, QA output. Exact fields **(verify)**. |

Authentication is the same as for `tools/dograh/configure_providers.py`: a bearer token from `POST /api/v1/auth/login`, or `X-API-Key`.

## How the runner works

1. Load `<agent>.yaml` and validate it.
2. Find the workflow ID by name with `GET /api/v1/workflow/fetch`. Run against the **draft** so a change can be tested before it is published **(verify that text chat uses the draft)**.
3. **Text channel.** For each turn, `say` → POST message. `silence_s` can't be simulated in text, so a text scenario must not use it; such scenarios are `channel: voice`. Collect the agent's turns, the tool events and the final `gathered_context`.
4. **Voice channel** (noise, silence, accents):
   - Pre-render caller audio with Speaches TTS, mixed with noise at `snr_db`, as 8 kHz μ-law WAVs, cached under `tests/scenarios/audio/` (git-ignored).
   - A test PJSIP endpoint or Local channel in Asterisk (`buzz-test` context, staging only) plays the WAVs into a call to the agent's DID, waiting for agent silence between turns. It records both legs.
   - The runner then reads the transcript from the run record.
   - **(verify)** that Dograh's transcript, with STT text and tool calls, is exposed by the runs API.
5. **Checks.** Every key in `expect` maps to a deterministic function:
   - regex over agent turns;
   - tool names from the tool events;
   - `ends_call` and `transferred` from the run state or disposition;
   - `extracted` from `gathered_context`;
   - `max_words_per_turn` counted per agent turn.
6. **Judge** (optional). Local vLLM (`buzz-llm`) with a fixed judge prompt and the scenario `rubric`, returning `{"score": 1-5, "reason": "..."}`. Three runs; it passes on 2 of 3. Using our own model keeps everything local. Its bias toward itself is accepted for v1, and staff spot-check 10 %.
7. **Report.** JUnit XML (for CI) plus a Markdown table: id, severity, pass/fail, failed checks, judge score, run IDs.

## Side effects and safety

- Text-chat runs execute the **real** tools. `opt_out` will POST to the suppression service.
  - Run the suite only on staging, against a staging suppression database.
  - Use caller numbers from a reserved test block (the `+260 97 000 00xx` placeholders in the YAML need agreeing).
  - The runner deletes those test suppressions afterwards with the audited manual procedure, which INTERFACES.md requires.
- Whether `transfer_call` and `end_call` behave sensibly in text mode is **(verify)**. If transfer fails in text, assert `tool_called: [transfer_to_staff]` only.
- Each run takes one of the org's concurrency slots (`DEFAULT_ORG_CONCURRENCY_LIMIT`), so run the suite sequentially or at most two at a time.

## Open items (verify)

- The `session_data` turn structure and where tool calls appear in it.
- Whether text chat honours `initial_context.caller_number`, which the `opt_out` preset template needs.
- Run-record fields: disposition, tags, QA result.
- A test-call path into Asterisk that doesn't touch the real trunk.
