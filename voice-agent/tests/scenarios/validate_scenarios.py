#!/usr/bin/env python3
"""Validate acceptance-scenario YAML files (PLAN R2: the 50-scenario suite).

File format (``tests/scenarios/<agent>.yaml``)::

    version: 1
    agent: front-desk                 # folder under agents/
    defaults:                         # merged into every scenario
      channel: text
      initial_context: {caller_number: "+260970000001", direction: inbound}
      max_words_per_turn: 45
    scenarios:
      - id: FD-001                    # unique, <PREFIX>-<3 digits>
        title: Opening hours
        tags: [faq]
        channel: text | voice         # text: Dograh text-chat API; voice: audio over SIP
        audio: {noise: street, snr_db: 10, codec: ulaw, accent: zambian-english}   # voice only
        initial_context: {...}        # optional, merged over defaults
        turns:                        # caller side, in order
          - say: "What time do you open on Saturday?"
          - silence_s: 12             # caller stays silent
        expect:                       # every check must pass (machine-checked)
          say_any: [regex, ...]       # some agent turn matches one regex
          say_all: [regex, ...]       # each regex matched by some agent turn
          not_say: [regex, ...]       # no agent turn matches any regex
          tool_called: [opt_out]      # LLM function names
          tool_not_called: [transfer_to_staff]
          ends_call: true | false
          transferred: true | false
          disposition: do_not_call
          extracted: {enquiry_topic: "(?i)cowork"}   # gathered_context regexes
          extracted_absent: [callback_number]
          max_words_per_turn: 45      # overrides default
          max_turns: 6                # agent turns until goal
        judge:                        # optional local LLM-as-judge rubric
          rubric: "..."
          min_score: 4                # of 5
        severity: blocker | major | minor

Pass rule: a scenario passes when every ``expect`` check passes and, if
present, the judge score is >= min_score in at least 2 of 3 runs (temperature
0.3 is not deterministic). The suite gate (PLAN R2): every ``blocker`` passes,
and at least 90 % of all scenarios pass.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any

import yaml

HERE = Path(__file__).resolve().parent
ID_RE = re.compile(r"^[A-Z]{2,5}-\d{3}$")
TURN_KEYS = {"say", "silence_s", "dtmf"}
EXPECT_KEYS = {
    "say_any", "say_all", "not_say", "tool_called", "tool_not_called", "ends_call", "transferred",
    "disposition", "extracted", "extracted_absent", "max_words_per_turn", "max_turns",
}
SCENARIO_KEYS = {"id", "title", "tags", "channel", "audio", "initial_context", "turns", "expect", "judge",
                 "severity", "notes"}
SEVERITIES = {"blocker", "major", "minor"}
CHANNELS = {"text", "voice"}


def _regexes(value: Any, where: str, errors: list[str]) -> None:
    if not isinstance(value, list) or not value:
        errors.append(f"{where}: must be a non-empty list of regexes")
        return
    for pattern in value:
        try:
            re.compile(str(pattern))
        except re.error as exc:
            errors.append(f"{where}: bad regex {pattern!r}: {exc}")


def validate(doc: Any, repo_root: Path | None = None) -> list[str]:
    errors: list[str] = []
    if not isinstance(doc, dict):
        return ["top level must be a mapping"]
    if doc.get("version") != 1:
        errors.append("version must be 1")
    agent = doc.get("agent")
    if not agent:
        errors.append("agent is required")
    elif repo_root is not None and not (repo_root / "agents" / agent).is_dir():
        errors.append(f"agent folder agents/{agent} does not exist")
    defaults = doc.get("defaults") or {}
    scenarios = doc.get("scenarios")
    if not isinstance(scenarios, list) or not scenarios:
        return errors + ["scenarios must be a non-empty list"]
    seen: set[str] = set()
    for i, raw in enumerate(scenarios):
        where = f"scenarios[{i}]"
        if not isinstance(raw, dict):
            errors.append(f"{where}: must be a mapping")
            continue
        sid = raw.get("id", "")
        where = f"{sid or where}"
        s = {**defaults, **raw}
        unknown = set(raw) - SCENARIO_KEYS
        if unknown:
            errors.append(f"{where}: unknown keys {sorted(unknown)}")
        if not ID_RE.match(str(sid)):
            errors.append(f"{where}: id must look like FD-001")
        if sid in seen:
            errors.append(f"{where}: duplicate id")
        seen.add(sid)
        if not s.get("title"):
            errors.append(f"{where}: title required")
        if s.get("channel") not in CHANNELS:
            errors.append(f"{where}: channel must be one of {sorted(CHANNELS)}")
        if s.get("audio") and s.get("channel") != "voice":
            errors.append(f"{where}: audio settings need channel: voice")
        if s.get("severity") not in SEVERITIES:
            errors.append(f"{where}: severity must be one of {sorted(SEVERITIES)}")
        turns = s.get("turns")
        if not isinstance(turns, list) or not turns:
            errors.append(f"{where}: turns must be a non-empty list")
        else:
            for j, turn in enumerate(turns):
                if not isinstance(turn, dict) or len(turn) != 1 or set(turn) - TURN_KEYS:
                    errors.append(f"{where}.turns[{j}]: exactly one of {sorted(TURN_KEYS)}")
        expect = s.get("expect")
        if not isinstance(expect, dict) or not expect:
            errors.append(f"{where}: expect must be a non-empty mapping")
            continue
        for key in set(expect) - EXPECT_KEYS:
            errors.append(f"{where}.expect: unknown check {key!r}")
        for key in ("say_any", "say_all", "not_say"):
            if key in expect:
                _regexes(expect[key], f"{where}.expect.{key}", errors)
        for key in ("tool_called", "tool_not_called", "extracted_absent"):
            if key in expect and not (isinstance(expect[key], list) and expect[key]):
                errors.append(f"{where}.expect.{key}: must be a non-empty list")
        for key in ("ends_call", "transferred"):
            if key in expect and not isinstance(expect[key], bool):
                errors.append(f"{where}.expect.{key}: must be true or false")
        if "extracted" in expect:
            if not isinstance(expect["extracted"], dict):
                errors.append(f"{where}.expect.extracted: must map variable -> regex")
            else:
                _regexes(list(expect["extracted"].values()), f"{where}.expect.extracted", errors)
        judge = s.get("judge")
        if judge is not None:
            if not isinstance(judge, dict) or not judge.get("rubric"):
                errors.append(f"{where}.judge: rubric required")
            elif not 1 <= int(judge.get("min_score", 4)) <= 5:
                errors.append(f"{where}.judge.min_score: 1..5")
    return errors


def main(argv: list[str] | None = None) -> int:
    paths = [Path(p) for p in (argv or [])] or sorted(HERE.glob("*.yaml"))
    failed = 0
    for path in paths:
        errors = validate(yaml.safe_load(path.read_text(encoding="utf-8")), HERE.parents[1])
        for error in errors:
            print(f"{path}: {error}")
        failed += bool(errors)
        if not errors:
            count = len(yaml.safe_load(path.read_text(encoding="utf-8"))["scenarios"])
            print(f"{path}: OK ({count} scenarios)")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
