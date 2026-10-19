"""Schema tests: examples validate, the real Dograh template renders to a valid
body, and a reference mapping (what the n8n post-call flow does) produces a
valid cleaned record."""

import copy
import json
import re
from collections import Counter
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker

from buzz_redaction import redact

HERE = Path(__file__).resolve().parent.parent
DOGRAH_SCHEMA = json.loads((HERE / "dograh-webhook.schema.json").read_text())
POST_CALL_SCHEMA = json.loads((HERE / "buzz-post-call.schema.json").read_text())
TEMPLATE = json.loads((HERE / "dograh-payload-template.json").read_text())
DOGRAH_EXAMPLE = json.loads((HERE / "examples" / "dograh-webhook.example.json").read_text())
POST_CALL_EXAMPLE = json.loads((HERE / "examples" / "buzz-post-call.example.json").read_text())


def validator(schema):
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema, format_checker=FormatChecker())


def errors(schema, instance):
    return [e.message for e in validator(schema).iter_errors(instance)]


# --- a faithful copy of Dograh's substitution semantics -----------------------
# api/utils/template_renderer.py (v1.49.0): regex at line 14, dotted paths over
# dicts (21-52), fallback filter (198-203), stringification (248-253).
_VAR = re.compile(r"\{\{\s*([^|\s}]+)(?:\s*\|\s*([^:}]+)(?::([^}]+))?)?\s*\}\}")


def _lookup(ctx, path):
    cur = ctx
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def _render_str(s, ctx):
    def repl(m):
        value = _lookup(ctx, m.group(1))
        if (value is None or value == "") and m.group(2):
            value = (m.group(3) if m.group(3) is not None else m.group(2)).strip()
        if value is None:
            return ""
        if isinstance(value, (dict, list)):
            return json.dumps(value)
        return str(value)

    return _VAR.sub(repl, s).replace("\\n", "\n")


def dograh_render(template, ctx):
    if isinstance(template, dict):
        out = {k: dograh_render(v, ctx) for k, v in template.items()}
        out.setdefault("call_disposition", (ctx.get("gathered_context") or {}).get("call_disposition", ""))
        return out
    if isinstance(template, list):
        return [dograh_render(v, ctx) for v in template]
    if isinstance(template, str):
        return _render_str(template, ctx)
    return template


OUTBOUND_CTX = {
    "workflow_run_id": 1842,
    "workflow_run_name": "run-1842",
    "workflow_id": 7,
    "workflow_name": "Buzz outbound: Bootcamp follow-up",
    "campaign_id": 12,
    "call_time": "2026-10-19T09:15:02.481236+00:00",
    "initial_context": {
        "direction": "outbound",
        "called_number": "+260971234567",
        "caller_number": "+260211000000",
        "hubspot_contact_id": "51234",
        "telephony_configuration_id": 1,
    },
    "gathered_context": {
        "call_status": "end_call",
        "call_disposition": "callback_requested",
        "mapped_call_disposition": "callback_requested",
        "call_tags": ["bootcamp", "callback"],
        "call_summary": "Wants a call back.",
        "opt_out_requested": False,
        "extracted_variables": {"call_summary": "Wants a call back.", "opt_out_requested": False},
    },
    "cost_info": {"call_duration_seconds": 143, "llm": {"x|||buzz-llm": {"total_tokens": 900}}},
    "annotations": {},
    "extra": {},
    "recording_url": "https://voice.bongohive.co.zm/api/v1/public/download/workflow/3f1c2a9e-8b7d-4c11-9e2f-0a1b2c3d4e5f/recording",
    "transcript_url": "https://voice.bongohive.co.zm/api/v1/public/download/workflow/3f1c2a9e-8b7d-4c11-9e2f-0a1b2c3d4e5f/transcript",
}

# A call that never connected: almost everything missing (status_processor.py).
FAILED_CTX = {
    "workflow_run_id": 1843,
    "workflow_id": 7,
    "workflow_name": None,
    "campaign_id": None,
    "call_time": "2026-10-19T09:20:00+00:00",
    "initial_context": {"direction": "outbound", "called_number": "+260961234567"},
    "gathered_context": {"call_status": "no-answer", "call_disposition": "no-answer"},
    "cost_info": {"call_duration_seconds": 0},
    "recording_url": None,
    "transcript_url": None,
}


# --- tests -------------------------------------------------------------------------


def test_schemas_are_valid_draft_2020_12():
    validator(DOGRAH_SCHEMA)
    validator(POST_CALL_SCHEMA)


