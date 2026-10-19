"""Table-driven tests for lint_workflows.py.

Each case starts from fixtures/valid (derived from the Dograh v1.49.0 schema,
see the module docstring) and applies one mutation; the case names the rule
that must fire. The real agents/**/workflow*.json must lint clean.
"""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path
from typing import Callable

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import lint_workflows as lw  # noqa: E402

FIXTURE = HERE / "fixtures" / "valid"
ENV = {"INFERENCE_HOST": "10.0.0.20"}


def cfg(**overrides) -> lw.Config:
    return lw.Config.load(HERE / "lint_config.yaml", overrides, environ=ENV)


def base() -> tuple[dict, dict]:
    doc = json.loads((FIXTURE / "workflow.json").read_text(encoding="utf-8"))
    tools, problems = lw.load_tools(FIXTURE)
    assert not problems
    return doc, tools


def node(doc: dict, node_id: str) -> dict:
    return next(n for n in doc["workflow_definition"]["nodes"] if n["id"] == node_id)


def opt_out_tool(tools: dict) -> dict:
    return tools["tool:opt_out"]


def set_path(path: str, value) -> Callable[[dict, dict], None]:
    def apply(doc, tools):
        node_id, _, key = path.partition(".")
        node(doc, node_id)["data"][key] = value
    return apply


Mutation = Callable[[dict, dict], None]

CASES: list[tuple[str, Mutation, str]] = [
    # provider rules
    ("dograh qa provider", lambda d, t: node(d, "q")["data"].update(qa_use_workflow_llm=False, qa_provider="dograh"), "P001"),
    ("dograh voicemail provider", lambda d, t: d["workflow_configurations"]["voicemail_detection"].update(
        use_workflow_llm=False, provider="dograh"), "P001"),
    ("dograh deep in override", lambda d, t: d["workflow_configurations"].update(
        legacy={"llm": {"provider": "dograh", "model": "default"}}), "P001"),
    ("openai without base_url", lambda d, t: d["workflow_configurations"].update(
        legacy={"llm": {"provider": "openai", "model": "gpt-4.1"}}), "P002"),
    ("openai with cloud base_url", lambda d, t: d["workflow_configurations"].update(
        legacy={"llm": {"provider": "openai", "base_url": "https://api.openai.com/v1"}}), "P002"),
    ("groq without base_url", lambda d, t: d["workflow_configurations"].update(
        legacy={"stt": {"provider": "groq"}}), "P002"),
    ("speaches on foreign host", lambda d, t: d["workflow_configurations"].update(
        legacy={"tts": {"provider": "speaches", "base_url": "http://evil.example:8001/v1"}}), "P002"),
    # QA
    ("qa not using workflow llm", set_path("q.qa_use_workflow_llm", False), "Q001"),
    ("qa flag missing", lambda d, t: node(d, "q")["data"].pop("qa_use_workflow_llm"), "Q002"),
    ("qa api key stored", set_path("q.qa_api_key", "sk-123"), "Q003"),
    # voicemail and overrides
    ("voicemail own llm", lambda d, t: d["workflow_configurations"]["voicemail_detection"].update(use_workflow_llm=False), "V001"),
    ("no workflow_configurations", lambda d, t: d.pop("workflow_configurations"), "V002"),
    ("unknown override uuid (warn)", lambda d, t: d["workflow_configurations"].update(
        model_configuration_override={"llm": {"provider_connection_uuid": "aaaa"}}), "M001"),
    # extraction PII
    *[(f"pii var {name}", (lambda name, hint: lambda d, t: node(d, "a")["data"]["extraction_variables"].append(
        {"name": name, "type": "string", "prompt": hint}))(name, hint), "X001")
      for name, hint in [
          ("pin", "Mobile money PIN"), ("card_number", "Card"), ("cvv", "3 digits"), ("nrc_number", "NRC"),
          ("national_id", "ID"), ("passport_no", "Passport"), ("password", "Login"), ("otp", "code"),
          ("verification", "The caller's credit card expiry date"), ("caller_secret", "their account password"),
      ]],
    ("missing call_summary", lambda d, t: node(d, "a")["data"].update(extraction_variables=[
        v for v in node(d, "a")["data"]["extraction_variables"] if v["name"] != "call_summary"]), "E001"),
    ("required vars but extraction disabled", set_path("a.extraction_enabled", False), "E001"),
    ("opt_out_requested wrong type", lambda d, t: [v.update(type="string") for v in node(d, "a")["data"]["extraction_variables"]
                                                   if v["name"] == "opt_out_requested"], "E002"),
    ("pii in extraction prompt (warn)", set_path("a.extraction_prompt", "Ask for the PIN"), "X002"),
    # tools and URLs
    ("tool host outside allow-list", lambda d, t: opt_out_tool(t)["definition"]["config"].update(
        url="https://hooks.example.com/opt-out"), "T002"),
    ("tool host templated", lambda d, t: opt_out_tool(t)["definition"]["config"].update(
        url="http://{{initial_context.host}}/x"), "T002"),
    ("tool host loopback", lambda d, t: opt_out_tool(t)["definition"]["config"].update(
        url="http://127.0.0.1:8090/v1/opt-out"), "T002"),
    ("hard-coded api key header", lambda d, t: opt_out_tool(t)["definition"]["config"].update(
        headers={"X-API-Key": "abc123"}), "T003"),
    ("webhook to cloud", lambda d, t: d["workflow_definition"]["nodes"].append(
        {"id": "w", "type": "webhook", "position": {"x": 0, "y": 0},
         "data": {"name": "Hook", "endpoint_url": "https://hooks.zapier.com/x"}}), "T002"),
    ("pre-call fetch to cloud", lambda d, t: node(d, "s")["data"].update(
        pre_call_fetch_mode="inbound", pre_call_fetch_url="https://crm.example.com/lookup"), "T002"),
    ("undefined tool reference", lambda d, t: node(d, "a")["data"]["tool_uuids"].append("tool:missing"), "T010"),
    # global node and opt-out
    ("no global node", lambda d, t: d["workflow_definition"].update(
        nodes=[n for n in d["workflow_definition"]["nodes"] if n["type"] != "globalNode"]), "G001"),
    ("global tools ignored (warn)", set_path("g.tool_uuids", ["tool:opt_out"]), "G002"),
    ("global prompt silent on opt-out", set_path("g.prompt", "Be nice."), "G003"),
    ("agent lacks opt_out", set_path("a.tool_uuids", []), "O001"),
    ("start lacks opt_out", lambda d, t: node(d, "s")["data"].pop("tool_uuids"), "O001"),
    # disclosure
    ("no greeting", lambda d, t: node(d, "s")["data"].pop("greeting"), "D001"),
    ("audio greeting without recording", set_path("s.greeting_type", "audio"), "D001"),
    ("greeting without disclosure", set_path("s.greeting", "Hello, BongoHive here."), "D002"),
    # structure
    ("missing viewport", lambda d, t: d["workflow_definition"].pop("viewport"), "S003"),
    ("two start nodes", lambda d, t: d["workflow_definition"]["nodes"].append(copy.deepcopy(node(d, "s")) | {"id": "s2"}), "S012"),
    ("empty prompt", set_path("a.prompt", "  "), "S009"),
    ("edge to missing node", lambda d, t: d["workflow_definition"]["edges"].append(
        {"id": "x", "source": "a", "target": "nope", "data": {"label": "l", "condition": "c"}}), "S010"),
    ("edge without condition", lambda d, t: d["workflow_definition"]["edges"][0]["data"].update(condition=""), "S011"),
    ("edge out of global", lambda d, t: d["workflow_definition"]["edges"].append(
        {"id": "g-a", "source": "g", "target": "a", "data": {"label": "l", "condition": "c"}}), "S014"),
    ("edge into start", lambda d, t: d["workflow_definition"]["edges"].append(
        {"id": "a-s", "source": "a", "target": "s", "data": {"label": "l", "condition": "c"}}), "S014"),
    ("unreachable end", lambda d, t: d["workflow_definition"].update(
        edges=[e for e in d["workflow_definition"]["edges"] if e["target"] != "e"]), "S015"),
    ("unknown node type (warn)", lambda d, t: d["workflow_definition"]["nodes"].append(
        {"id": "x", "type": "mystery", "position": {"x": 0, "y": 0}, "data": {"name": "X"}}), "S007"),
]


