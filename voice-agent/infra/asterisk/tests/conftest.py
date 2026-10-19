"""Shared fixtures: render the Asterisk templates with a sample environment.

The real entrypoint.sh does the rendering (envsubst with the explicit list in
config/env.list), so the tests exercise the same code path as the container.
Needs bash and envsubst (gettext) on PATH; no Asterisk, no Docker.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]  # infra/asterisk
CONFIG = ROOT / "config"
ENTRYPOINT = ROOT / "entrypoint.sh"

SAMPLE_ENV = {
    "ARI_PASSWORD": "a" * 32,
    "DOGRAH_STASIS_APP": "dograh_a1b2c3d4e5f6",
    "ASTERISK_METRICS_PASSWORD": "m" * 32,
    "ASTERISK_PUBLIC_IP": "203.0.113.10",
    "ASTERISK_LAN_CIDR": "10.0.0.0/24",
    "STAFF_LAN_CIDR": "10.0.0.0/24",
    "ZM_TRUNK_HOST": "sip.carrier.example",
    "ZM_TRUNK_MATCH": "198.51.100.10,198.51.100.11/32",
    "SUPPRESSION_API_KEY": "s" * 32,
    "OUTBOUND_CLI": "+260211123456",
    "CALL_CLOSED_DAYS": "1 jan, 25 dec",
    "MESSAGE_WEBHOOK_URL": "http://127.0.0.1:5678/webhook/buzz-message",
    "MESSAGE_WEBHOOK_SECRET": "w" * 32,
}


def zoneinfo_dir() -> str:
    for candidate in ("/usr/share/zoneinfo", "/var/db/timezone/zoneinfo"):
        if Path(candidate, "Africa", "Lusaka").exists():
            return candidate
    pytest.skip("no zoneinfo database with Africa/Lusaka on this machine")


def run_render(dest: Path, overrides: dict[str, str | None] | None = None) -> subprocess.CompletedProcess:
    if not shutil.which("envsubst"):
        pytest.skip("envsubst (gettext) not installed")
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "BUZZ_LOCAL_DIR": str(ROOT / "local.example"),
        "BUZZ_ZONEINFO_DIR": zoneinfo_dir(),
    }
    env.update(SAMPLE_ENV)
    for key, value in (overrides or {}).items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value
    return subprocess.run(
        ["bash", str(ENTRYPOINT), "render", str(CONFIG), str(dest)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.fixture(scope="session")
def rendered(tmp_path_factory) -> Path:
    dest = tmp_path_factory.mktemp("etc-asterisk")
    result = run_render(dest)
    assert result.returncode == 0, result.stderr
    return dest


def env_names() -> list[str]:
    names = []
    for line in (CONFIG / "env.list").read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            names.append(line.split("|", 1)[0])
    return names