def test_dograh_example_validates():
    assert errors(DOGRAH_SCHEMA, DOGRAH_EXAMPLE) == []


def test_post_call_example_validates():
    assert errors(POST_CALL_SCHEMA, POST_CALL_EXAMPLE) == []


def test_template_keys_match_schema():
    assert set(TEMPLATE) == set(DOGRAH_SCHEMA["required"]) == set(DOGRAH_SCHEMA["properties"])


@pytest.mark.parametrize("ctx", [OUTBOUND_CTX, FAILED_CTX], ids=["connected", "no-answer"])
def test_rendered_template_validates(ctx):
    body = dograh_render(TEMPLATE, ctx)
    assert errors(DOGRAH_SCHEMA, body) == []
    assert all(isinstance(v, str) for v in body.values())  # Dograh stringifies everything


def test_rendering_is_stringly_typed():
    body = dograh_render(TEMPLATE, OUTBOUND_CTX)
    assert body["workflow_run_id"] == "1842"
    assert body["opt_out_requested"] == "False"
    assert json.loads(body["call_tags"]) == ["bootcamp", "callback"]
    assert "recording" not in json.dumps(body)  # data minimisation: no recording URL


def test_typed_values_are_rejected():
    """Guards against someone 'fixing' the schema to integers: Dograh never sends them."""
    bad = {**DOGRAH_EXAMPLE, "workflow_run_id": 1842}
    assert errors(DOGRAH_SCHEMA, bad)


@pytest.mark.parametrize(
    "patch",
    [
        {"consent": {"property": "buzz_voice_call_consent", "value": "unchanged"}, "opt_out": True},
        {"disposition": "no answer"},
        {"contact_e164": "0971234567"},
        {"tags": "bootcamp"},
        {"extra_field": 1},
        {"redactions": {}},
    ],
)
def test_post_call_negative(patch):
    assert errors(POST_CALL_SCHEMA, {**POST_CALL_EXAMPLE, **patch})


# --- reference mapping: what the n8n post-call flow must do -----------------------


def to_post_call(body, delivery_id, transcript_text, contact_e164, suppressed):
    """Reference implementation of the n8n mapping (integrations/n8n/README.md)."""
    counts = Counter()
    summary = redact(body["summary"], counts=counts)
    transcript = redact(transcript_text, counts=counts) if transcript_text else None
    disposition = body["mapped_call_disposition"] or body["call_disposition"] or body["call_status"] or "unknown"
    return {
        "schema": "buzz.post-call.v1",
        "delivery_id": delivery_id,
        "workflow_run_id": int(body["workflow_run_id"]),
        "workflow_name": body["workflow_name"] or None,
        "campaign_id": int(body["campaign_id"]) if body["campaign_id"] else None,
        "direction": body["direction"] or "unknown",
        "contact_e164": contact_e164,
        "hubspot_contact_id": body["hubspot_contact_id"] or None,
        "call_started_at": body["call_time"],
        "duration_seconds": int(body["duration_seconds"] or 0),
        "call_status": body["call_status"],
        "disposition": disposition,
        "tags": json.loads(body["call_tags"]) if body["call_tags"] else [],
        "summary": summary,
        "transcript": transcript,
        "redactions": {**counts, "total": sum(counts.values())},
        "opt_out": suppressed,
        "consent": {"property": "buzz_voice_call_consent", "value": "withdrawn" if suppressed else "unchanged"},
    }


def test_reference_mapping_of_example_matches_post_call_example():
    transcript = (
        "[2026-10-19T09:15:04.010+00:00] assistant: Hello, this is the BongoHive AI assistant. "
        "This call is handled by an automated agent.\n"
        "[2026-10-19T09:15:09.220+00:00] user: Hi, my PIN is 4321, sorry.\n"
    )
    out = to_post_call(
        copy.deepcopy(DOGRAH_EXAMPLE),
        "6b0e7f3a-2d4c-4e8b-9a1f-5c6d7e8f9a0b",
        transcript,
        "+260971234567",
        False,
    )
    assert errors(POST_CALL_SCHEMA, out) == []
    assert out == POST_CALL_EXAMPLE


def test_reference_mapping_of_failed_call_with_opt_out():
    body = dograh_render(TEMPLATE, FAILED_CTX)
    out = to_post_call(body, "00000000-0000-4000-8000-000000000000", None, "+260961234567", True)
    assert errors(POST_CALL_SCHEMA, out) == []
    assert out["transcript"] is None and out["consent"]["value"] == "withdrawn"
