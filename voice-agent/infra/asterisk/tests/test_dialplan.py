"""Static checks of the rendered dialplan, without Asterisk.

A small parser for the subset of extensions.conf syntax we use: contexts,
include =>, #include, exten => / same => with numeric, n and n(label)
priorities. It checks that every jump target exists, that every application
and function is built into the image, and the compliance ordering of the
inbound, outbound and transfer paths.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from conftest import ROOT

# --------------------------------------------------------------------------
# Parser
# --------------------------------------------------------------------------


@dataclass
class Step:
    prio: int
    label: str | None
    app: str
    args: str
    lineno: int
    file: str


@dataclass
class Context:
    name: str
    includes: list[str] = field(default_factory=list)
    extens: dict[str, list[Step]] = field(default_factory=dict)


def load_lines(path: Path, seen: set[Path] | None = None):
    seen = seen or set()
    if path in seen:
        raise AssertionError(f"#include loop at {path}")
    seen.add(path)
    for lineno, raw in enumerate(path.read_text().splitlines(), 1):
        line = raw.strip()
        if line.startswith("#include"):
            target = path.parent / line.split(None, 1)[1].strip().strip('"')
            assert target.exists(), f"{path.name}:{lineno}: #include {target.name} missing"
            yield from load_lines(target, seen)
            continue
        yield path.name, lineno, raw


def strip_comment(line: str) -> str:
    # ';' starts a comment unless escaped. The dialplan here never uses '\;'.
    return line.split(";", 1)[0].rstrip()


def split_app(body: str) -> tuple[str, str]:
    m = re.match(r"([A-Za-z_][A-Za-z0-9_]*)\((.*)\)\s*$", body)
    if m:
        return m.group(1), m.group(2)
    return body.strip(), ""


def parse(path: Path) -> dict[str, Context]:
    contexts: dict[str, Context] = {}
    ctx: Context | None = None
    current_exten: str | None = None
    for fname, lineno, raw in load_lines(path):
        line = strip_comment(raw).strip()
        if not line:
            continue
        m = re.match(r"^\[([^\]]+)\]", line)
        if m:
            name = m.group(1)
            ctx = contexts.setdefault(name, Context(name))
            current_exten = None
            continue
        if ctx is None or ctx.name in ("general", "globals"):
            continue
        m = re.match(r"^include\s*=>\s*(\S+)", line)
        if m:
            ctx.includes.append(m.group(1))
            continue
        m = re.match(r"^exten\s*=>\s*([^,]+),([^,]+),(.*)$", line)
        if m:
            exten, prio_s, body = m.group(1).strip(), m.group(2).strip(), m.group(3)
            current_exten = exten
            steps = ctx.extens.setdefault(exten, [])
        else:
            m = re.match(r"^same\s*=>\s*([^,]+),(.*)$", line)
            assert m, f"{fname}:{lineno}: cannot parse: {raw}"
            assert current_exten is not None, f"{fname}:{lineno}: 'same' without 'exten'"
            prio_s, body = m.group(1).strip(), m.group(2)
            steps = ctx.extens[current_exten]
        label = None
        lm = re.match(r"^(\w+)\((\w+)\)$", prio_s)
        if lm:
            prio_s, label = lm.group(1), lm.group(2)
        if prio_s == "n":
            prio = (steps[-1].prio + 1) if steps else 1
        else:
            assert prio_s.isdigit(), f"{fname}:{lineno}: bad priority {prio_s}"
            prio = int(prio_s)
            assert prio == (steps[-1].prio + 1 if steps else 1), f"{fname}:{lineno}: priority gap"
        app, args = split_app(body)
        steps.append(Step(prio, label, app, args, lineno, fname))
    return contexts


def top_split(s: str, sep: str) -> list[str]:
    """Split on sep outside ${...}, $[...], (...) and quotes."""
    out, depth, cur, quote = [], 0, "", False
    for ch in s:
        if ch == '"':
            quote = not quote
        if not quote:
            if ch in "{[(":
                depth += 1
            elif ch in "}])":
                depth -= 1
        if ch == sep and depth == 0 and not quote:
            out.append(cur)
            cur = ""
        else:
            cur += ch
    out.append(cur)
    return out


@pytest.fixture(scope="module")
def dp(rendered: Path) -> dict[str, Context]:
    return parse(rendered / "extensions.conf")


def all_steps(dp):
    for ctx in dp.values():
        for exten, steps in ctx.extens.items():
            for st in steps:
                yield ctx, exten, st


def exten_exists(ctx: Context, exten: str) -> bool:
    return exten in ctx.extens


def resolve(dp, ctx: Context, exten: str, target: str, where: str):
    """Check a Goto-style target [[context,]exten,]priority|label."""
    parts = [p.strip() for p in top_split(target, ",")]
    if any("${" in p for p in parts):
        # Dynamic target: only the context (if literal) can be checked.
        if len(parts) == 3 and "${" not in parts[0]:
            assert parts[0] in dp, f"{where}: unknown context {parts[0]}"
        return
    if len(parts) == 1:
        tctx, texten, tprio = ctx, exten, parts[0]
    elif len(parts) == 2:
        tctx, texten, tprio = ctx, parts[0], parts[1]
    else:
        assert parts[0] in dp, f"{where}: unknown context {parts[0]}"
        tctx, texten, tprio = dp[parts[0]], parts[1], parts[2]
    tprio = re.sub(r"\(.*\)$", "", tprio)  # Gosub arguments
    assert exten_exists(tctx, texten), f"{where}: no extension {texten} in [{tctx.name}]"
    steps = tctx.extens[texten]
    if tprio.isdigit():
        assert any(s.prio == int(tprio) for s in steps), f"{where}: no priority {tprio} at {texten}@{tctx.name}"
    else:
        assert any(s.label == tprio for s in steps), f"{where}: no label {tprio} at {texten}@{tctx.name}"


def branch_targets(args: str) -> list[str]:
    parts = top_split(args, "?")
    assert len(parts) == 2, f"cannot split condition in {args!r}"
    return [t for t in top_split(parts[1], ":") if t.strip()]


# --------------------------------------------------------------------------
# Structural checks
# --------------------------------------------------------------------------

EXPECTED_CONTEXTS = {
    "from-trunk", "buzz-inbound-dids", "buzz-inbound-unknown", "buzz-staff", "buzz-inbound",
    "buzz-stasis", "buzz-outbound", "buzz-sub-hours", "buzz-sub-trunk-number",
    "buzz-sub-callee-disclosure", "buzz-transfer", "buzz-transfer-targets",
    "buzz-sub-transfer-screen", "buzz-take-message", "buzz-sub-message-webhook",
}


def test_expected_contexts_exist(dp):
    missing = EXPECTED_CONTEXTS - set(dp)
    assert not missing, f"missing contexts: {missing}"


def test_includes_resolve(dp):
    for ctx in dp.values():
        for inc in ctx.includes:
            assert inc in dp, f"[{ctx.name}] includes unknown context {inc}"


def test_jump_targets_exist(dp):
    for ctx, exten, st in all_steps(dp):
        where = f"{st.file}:{st.lineno}"
        if st.app in ("Goto", "Gosub"):
            resolve(dp, ctx, exten, st.args, where)
        elif st.app in ("GotoIf", "GotoIfTime"):
            for t in branch_targets(st.args):
                resolve(dp, ctx, exten, t, where)


def test_dial_gosubs_handlers_and_local_targets_exist(dp, rendered: Path):
    text = "\n".join(strip_comment(l) for _, _, l in load_lines(rendered / "extensions.conf"))
    for sub in re.findall(r"\bU\(([a-z0-9-]+)", text):
        assert sub in dp and "s" in dp[sub].extens, f"Dial U({sub}) has no s extension"
    for handler in re.findall(r"hangup_handler_push\)=([^)\s]+)", text):
        ctx_name, exten, prio = handler.split(",")
        assert ctx_name in dp and exten in dp[ctx_name].extens, f"hangup handler {handler} missing"
    for ctx_name in re.findall(r"Local/[^@]+@([a-z0-9-]+)", text):
        assert ctx_name in dp, f"Local channel into unknown context {ctx_name}"


def test_pjsip_contexts_exist(dp, rendered: Path):
    for conf in ("pjsip.conf",):
        for name in re.findall(r"^context\s*=\s*(\S+)", (rendered / conf).read_text(), re.M):
            assert name in dp, f"{conf}: context {name} not in dialplan"


# --------------------------------------------------------------------------
# Every application and function must be built into the image
# --------------------------------------------------------------------------

BUILTIN_APPS = {"NoOp", "Set", "Goto", "GotoIf", "GotoIfTime", "Hangup", "Answer", "Wait"}
APP_MODULE = {
    "Dial": "app_dial", "Playback": "app_playback", "Read": "app_read", "Record": "app_record",
    "Gosub": "app_stack", "Return": "app_stack", "Stasis": "app_stasis",
    "Verbose": "app_verbose", "Log": "app_verbose", "Echo": "app_echo",
}
FUNC_MODULE = {
    "CURL": "func_curl", "CURLOPT": "func_curl", "REGEX": "func_strings", "STRFTIME": "func_strings",
    "CUT": "func_cut", "URIENCODE": "func_uri", "GROUP": "func_groupcount",
    "GROUP_COUNT": "func_groupcount", "IF": "func_logic", "STAT": "func_env",
    "DIALPLAN_EXISTS": "func_dialplan", "CHANNEL": "func_channel", "CALLERID": "func_callerid",
    "CONNECTEDLINE": "func_callerid",
}


def built_modules() -> set[str]:
    mods = set()
    for line in (ROOT / "modules.list").read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            mods.add(line)
    return mods


def test_apps_are_built(dp):
    mods = built_modules()
    for ctx, exten, st in all_steps(dp):
        if st.app in BUILTIN_APPS:
            continue
        assert st.app in APP_MODULE, f"{st.file}:{st.lineno}: unknown application {st.app}"
        assert APP_MODULE[st.app] in mods, f"{st.app} needs {APP_MODULE[st.app]} in modules.list"


def test_functions_are_built(rendered: Path):
    mods = built_modules()
    text = "\n".join(strip_comment(l) for _, _, l in load_lines(rendered / "extensions.conf"))
    for name in set(re.findall(r"\$\{([A-Z_]+)\(", text)) | set(re.findall(r"Set\(([A-Z_]+)\(", text)):
        assert name in FUNC_MODULE, f"unknown dialplan function {name}"
        assert FUNC_MODULE[name] in mods, f"{name} needs {FUNC_MODULE[name]} in modules.list"


def test_modules_conf_does_not_noload_built_modules():
    mods = built_modules()
    text = (ROOT / "config" / "modules.conf").read_text()
    for name in re.findall(r"^noload\s*=>\s*(\S+)\.so", text, re.M):
        assert name not in mods, f"modules.conf noloads {name}, which modules.list builds"


def test_core_modules_listed():
    mods = built_modules()
    for m in ("res_ari", "res_http_websocket", "res_websocket_client", "chan_websocket", "res_pjsip",
              "chan_pjsip", "res_curl", "func_curl", "res_prometheus", "codec_ulaw", "app_stasis"):
        assert m in mods


# --------------------------------------------------------------------------
# Compliance ordering
# --------------------------------------------------------------------------


def apps(dp, ctx: str, exten: str) -> list[Step]:
    return dp[ctx].extens[exten]


def index_of(steps: list[Step], pred) -> int:
    for i, s in enumerate(steps):
        if pred(s):
            return i
    raise AssertionError("step not found")


def test_stasis_only_in_buzz_stasis(dp):
    for ctx, exten, st in all_steps(dp):
        if st.app == "Stasis":
            assert ctx.name == "buzz-stasis", f"Stasis() in [{ctx.name}]"


def test_inbound_disclosure_before_agent(dp):
    steps = apps(dp, "buzz-inbound", "s")
    play = index_of(steps, lambda s: s.app == "Playback" and "disclosure-inbound" in s.args)
    check = index_of(steps, lambda s: s.app == "GotoIf" and "PLAYBACKSTATUS" in s.args)
    dial = index_of(steps, lambda s: s.app == "Dial")
    assert play < check < dial
    assert "@buzz-stasis/n" in steps[dial].args
    # failed disclosure hangs up, never reaches the agent
    assert steps[check + 1].app == "Log" and steps[check + 2].app == "Hangup"


def test_only_outbound_guard_dials_the_trunk(dp):
    for ctx, exten, st in all_steps(dp):
        if st.app == "Dial" and ("BUZZ_TRUNK" in st.args or "zm-trunk" in st.args):
            assert ctx.name == "buzz-outbound", f"trunk dialled from [{ctx.name}]"


def test_outbound_order_and_fail_closed(dp):
    steps = apps(dp, "buzz-outbound", "_[+0-9].")
    curl = index_of(steps, lambda s: s.app == "Set" and "CURL(" in s.args and "dialplan/check" in s.args)
    allow = index_of(steps, lambda s: s.app == "GotoIf" and "^ALLOW" in s.args)
    deny = index_of(steps, lambda s: s.app == "GotoIf" and "^DENY" in s.args)
    hours = index_of(steps, lambda s: s.app == "Gosub" and s.args.startswith("buzz-sub-hours"))
    limit = index_of(steps, lambda s: s.app == "GotoIf" and "GROUP_COUNT" in s.args)
    dial = index_of(steps, lambda s: s.app == "Dial")
    assert curl < allow < deny < hours < limit < dial
    # Header is set before the request
    header = index_of(steps, lambda s: s.app == "Set" and "CURLOPT(httpheader)=X-API-Key" in s.args)
    assert header < curl
    # Anything that is neither ALLOW nor DENY falls straight through to a hangup
    fall = steps[deny + 1 :]
    first_hangup = index_of(fall, lambda s: s.app == "Hangup")
    assert all(s.app != "Dial" for s in fall[:first_hangup])
    assert fall[first_hangup].args == "41"


def test_denials_never_use_retryable_causes(dp):
    steps = apps(dp, "buzz-outbound", "_[+0-9].")
    by_label = {s.label: i for i, s in enumerate(steps) if s.label}
    causes = {}
    for label in ("denied", "allowed", "in_hours", "have_channel"):
        assert label in by_label
    for i, s in enumerate(steps):
        if s.app == "Hangup" and s.args.isdigit():
            causes[i] = int(s.args)
    before_dial = index_of(steps, lambda s: s.app == "Dial")
    for i, cause in causes.items():
        if i < before_dial:
            assert cause not in (17, 18, 19), "Dograh campaigns retry 17/18/19; a refusal must not use them"
    assert sorted(c for i, c in causes.items() if i < before_dial) == [21, 29, 34, 41]


def test_callee_disclosure_aborts_on_failure(dp):
    steps = apps(dp, "buzz-sub-callee-disclosure", "s")
    play = index_of(steps, lambda s: s.app == "Playback" and "disclosure-outbound" in s.args)
    assert any(s.app == "Set" and s.args == "GOSUB_RESULT=ABORT" for s in steps[play:])
    dial = next(s for s in apps(dp, "buzz-outbound", "_[+0-9].") if s.app == "Dial")
    assert "U(buzz-sub-callee-disclosure)" in dial.args


def test_transfer_screens_then_takes_message(dp):
    steps = apps(dp, "buzz-transfer", "_X.")
    dial = index_of(steps, lambda s: s.app == "Dial")
    assert "U(buzz-sub-transfer-screen^" in steps[dial].args
    # no Answer() before staff accept: Dograh treats an answer as transfer success
    assert all(s.app != "Answer" for s in steps[:dial])
    screen = apps(dp, "buzz-sub-transfer-screen", "s")
    assert any(s.app == "Set" and s.args == "GOSUB_RESULT=CONTINUE" for s in screen)
    msg = apps(dp, "buzz-take-message", "s")
    handler = index_of(msg, lambda s: "hangup_handler_push" in s.args)
    record = index_of(msg, lambda s: s.app == "Record")
    assert handler < record
    assert msg[record].args.endswith(",k")


def test_hours_default_closed(dp):
    steps = apps(dp, "buzz-sub-hours", "s")
    first_return = index_of(steps, lambda s: s.app == "Return")
    assert steps[first_return].args == "closed"


def test_no_semicolons_in_dialplan_lines(rendered: Path):
    """';' starts a comment in Asterisk config, even inside app arguments."""
    for fname, lineno, raw in load_lines(rendered / "extensions.conf"):
        line = raw.strip()
        if line.startswith(("exten", "same")):
            assert ";" not in line, f"{fname}:{lineno}: ';' would truncate this line: {line}"
