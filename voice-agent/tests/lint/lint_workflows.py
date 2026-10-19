#!/usr/bin/env python3
"""CI linter for Buzz agent workflow exports (Dograh v1.49.0).

Input format (one agent per folder):

    agents/<agent>/workflow*.json   {"name", "workflow_definition", "workflow_configurations"?}
    agents/<agent>/tools/*.json     one Dograh tool each, CreateToolRequest shape, optional "tool_uuid"

``name`` + ``workflow_definition`` is exactly what the UI's "Download Workflow"
writes and "Upload Agent Definition" reads (ui/src/app/workflow/[workflowId]/
components/WorkflowEditorHeader.tsx:141-158, ui/src/components/workflow/
UploadWorkflowButton.tsx:23-41; the upload requires nodes, edges and viewport).
``workflow_configurations`` is NOT in the UI export: voicemail detection and the
model-configuration override live there (WorkflowResponse, api/routes/workflow.py:
265-278; UpdateWorkflowRequest :325-332). Take it from GET /api/v1/workflow/fetch/{id}.
Tools are not embedded either: nodes hold ``tool_uuids`` (api/services/workflow/
dto.py:164-171), so the tool definitions are kept beside the workflow. A
``tool_uuids`` entry may be a real UUID (matched against a tool file's
``tool_uuid``) or the placeholder ``tool:<file stem>``.

Schema facts relied on (all api/services/workflow/dto.py unless noted):
- node ``type`` values: startCall, agentNode, endCall, globalNode, trigger,
  webhook, qa (:26-33, :1132-1140); integration packages may add more.
- one startCall exactly, at most one globalNode (:208-213, :590-596).
- prompt required on startCall/agentNode/endCall/globalNode (:1036-1041, :1063-1065).
- extraction: ``extraction_enabled``, ``extraction_prompt``,
  ``extraction_variables`` [{name, type, prompt}] (:54-81, :137-161).
- tool refs ``tool_uuids`` on startCall and agentNode only; GlobalNodeData has
  no tool field (:625-626) and unknown data keys are stripped on save
  (:1144-1185), so a "Global-node tool" cannot exist in 1.49.0.
- greeting on startCall: ``greeting_type`` text|audio, ``greeting``,
  ``greeting_recording_id`` (:341-354).
- ``pre_call_fetch_url`` (startCall, :362), webhook ``endpoint_url`` (:798).
- QA node: ``qa_use_workflow_llm`` (default true), ``qa_provider``,
  ``qa_endpoint`` (:932-944).
- voicemail: workflow_configurations.voicemail_detection.use_workflow_llm,
  default true; false makes Dograh build ``provider`` (default "openai") with
  ``model``/``api_key`` (api/services/pipecat/run_pipeline.py:151-163,
  ui/src/types/workflow-configurations.ts:70-83).
- tools: ``definition.type`` http_api|end_call|transfer_call|transfer_agent|
  calculator|mcp; http_api ``config.url``, ``headers``, ``credential_uuid``
  (api/schemas/tool.py:30-39, :96-187, :544-646); transfer resolver
  ``config.resolver.url`` (:218-259); mcp ``config.url`` (:483-541). The LLM
  function name is the tool name lower-cased with non [a-z0-9_] runs turned to
  "_" (api/services/workflow/tools/custom_tool.py:41-44).

Exit code 1 if any error (or any warning with --strict).
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator
from urllib.parse import urlparse

try:
    import yaml  # type: ignore
except ImportError:  # pragma: no cover - PyYAML is in requirements.txt
    yaml = None

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = Path(__file__).with_name("lint_config.yaml")

CORE_NODE_TYPES = {"startCall", "agentNode", "endCall", "globalNode", "trigger", "webhook", "qa"}
PROMPTED_NODE_TYPES = {"startCall", "agentNode", "endCall", "globalNode"}
TOOL_NODE_TYPES = {"startCall", "agentNode"}
TOOL_TYPES = {"http_api", "end_call", "transfer_call", "transfer_agent", "calculator", "mcp"}

DEFAULTS: dict[str, Any] = {
    # Hosts HTTP tools, webhooks, pre-call fetch and MCP may call.
    "allowed_tool_hosts": ["suppression", "n8n"],
    # Hosts a model provider base_url / endpoint may point at.
    "allowed_model_hosts": ["${INFERENCE_HOST}"],
    # Providers allowed without an explicit base_url (none: every provider must be ours).
    "local_providers": ["speaches"],
    "allowed_connection_uuids": [],
    "required_tool_function": "opt_out",
    # Extraction variables the post-call webhook template needs
    # (integrations/webhook-schemas/dograh-payload-template.json, INTERFACES.md).
    "required_extraction_variables": {"call_summary": "string", "opt_out_requested": "boolean"},
    "disclosure_pattern": r"\b(AI|artificial intelligence|automated|virtual assistant|recorded)\b",
    "pii_patterns": [
        r"\bpins?\b", r"pin[_ ]?(code|number)", r"pass(word|code|phrase)", r"\bcvv2?\b", r"\bcvc\b",
        r"security[_ ]?code", r"card[_ ]?(number|no|num|details)", r"\b(credit|debit|bank)[_ ]?card",
        r"\bpan\b", r"expiry|expiration", r"\bnrc\b", r"national[_ ]?(id|identity|registration)",
        r"\bid[_ ]?(number|no)\b", r"passport", r"\botp\b", r"one[_ -]?time[_ ]?(code|password)",
        r"account[_ ]?(number|no)", r"\bsort[_ ]?code\b", r"\biban\b", r"mobile[_ ]?money[_ ]?pin",
        r"\bssn\b", r"social[_ ]?security",
    ],
}


@dataclass
class Finding:
    severity: str  # "error" | "warning"
    rule: str
    message: str
    where: str = ""

    def format(self, path: str) -> str:
        loc = f" [{self.where}]" if self.where else ""
        return f"{path}: {self.severity.upper()} {self.rule}{loc}: {self.message}"


@dataclass
class Config:
    allowed_tool_hosts: set[str]
    allowed_model_hosts: set[str]
    local_providers: set[str]
    allowed_connection_uuids: set[str]
    required_tool_function: str
    required_extraction_variables: dict[str, str]
    disclosure_re: re.Pattern[str]
    pii_res: list[re.Pattern[str]] = field(default_factory=list)

    @classmethod
    def load(cls, path: Path | None = None, overrides: dict[str, Any] | None = None,
             environ: dict[str, str] | None = None) -> "Config":
        raw = dict(DEFAULTS)
        if path is not None and path.is_file():
            if yaml is None:
                raise RuntimeError("PyYAML is required to read the lint config")
            raw.update(yaml.safe_load(path.read_text(encoding="utf-8")) or {})
        raw.update(overrides or {})
        env = os.environ if environ is None else environ

        def hosts(values: Iterable[str]) -> set[str]:
            out = set()
            for value in values:
                expanded = re.sub(r"\$\{(\w+)\}", lambda m: env.get(m.group(1), ""), str(value)).strip().lower()
                if expanded:
                    out.add(expanded)
            return out

        return cls(
            allowed_tool_hosts=hosts(raw["allowed_tool_hosts"]),
            allowed_model_hosts=hosts(raw["allowed_model_hosts"]),
            local_providers={p.lower() for p in raw["local_providers"]},
            allowed_connection_uuids={str(u) for u in raw["allowed_connection_uuids"]},
            required_tool_function=raw["required_tool_function"],
            required_extraction_variables=dict(raw["required_extraction_variables"] or {}),
            disclosure_re=re.compile(raw["disclosure_pattern"], re.IGNORECASE),
            pii_res=[re.compile(p, re.IGNORECASE) for p in raw["pii_patterns"]],
        )


def tool_function_name(name: str) -> str:
    """Mirror of custom_tool_function_name (api/services/workflow/tools/custom_tool.py:41-44)."""
    return re.sub(r"_+", "_", re.sub(r"[^a-z0-9_]", "_", name.lower())).strip("_")


def host_of(url: Any) -> str | None:
    if not isinstance(url, str) or not url.strip():
        return None
    parsed = urlparse(url.strip())
    if parsed.scheme not in {"http", "https", "ws", "wss"}:
        return None
    return (parsed.hostname or "").lower() or None


def host_allowed(host: str | None, allowed: set[str]) -> bool:
    return bool(host) and any(fnmatch.fnmatch(host, pattern) for pattern in allowed)


def walk(node: Any, path: str = "") -> Iterator[tuple[str, Any, Any]]:
    """Yield (path, key, value) for every dict entry, depth first."""
    if isinstance(node, dict):
        for key, value in node.items():
            here = f"{path}.{key}" if path else str(key)
            yield here, key, value
            yield from walk(value, here)
    elif isinstance(node, list):
        for i, item in enumerate(node):
            yield from walk(item, f"{path}[{i}]")


def walk_dicts(node: Any, path: str = "") -> Iterator[tuple[str, dict]]:
    if isinstance(node, dict):
        yield path, node
        for key, value in node.items():
            yield from walk_dicts(value, f"{path}.{key}" if path else str(key))
    elif isinstance(node, list):
        for i, item in enumerate(node):
            yield from walk_dicts(item, f"{path}[{i}]")


# --------------------------------------------------------------------------
# Tools
# --------------------------------------------------------------------------


def load_tools(agent_dir: Path) -> tuple[dict[str, dict], list[tuple[str, Finding]]]:
    """Map both 'tool:<stem>' and real tool_uuid to the tool document."""
    tools: dict[str, dict] = {}
    problems: list[tuple[str, Finding]] = []
    tools_dir = agent_dir / "tools"
    if not tools_dir.is_dir():
        return tools, problems
    for path in sorted(tools_dir.glob("*.json")):
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            problems.append((str(path), Finding("error", "S100", f"invalid JSON: {exc}")))
            continue
        doc["_file"] = str(path)
        tools[f"tool:{path.stem}"] = doc
        if doc.get("tool_uuid"):
            tools[str(doc["tool_uuid"])] = doc
    return tools, problems


def lint_tool(doc: dict, cfg: Config) -> list[Finding]:
    findings: list[Finding] = []
    name = doc.get("name") or ""
    definition = doc.get("definition") or {}
    ttype = definition.get("type")
    where = f"tool {name or doc.get('_file')}"
    if not name:
        findings.append(Finding("error", "S101", "tool has no name", where))
    if ttype not in TOOL_TYPES:
        findings.append(Finding("error", "S102", f"unknown tool definition.type {ttype!r}", where))
        return findings
    if doc.get("category") and doc["category"] != ttype:
        findings.append(Finding("error", "S103", "category must match definition.type (api/schemas/tool.py:700-708)", where))
    config = definition.get("config") or {}
    urls: list[tuple[str, Any]] = []
    if ttype == "http_api":
        urls.append(("config.url", config.get("url")))
        if not config.get("url"):
            findings.append(Finding("error", "T001", "http_api tool has no config.url", where))
        for header, value in (config.get("headers") or {}).items():
            if re.search(r"key|token|auth|secret|password", header, re.IGNORECASE) and value and "{{" not in str(value):
                findings.append(Finding("error", "T003", f"secret-looking header {header!r} is hard-coded; "
                                        "use a credential (credential_uuid) instead", where))
    elif ttype == "transfer_call":
        resolver = config.get("resolver") or {}
        if resolver.get("url"):
            urls.append(("config.resolver.url", resolver.get("url")))
    elif ttype == "mcp":
        urls.append(("config.url", config.get("url")))
    for label, url in urls:
        if url is None:
            continue
        host = host_of(url)
        if "{{" in urlparse(str(url)).netloc:
            findings.append(Finding("error", "T002", f"{label} host is templated ({url}); hosts must be fixed", where))
        elif not host_allowed(host, cfg.allowed_tool_hosts):
            findings.append(Finding("error", "T002", f"{label} {url!r} is outside the allowed hosts "
                                    f"{sorted(cfg.allowed_tool_hosts)}", where))
    return findings


# --------------------------------------------------------------------------
# Workflow
# --------------------------------------------------------------------------


def lint_workflow(doc: Any, tools: dict[str, dict], cfg: Config) -> list[Finding]:
    f: list[Finding] = []
    if not isinstance(doc, dict) or not isinstance(doc.get("workflow_definition"), dict):
        return [Finding("error", "S001", "top level must be an object with 'workflow_definition' (UI export format)")]
    if not doc.get("name"):
        f.append(Finding("warning", "S002", "no 'name'; the UI will invent one on upload"))
    wd = doc["workflow_definition"]
    for key in ("nodes", "edges", "viewport"):
        if key not in wd:
            f.append(Finding("error", "S003", f"workflow_definition.{key} missing (UI upload requires nodes, edges, viewport)"))
    nodes = wd.get("nodes") or []
    edges = wd.get("edges") or []
    if not isinstance(nodes, list) or not isinstance(edges, list):
        return f + [Finding("error", "S003", "nodes and edges must be lists")]

    by_type: dict[str, list[dict]] = {}
    ids: set[str] = set()
    for i, node in enumerate(nodes):
        if not isinstance(node, dict) or not isinstance(node.get("data"), dict):
            f.append(Finding("error", "S004", "node without object 'data'", f"nodes[{i}]"))
            continue
        nid = str(node.get("id", f"#{i}"))
        if nid in ids:
            f.append(Finding("error", "S005", f"duplicate node id {nid!r}"))
        ids.add(nid)
        ntype = node.get("type")
        if "position" not in node:
            f.append(Finding("error", "S006", "node has no position", nid))
        if ntype not in CORE_NODE_TYPES:
            f.append(Finding("warning", "S007", f"node type {ntype!r} is not a core Dograh type; review by hand", nid))
        by_type.setdefault(ntype, []).append(node)
        data = node["data"]
        if not data.get("name"):
            f.append(Finding("error", "S008", "node data.name is required", nid))
        if ntype in PROMPTED_NODE_TYPES and not str(data.get("prompt") or "").strip():
            f.append(Finding("error", "S009", f"{ntype} needs a non-empty prompt", nid))
    for i, edge in enumerate(edges):
        if not isinstance(edge, dict):
            f.append(Finding("error", "S010", "edge is not an object", f"edges[{i}]"))
            continue
        for end in ("source", "target"):
            if edge.get(end) not in ids:
                f.append(Finding("error", "S010", f"edge {end} {edge.get(end)!r} is not a node", str(edge.get("id", i))))
        data = edge.get("data") or {}
        if not str(data.get("label") or "").strip() or not str(data.get("condition") or "").strip():
            f.append(Finding("error", "S011", "edge data needs label and condition", str(edge.get("id", i))))

    # Graph constraints from each NodeSpec (dto.py:208-213 startCall, :432 agentNode,
    # :504 endCall, :590-596 globalNode, :661-665 trigger, :725-727 webhook, :836-838 qa).
    type_of = {str(n.get("id")): n.get("type") for n in nodes if isinstance(n, dict)}
    no_in = {"startCall", "globalNode", "trigger", "webhook", "qa"}
    no_out = {"endCall", "globalNode", "webhook", "qa"}
    incoming: dict[str, int] = {}
    for edge in edges:
        if not isinstance(edge, dict):
            continue
        src, dst = type_of.get(str(edge.get("source"))), type_of.get(str(edge.get("target")))
        incoming[str(edge.get("target"))] = incoming.get(str(edge.get("target")), 0) + 1
        if dst in no_in:
            f.append(Finding("error", "S014", f"edges may not enter a {dst} node", str(edge.get("id"))))
        if src in no_out:
            f.append(Finding("error", "S014", f"edges may not leave a {src} node", str(edge.get("id"))))
    for node in by_type.get("agentNode", []) + by_type.get("endCall", []):
        if not incoming.get(str(node.get("id"))):
            f.append(Finding("error", "S015", f"{node.get('type')} is unreachable (needs an incoming edge)", str(node.get("id"))))

    starts = by_type.get("startCall", [])
    if len(starts) != 1:
        f.append(Finding("error", "S012", f"exactly one startCall node required, found {len(starts)}"))
    if len(by_type.get("globalNode", [])) > 1:
        f.append(Finding("error", "S013", "at most one globalNode allowed"))

    f += check_providers(doc, cfg)
    f += check_qa(by_type.get("qa", []))
    f += check_voicemail_and_overrides(doc, cfg)
    f += check_extraction(nodes, cfg)
    f += check_required_variables(nodes, cfg)
    f += check_tools_and_urls(nodes, tools, cfg)
    f += check_global_and_opt_out(by_type, tools, cfg)
    f += check_disclosure(starts, cfg)
    return f


def check_providers(doc: dict, cfg: Config) -> list[Finding]:
    """P001: no 'dograh' provider anywhere. P002: any provider block must point at our hosts."""
    f: list[Finding] = []
    for path, key, value in walk(doc):
        if isinstance(key, str) and key.endswith("provider") and isinstance(value, str) and value.strip().lower() == "dograh":
            f.append(Finding("error", "P001", "provider 'dograh' sends traffic to Dograh cloud", path))
    for path, obj in walk_dicts(doc):
        provider = None
        for key in ("provider", "qa_provider"):
            if isinstance(obj.get(key), str):
                provider = obj[key].strip().lower()
                break
        if not provider or provider == "dograh":
            continue
        if path.endswith("config") and provider == "ari":
            continue  # telephony config, not a model provider
        if obj.get("qa_use_workflow_llm") is True:
            continue  # QA provider fields are ignored while using the workflow LLM
        if obj.get("use_workflow_llm") is True:
            continue  # voicemail provider fields ignored likewise
        urls = [obj.get(k) for k in ("base_url", "endpoint", "qa_endpoint") if obj.get(k)]
        if not urls:
            if provider not in cfg.local_providers:
                f.append(Finding("error", "P002", f"provider {provider!r} without our base_url falls back to the vendor's "
                                 "cloud endpoint", path or "<root>"))
            continue
        for url in urls:
            if not host_allowed(host_of(url), cfg.allowed_model_hosts):
                f.append(Finding("error", "P002", f"provider {provider!r} url {url!r} is not an allowed model host "
                                 f"{sorted(cfg.allowed_model_hosts)}", path or "<root>"))
    return f


def check_qa(qa_nodes: list[dict]) -> list[Finding]:
    f: list[Finding] = []
    for node in qa_nodes:
        data = node["data"]
        value = data.get("qa_use_workflow_llm")
        if value is False:
            f.append(Finding("error", "Q001", "QA node must use the workflow's LLM (qa_use_workflow_llm=true)", node.get("id", "")))
        elif value is None:
            f.append(Finding("warning", "Q002", "qa_use_workflow_llm not set explicitly (defaults to true in 1.49.0, "
                             "dto.py:934); set it to true", node.get("id", "")))
        if data.get("qa_api_key"):
            f.append(Finding("error", "Q003", "qa_api_key must not be stored in the export", node.get("id", "")))
    return f


def check_voicemail_and_overrides(doc: dict, cfg: Config) -> list[Finding]:
    f: list[Finding] = []
    wc = doc.get("workflow_configurations")
    if wc is None:
        return [Finding("warning", "V002", "no workflow_configurations: voicemail and model override cannot be verified; "
                        "export them from GET /api/v1/workflow/fetch/{id}")]
    vm = wc.get("voicemail_detection")
    if isinstance(vm, dict):
        if vm.get("use_workflow_llm") is False:
            f.append(Finding("error", "V001", "voicemail_detection.use_workflow_llm is false: Dograh would call "
                             f"{vm.get('provider', 'openai')!r} (run_pipeline.py:151-163)", "workflow_configurations.voicemail_detection"))
        if vm.get("api_key"):
            f.append(Finding("error", "V003", "voicemail api_key must not be stored", "workflow_configurations.voicemail_detection"))
    override = wc.get("model_configuration_override")
    if isinstance(override, dict):
        uuids = sorted({str(v) for _, k, v in walk(override) if k in ("provider_connection_uuid", "model_configuration_uuid") and v})
        for u in uuids:
            if not cfg.allowed_connection_uuids:
                f.append(Finding("warning", "M001", f"model override references {u}; set allowed_connection_uuids to verify it",
                                 "workflow_configurations.model_configuration_override"))
            elif u not in cfg.allowed_connection_uuids:
                f.append(Finding("error", "M001", f"model override references {u}, not one of our connections/configurations",
                                 "workflow_configurations.model_configuration_override"))
    return f


def check_extraction(nodes: list[dict], cfg: Config) -> list[Finding]:
    f: list[Finding] = []
    for node in nodes:
        data = node.get("data") if isinstance(node, dict) else None
        if not isinstance(data, dict):
            continue
        for i, var in enumerate(data.get("extraction_variables") or []):
            if not isinstance(var, dict):
                continue
            text = " ".join(str(var.get(k) or "") for k in ("name", "prompt", "description")).replace("_", " ")
            raw = " ".join(str(var.get(k) or "") for k in ("name", "prompt", "description"))
            for pattern in cfg.pii_res:
                if pattern.search(text) or pattern.search(raw):
                    f.append(Finding("error", "X001", f"extraction variable {var.get('name')!r} looks like a PIN, card, ID or "
                                     f"password (matched /{pattern.pattern}/)", f"{node.get('id')}.extraction_variables[{i}]"))
                    break
        prompt = str(data.get("extraction_prompt") or "")
        for pattern in cfg.pii_res:
            if prompt and pattern.search(prompt.replace("_", " ")):
                f.append(Finding("warning", "X002", f"extraction_prompt mentions /{pattern.pattern}/; make sure it forbids, "
                                 "not requests, that data", str(node.get("id"))))
                break
    return f


def check_required_variables(nodes: list[dict], cfg: Config) -> list[Finding]:
    """E001/E002: variables the post-call webhook depends on must be extracted somewhere."""
    f: list[Finding] = []
    found: dict[str, set[str]] = {}
    for node in nodes:
        data = node.get("data") if isinstance(node, dict) else None
        if not isinstance(data, dict) or not data.get("extraction_enabled"):
            continue  # variables only run when extraction_enabled (dto.py:137-161)
        for var in data.get("extraction_variables") or []:
            if isinstance(var, dict) and var.get("name"):
                found.setdefault(var["name"], set()).add(str(var.get("type")))
    for name, vtype in cfg.required_extraction_variables.items():
        if name not in found:
            f.append(Finding("error", "E001", f"required extraction variable {name!r} ({vtype}) is not defined on any "
                             "node with extraction_enabled; the post-call webhook template needs it"))
        elif vtype not in found[name]:
            f.append(Finding("error", "E002", f"extraction variable {name!r} must have type {vtype!r}, "
                             f"found {sorted(found[name])}"))
    return f


def check_tools_and_urls(nodes: list[dict], tools: dict[str, dict], cfg: Config) -> list[Finding]:
    f: list[Finding] = []
    seen_tools: set[int] = set()
    for node in nodes:
        data = node.get("data") if isinstance(node, dict) else None
        if not isinstance(data, dict):
            continue
        nid = str(node.get("id"))
        for ref in data.get("tool_uuids") or []:
            doc = tools.get(str(ref))
            if doc is None:
                f.append(Finding("error", "T010", f"tool {ref!r} has no definition in tools/ (cannot verify its URL)", nid))
                continue
            if id(doc) not in seen_tools:
                seen_tools.add(id(doc))
                f += lint_tool(doc, cfg)
        for key in ("pre_call_fetch_url", "endpoint_url"):
            url = data.get(key)
            enabled = data.get("pre_call_fetch_mode", "disabled") != "disabled" if key == "pre_call_fetch_url" else True
            if url and enabled and not host_allowed(host_of(url), cfg.allowed_tool_hosts):
                f.append(Finding("error", "T002", f"{key} {url!r} is outside the allowed hosts", nid))
        for header in data.get("custom_headers") or []:
            if isinstance(header, dict) and re.search(r"key|token|auth|secret", str(header.get("key")), re.IGNORECASE) \
                    and "{{" not in str(header.get("value")):
                f.append(Finding("error", "T003", f"webhook header {header.get('key')!r} is hard-coded; use a credential", nid))
    return f


def check_global_and_opt_out(by_type: dict[str, list[dict]], tools: dict[str, dict], cfg: Config) -> list[Finding]:
    f: list[Finding] = []
    globals_ = by_type.get("globalNode", [])
    if not globals_:
        f.append(Finding("error", "G001", "no globalNode: the shared compliance rules (no PINs/cards/IDs, opt-out, "
                         "transfer) must live in the Global prompt"))
    for g in globals_:
        if g["data"].get("tool_uuids"):
            f.append(Finding("warning", "G002", "globalNode.tool_uuids is ignored and stripped by Dograh 1.49.0 "
                             "(GlobalNodeData has no tools, dto.py:625-626)", str(g.get("id"))))
        if not re.search(r"opt[ -_]?out", str(g["data"].get("prompt") or ""), re.IGNORECASE):
            f.append(Finding("error", "G003", "Global prompt must tell the agent how to handle opt-out requests", str(g.get("id"))))
    want = cfg.required_tool_function
    for ntype in sorted(TOOL_NODE_TYPES):
        for node in by_type.get(ntype, []):
            names = {tool_function_name(tools[r].get("name", "")) for r in node["data"].get("tool_uuids") or [] if r in tools}
            if want not in names:
                f.append(Finding("error", "O001", f"{ntype} lacks the {want!r} tool; in 1.49.0 tools attach per node, so every "
                                 "startCall/agentNode must carry it", str(node.get("id"))))
    return f


def check_disclosure(starts: list[dict], cfg: Config) -> list[Finding]:
    f: list[Finding] = []
    for node in starts:
        data, nid = node["data"], str(node.get("id"))
        gtype = data.get("greeting_type") or "text"
        if gtype == "audio":
            if not data.get("greeting_recording_id"):
                f.append(Finding("error", "D001", "audio greeting selected but greeting_recording_id is empty", nid))
        elif not str(data.get("greeting") or "").strip():
            f.append(Finding("error", "D001", "startCall has no greeting: the call must open with the AI disclosure", nid))
        elif not cfg.disclosure_re.search(str(data["greeting"])):
            f.append(Finding("error", "D002", "greeting does not disclose that the caller is speaking to an AI/automated "
                             "assistant", nid))
    return f


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def discover(paths: list[str]) -> list[Path]:
    if not paths:
        return sorted((REPO_ROOT / "agents").glob("**/workflow*.json"))
    out: list[Path] = []
    for p in map(Path, paths):
        out += sorted(p.glob("**/workflow*.json")) if p.is_dir() else [p]
    return out


def lint_file(path: Path, cfg: Config) -> list[Finding]:
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return [Finding("error", "S000", f"invalid JSON: {exc}")]
    tools, problems = load_tools(path.parent)
    return [p for _, p in problems] + lint_workflow(doc, tools, cfg)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Lint Dograh workflow exports for Buzz compliance rules.")
    parser.add_argument("paths", nargs="*", help="workflow JSON files or folders (default: agents/**/workflow*.json)")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--strict", action="store_true", help="treat warnings as errors")
    args = parser.parse_args(argv)
    cfg = Config.load(args.config)
    files = discover(args.paths)
    if not files:
        print("no workflow files found")
        return 0
    errors = warnings = 0
    for path in files:
        findings = lint_file(path, cfg)
        for finding in findings:
            print(finding.format(str(path)))
        errors += sum(1 for x in findings if x.severity == "error")
        warnings += sum(1 for x in findings if x.severity == "warning")
    print(f"{len(files)} file(s), {errors} error(s), {warnings} warning(s)")
    return 1 if errors or (args.strict and warnings) else 0


if __name__ == "__main__":
    sys.exit(main())