def test_valid_fixture_is_clean():
    doc, tools = base()
    assert lw.lint_workflow(doc, tools, cfg()) == []


@pytest.mark.parametrize("label,mutate,rule", CASES, ids=[c[0] for c in CASES])
def test_rule_fires(label, mutate, rule):
    doc, tools = base()
    mutate(doc, tools)
    findings = lw.lint_workflow(doc, tools, cfg())
    assert rule in {f.rule for f in findings}, [f.format("x") for f in findings]


def test_override_uuid_allowed_when_listed():
    doc, tools = base()
    doc["workflow_configurations"]["model_configuration_override"] = {"llm": {"provider_connection_uuid": "aaaa"}}
    assert lw.lint_workflow(doc, tools, cfg(allowed_connection_uuids=["aaaa"])) == []
    errors = [f for f in lw.lint_workflow(doc, tools, cfg(allowed_connection_uuids=["bbbb"])) if f.severity == "error"]
    assert [f.rule for f in errors] == ["M001"]


def test_qa_provider_ignored_while_using_workflow_llm():
    doc, tools = base()
    node(doc, "q")["data"].update(qa_provider="openai", qa_model="gpt-4o")
    assert lw.lint_workflow(doc, tools, cfg()) == []


def test_model_host_unset_env_means_nothing_allowed():
    doc, tools = base()
    doc["workflow_configurations"]["legacy"] = {"llm": {"provider": "openai", "base_url": "http://10.0.0.20:8000/v1"}}
    assert lw.lint_workflow(doc, tools, cfg()) == []
    no_env = lw.Config.load(HERE / "lint_config.yaml", environ={})
    assert "P002" in {f.rule for f in lw.lint_workflow(doc, tools, no_env)}


def test_tool_function_name_mirrors_dograh():
    assert lw.tool_function_name("Opt Out") == "opt_out"
    assert lw.tool_function_name("opt-out!!") == "opt_out"


def test_cli_exit_codes(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("INFERENCE_HOST", "10.0.0.20")
    assert lw.main([str(FIXTURE / "workflow.json")]) == 0
    bad = tmp_path / "agent"
    (bad / "tools").mkdir(parents=True)
    doc, _ = base()
    node(doc, "q")["data"]["qa_use_workflow_llm"] = False
    (bad / "workflow.json").write_text(json.dumps(doc), encoding="utf-8")
    (bad / "tools" / "opt_out.json").write_text((FIXTURE / "tools" / "opt_out.json").read_text(), encoding="utf-8")
    assert lw.main([str(bad)]) == 1
    assert "Q001" in capsys.readouterr().out


def test_repository_agents_lint_clean(monkeypatch):
    monkeypatch.setenv("INFERENCE_HOST", "10.0.0.20")
    files = lw.discover([])
    assert files, "expected at least agents/front-desk/workflow.json"
    config = lw.Config.load(HERE / "lint_config.yaml")
    for path in files:
        findings = lw.lint_file(path, config)
        assert findings == [], [f.format(str(path)) for f in findings]
