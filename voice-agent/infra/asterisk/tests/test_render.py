"""Template rendering: env substitution, validation and generated includes."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from conftest import CONFIG, ENTRYPOINT, ROOT, SAMPLE_ENV, env_names, run_render

VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)")

# Asterisk channel variables and dialplan functions the dialplan may use.
ASTERISK_NAMES = {
    # variables
    "EXTEN", "UNIQUEID", "EPOCH", "DIALSTATUS", "HANGUPCAUSE", "PLAYBACKSTATUS",
    "STASISSTATUS", "GOSUB_RETVAL", "RECORD_STATUS",
    # functions
    "CALLERID", "CONNECTEDLINE", "CHANNEL", "CURL", "CURLOPT", "URIENCODE", "REGEX",
    "CUT", "GROUP", "GROUP_COUNT", "IF", "STAT", "STRFTIME", "DIALPLAN_EXISTS",
}


def test_env_list_has_no_buzz_prefix():
    for name in env_names():
        assert not name.startswith("BUZZ_"), f"{name}: BUZZ_ is reserved for dialplan variables"


def test_every_template_variable_is_declared():
    declared = set(env_names())
    for tpl in CONFIG.glob("*.conf"):
        if tpl.name == "extensions.conf":
            continue  # checked separately: mixes env and dialplan variables
        for name in VAR_RE.findall(tpl.read_text()):
            assert name in declared, f"{tpl.name}: ${{{name}}} is not in env.list"


def test_env_variables_only_in_extensions_globals():
    names = set(env_names())
    section = None
    for lineno, line in enumerate((CONFIG / "extensions.conf").read_text().splitlines(), 1):
        stripped = line.strip()
        if stripped.startswith("[") and "]" in stripped:
            section = stripped[1 : stripped.index("]")]
        if stripped.startswith(";"):
            continue
        for name in VAR_RE.findall(line):
            if name in names:
                assert section == "globals", f"extensions.conf:{lineno}: env var {name} outside [globals]"


def test_rendered_files_have_no_unresolved_env_vars(rendered: Path):
    names = set(env_names())
    for conf in rendered.glob("*.conf"):
        for name in VAR_RE.findall(conf.read_text()):
            assert name not in names, f"{conf.name}: ${{{name}}} was not substituted"


def test_only_asterisk_variables_survive(rendered: Path):
    for conf in rendered.glob("*.conf"):
        text = conf.read_text()
        for name in VAR_RE.findall(text):
            ok = name.startswith("BUZZ_") or re.fullmatch(r"ARG\d+", name) or name in ASTERISK_NAMES
            assert ok, f"{conf.name}: unexpected ${{{name}}} left after rendering"
        if conf.name not in {"extensions.conf", "extensions_dids.conf", "extensions_transfer_targets.conf", "extensions_hours.conf"}:
            assert "${" not in text, f"{conf.name}: non-dialplan file still contains ${{...}}"


def test_dialplan_expressions_survive(rendered: Path):
    ext = (rendered / "extensions.conf").read_text()
    assert "${EXTEN}" in ext
    assert '$["${PLAYBACKSTATUS}" = "SUCCESS"]' in ext
    assert "${GROUP_COUNT(trunk@buzz)}" in ext


def test_values_land_where_expected(rendered: Path):
    ari = (rendered / "ari.conf").read_text()
    assert f"password = {SAMPLE_ENV['ARI_PASSWORD']}" in ari
    assert "[dograh]" in ari
    http = (rendered / "http.conf").read_text()
    assert "bindaddr = 172.30.0.1" in http and "bindport = 8088" in http
    ws = (rendered / "websocket_client.conf").read_text()
    assert "uri = ws://127.0.0.1:8000/api/v1/telephony/ws/ari" in ws
    assert "protocols = media" in ws
    assert "connection_type = per_call_config" in ws
    assert "?" not in re.search(r"^uri = (.*)$", ws, re.M).group(1)
    ext = (rendered / "extensions.conf").read_text()
    assert "BUZZ_STASIS_APP=dograh_a1b2c3d4e5f6" in ext
    assert f"BUZZ_SUPPRESSION_KEY={SAMPLE_ENV['SUPPRESSION_API_KEY']}" in ext
    pjsip = (rendered / "pjsip.conf").read_text()
    assert "match = 198.51.100.10,198.51.100.11/32" in pjsip
    assert "from_domain = sip.carrier.example" in pjsip  # derived default
    assert "allow = ulaw" in pjsip and "disallow = all" in pjsip
    rtp = (rendered / "rtp.conf").read_text()
    assert "rtpstart = 10000" in rtp and "rtpend = 20000" in rtp


def test_hours_file(rendered: Path):
    hours = (rendered / "extensions_hours.conf").read_text()
    assert "[buzz-sub-hours]" in hours
    assert "GotoIfTime(08:00-17:59,mon-fri,*,*,Africa/Lusaka?open)" in hours
    assert "GotoIfTime(09:00-12:59,sat,*,*,Africa/Lusaka?open)" in hours
    assert "GotoIfTime(*,*,1,jan,Africa/Lusaka?closed)" in hours
    assert "GotoIfTime(*,*,25,dec,Africa/Lusaka?closed)" in hours
    # closed days are checked before opening hours; default is closed
    assert hours.index("?closed)") < hours.index("?open)")
    assert "n(closed),Return(closed)" in hours


def test_ip_trunk_has_no_auth(rendered: Path):
    auth = (rendered / "pjsip_trunk_auth.conf").read_text()
    assert "type=auth" not in auth


def test_trunk_auth_and_registration(tmp_path: Path):
    r = run_render(tmp_path, {"ZM_TRUNK_USERNAME": "buzz", "ZM_TRUNK_PASSWORD": "p" * 20, "ZM_TRUNK_REGISTER": "yes"})
    assert r.returncode == 0, r.stderr
    auth = (tmp_path / "pjsip_trunk_auth.conf").read_text()
    assert "[zm-trunk](+)" in auth and "outbound_auth=zm-trunk-auth" in auth
    assert "type=registration" in auth and "server_uri=sip:sip.carrier.example:5060" in auth


def test_local_files_copied(rendered: Path):
    assert "[buzz-inbound-dids]" in (rendered / "extensions_dids.conf").read_text()
    assert "[buzz-transfer-targets]" in (rendered / "extensions_transfer_targets.conf").read_text()
    assert "(staff-endpoint)" in (rendered / "pjsip_staff.conf").read_text()


@pytest.mark.parametrize(
    "overrides, message",
    [
        ({"ARI_PASSWORD": None}, "ARI_PASSWORD"),
        ({"ARI_PASSWORD": "short"}, "ARI_PASSWORD"),
        ({"ARI_PASSWORD": "abc;defghijklmnopqrstuvwxyz"}, "ARI_PASSWORD"),
        ({"ARI_PASSWORD": "change-me-change-me-change-me"}, "placeholder"),
        ({"DOGRAH_STASIS_APP": "buzz"}, "DOGRAH_STASIS_APP"),
        ({"DOGRAH_WS_URI": "ws://127.0.0.1:8000/api/v1/telephony/ws/ari?x=1"}, "DOGRAH_WS_URI"),
        ({"ZM_TRUNK_MATCH": "carrier.example"}, "ZM_TRUNK_MATCH"),
        ({"ZM_TRUNK_NUMBER_FORMAT": "local"}, "ZM_TRUNK_NUMBER_FORMAT"),
        ({"CALL_HOURS": "weekdays 8-6"}, "CALL_HOURS"),
        ({"CALL_HOURS": "mon-fri 08:00-24:00"}, "CALL_HOURS"),
        ({"CALL_TIMEZONE": "Mars/Olympus"}, "CALL_TIMEZONE"),
        ({"CALL_CLOSED_DAYS": "christmas"}, "CALL_CLOSED_DAYS"),
        ({"OUTBOUND_RING_TIMEOUT": "25"}, "OUTBOUND_RING_TIMEOUT"),
        ({"OUTBOUND_CLI": "BongoHive"}, "OUTBOUND_CLI"),
        ({"ZM_TRUNK_REGISTER": "yes"}, "ZM_TRUNK_REGISTER"),
        ({"MESSAGE_WEBHOOK_SECRET": ""}, "MESSAGE_WEBHOOK_SECRET"),
    ],
)
def test_bad_environment_is_rejected(tmp_path: Path, overrides, message):
    r = run_render(tmp_path, overrides)
    assert r.returncode != 0
    assert message in r.stderr


def test_shell_scripts_parse():
    for script in (ENTRYPOINT, ROOT / "healthcheck.sh", ROOT / "sounds" / "generate_placeholders.sh"):
        r = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
        assert r.returncode == 0, f"{script.name}: {r.stderr}"
