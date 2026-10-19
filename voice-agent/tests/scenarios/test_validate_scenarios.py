"""Tests for the scenario format validator, plus a check of the committed suites."""

from __future__ import annotations

import copy
import sys
from pathlib import Path

import pytest
import yaml

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import validate_scenarios as vs  # noqa: E402

GOOD = {
    "version": 1,
    "agent": "front-desk",
    "defaults": {"channel": "text", "severity": "major"},
    "scenarios": [{"id": "FD-001", "title": "t", "turns": [{"say": "hi"}], "expect": {"say_any": ["(?i)hello"]}}],
}


def mutate(fn):
    doc = copy.deepcopy(GOOD)
    fn(doc)
    return doc


def test_good_minimal():
    assert vs.validate(GOOD) == []


@pytest.mark.parametrize("fn,needle", [
    (lambda d: d.update(version=2), "version"),
    (lambda d: d["scenarios"][0].update(id="fd1"), "id must"),
    (lambda d: d["scenarios"].append(copy.deepcopy(d["scenarios"][0])), "duplicate"),
    (lambda d: d["scenarios"][0].update(channel="sms"), "channel"),
    (lambda d: d["scenarios"][0].update(audio={"snr_db": 5}), "audio settings"),
    (lambda d: d["scenarios"][0].update(turns=[{"say": "a", "silence_s": 2}]), "exactly one"),
    (lambda d: d["scenarios"][0].update(expect={}), "non-empty"),
    (lambda d: d["scenarios"][0]["expect"].update(say_any=["("]), "bad regex"),
    (lambda d: d["scenarios"][0]["expect"].update(sounds_nice=True), "unknown check"),
    (lambda d: d["scenarios"][0]["expect"].update(ends_call="yes"), "true or false"),
    (lambda d: d["scenarios"][0].update(judge={"min_score": 4}), "rubric"),
    (lambda d: d["scenarios"][0].update(severity="critical"), "severity"),
    (lambda d: d["scenarios"][0].update(colour="red"), "unknown keys"),
])
def test_bad(fn, needle):
    errors = vs.validate(mutate(fn))
    assert any(needle in e for e in errors), errors


def test_committed_suites_are_valid():
    files = sorted(HERE.glob("*.yaml"))
    assert files
    for path in files:
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert vs.validate(doc, HERE.parents[1]) == [], path
        assert len(doc["scenarios"]) >= 15
