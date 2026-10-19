"""Tests for configure_providers.py against an in-memory fake of the Dograh v1.49.0 API.

The fake mirrors the behaviour the script relies on (see the citations in
configure_providers.py): soft archive, revision checks, the refusal to archive
a connection that an active configuration references, and the generated
Stasis application name.
"""

from __future__ import annotations

import io
import json
import sys
import uuid
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import configure_providers as cp  # noqa: E402

SECRET_VLLM = "vllm-secret-key-123"
SECRET_SPEACHES = "speaches-secret-key-456"
SECRET_ARI = "ari-secret-pass-789"
SECRET_ADMIN = "admin-secret-pass-000"

ENV = {
    "DOGRAH_API_URL": "http://dograh.test",
    "DOGRAH_ADMIN_EMAIL": "admin@bongohive.co.zm",
    "DOGRAH_ADMIN_PASSWORD": SECRET_ADMIN,
    "INFERENCE_HOST": "10.0.0.20",
    "VLLM_API_KEY": SECRET_VLLM,
    "SPEACHES_API_KEY": SECRET_SPEACHES,
    "ARI_PASSWORD": SECRET_ARI,
    "TTS_VOICE": "bf_emma",
}


class FakeDograh:
    def __init__(self, *, bootstrap_dograh: bool = True):
        self.connections: dict[str, dict] = {}
        self.configs: dict[str, dict] = {}
        self.default: str | None = None
        self.telephony: dict[int, dict] = {}
        self.writes: list[str] = []
        self.token = "jwt-token"
        if bootstrap_dograh:
            # What organization_bootstrap.py creates on first sign-in.
            conn = self._add_connection("Dograh", "dograh", {"api_key": "mps-xyz"}, {})
            cfg = self._add_config("Organization default", {
                "version": 3, "mode": "pipeline",
                **{r: {"provider_connection_uuid": conn, "settings": {}} for r in ("llm", "stt", "tts", "embeddings")},
            })
            self.default = cfg

    def _add_connection(self, name, provider, credentials, settings):
        u = str(uuid.uuid4())
        self.connections[u] = {"uuid": u, "name": name, "provider": provider, "credentials": dict(credentials),
                               "connection_settings": dict(settings), "revision": 1, "is_active": True}
        return u

    def _add_config(self, name, configuration):
        u = str(uuid.uuid4())
        self.configs[u] = {"uuid": u, "name": name, "configuration": configuration, "revision": 1, "is_active": True}
        return u

    @staticmethod
    def _conn_out(row):
        return {k: row[k] for k in ("uuid", "name", "provider", "connection_settings", "revision", "is_active")} | {
            "configured_credentials": sorted(k for k, v in row["credentials"].items() if v)}

    @staticmethod
    def _cfg_out(row):
        full = {"version": 3, "mode": "pipeline", "llm_fallback": None, "realtime": None, "embeddings": None}
        full.update(row["configuration"])
        return {**row, "configuration": full}

    def _tel_detail(self, row):
        creds = dict(row["credentials"])
        creds["app_password"] = "ar****89"
        return {"id": row["id"], "name": row["name"], "provider": "ari", "is_default_outbound": row["is_default_outbound"],
                "credentials": creds, "created_at": "2026-10-19T00:00:00Z", "updated_at": "2026-10-19T00:00:00Z"}

    def handler(self, request: httpx.Request) -> httpx.Response:
        path, method = request.url.path, request.method
        body = json.loads(request.content) if request.content else None
        if path == "/api/v1/auth/login":
            if body == {"email": ENV["DOGRAH_ADMIN_EMAIL"], "password": SECRET_ADMIN}:
                return httpx.Response(200, json={"token": self.token, "user": {"id": 1, "email": body["email"]}})
            return httpx.Response(401, json={"detail": "Invalid email or password"})
        if request.headers.get("authorization") != f"Bearer {self.token}":
            return httpx.Response(401, json={"detail": "Authorization header required"})
        if method != "GET":
            self.writes.append(f"{method} {path}")

        base = "/api/v1/model-connections"
        if path == f"{base}/provider-connections":
            if method == "GET":
                return httpx.Response(200, json=[self._conn_out(r) for r in self.connections.values() if r["is_active"]])
            u = self._add_connection(body["name"], body["provider"], body["credentials"], body["connection_settings"])
            return httpx.Response(201, json=self._conn_out(self.connections[u]))
        if path.startswith(f"{base}/provider-connections/"):
            u = path.rsplit("/", 1)[1]
            row = self.connections[u]
            if method == "PATCH":
                if body.get("revision", row["revision"]) != row["revision"]:
                    return httpx.Response(409, json={"detail": "changed"})
                row["connection_settings"] = body.get("connection_settings", row["connection_settings"])
                row["credentials"].update(body.get("credentials", {}))
                row["revision"] += 1
                return httpx.Response(200, json=self._conn_out(row))
            if method == "DELETE":
                for cfg in self.configs.values():
                    if cfg["is_active"] and u in cp.referenced_connection_uuids(cfg["configuration"]):
                        return httpx.Response(409, json={"detail": "Provider connection is used by a model configuration"})
                row["is_active"] = False
                return httpx.Response(204)
        if path == f"{base}/model-configurations":
            if method == "GET":
                return httpx.Response(200, json=[self._cfg_out(r) for r in self.configs.values() if r["is_active"]])
            u = self._add_config(body["name"], body["configuration"])
            return httpx.Response(201, json=self._cfg_out(self.configs[u]))
        if path.startswith(f"{base}/model-configurations/"):
            u = path.rsplit("/", 1)[1]
            row = self.configs[u]
            if method == "PATCH":
                row["configuration"] = body["configuration"]
                row["revision"] += 1
                return httpx.Response(200, json=self._cfg_out(row))
            if method == "DELETE":
                if self.default == u:
                    return httpx.Response(409, json={"detail": "Select another organization default before archiving"})
                row["is_active"] = False
                return httpx.Response(204)
        if path == f"{base}/default":
            if method == "GET":
                return httpx.Response(200, json={"model_configuration_uuid": self.default})
            self.default = body["model_configuration_uuid"]
            return httpx.Response(200, json=body)

        tel = "/api/v1/organizations/telephony-configs"
        if path == tel:
            if method == "GET":
                return httpx.Response(200, json={"configurations": [
                    {"id": r["id"], "name": r["name"], "provider": "ari", "is_default_outbound": r["is_default_outbound"],
                     "created_at": "x", "updated_at": "x"} for r in self.telephony.values()]})
            cid = len(self.telephony) + 1
            creds = {k: v for k, v in body["config"].items() if k != "provider"}
            creds["stasis_app_name"] = "dograh_0123456789ab"
            self.telephony[cid] = {"id": cid, "name": body["name"], "is_default_outbound": body["is_default_outbound"],
                                   "credentials": creds}
            return httpx.Response(200, json=self._tel_detail(self.telephony[cid]))
        if path.startswith(tel + "/"):
            parts = path[len(tel) + 1:].split("/")
            row = self.telephony[int(parts[0])]
            if len(parts) == 2 and parts[1] == "set-default-outbound":
                row["is_default_outbound"] = True
                return httpx.Response(200, json=self._tel_detail(row))
            if method == "GET":
                return httpx.Response(200, json=self._tel_detail(row))
            if method == "PUT":
                stasis = row["credentials"]["stasis_app_name"]
                row["credentials"] = {k: v for k, v in body["config"].items() if k != "provider"} | {"stasis_app_name": stasis}
                return httpx.Response(200, json=self._tel_detail(row))
        return httpx.Response(404, json={"detail": f"no fake route {method} {path}"})


def run(fake: FakeDograh, *args: str, env: dict | None = None) -> tuple[int, str]:
    out = io.StringIO()
    code = cp.main([*args, "--env-file", "/nonexistent/.env"], environ=env or ENV,
                   transport=httpx.MockTransport(fake.handler), out=out)
    return code, out.getvalue()


def active(fake, provider):
    return [r for r in fake.connections.values() if r["provider"] == provider and r["is_active"]]


def test_first_run_converges_everything():
    fake = FakeDograh()
    code, out = run(fake)
    assert code == 0, out
    (vllm,) = active(fake, "openai")
    assert vllm["name"] == "buzz-vllm"
    assert vllm["connection_settings"] == {"base_url": "http://10.0.0.20:8000/v1"}
    assert vllm["credentials"] == {"api_key": SECRET_VLLM}
    (sp,) = active(fake, "speaches")
    assert sp["connection_settings"] == {"base_url": "http://10.0.0.20:8001/v1"}
    assert active(fake, "dograh") == []
    buzz = [c for c in fake.configs.values() if c["name"] == "buzz-local"][0]
    assert fake.default == buzz["uuid"]
    cfg = buzz["configuration"]
    assert cfg["llm"] == {"provider_connection_uuid": vllm["uuid"], "settings": {"model": "buzz-llm", "temperature": 0.3}}
    assert cfg["stt"]["settings"] == {"model": "deepdml/faster-whisper-large-v3-turbo-ct2", "language": "en"}
    assert cfg["tts"]["settings"] == {"model": "speaches-ai/Kokoro-82M-v1.0-ONNX", "voice": "bf_emma", "speed": 1.0}
    org_default = [c for c in fake.configs.values() if c["name"] == "Organization default"][0]
    assert org_default["is_active"] is False
    (tel,) = fake.telephony.values()
    assert tel["credentials"]["dial_string_template"] == "Local/{number}@buzz-outbound/n"
    assert tel["credentials"]["ari_endpoint"] == "http://172.30.0.1:8088"
    assert tel["credentials"]["app_name"] == "dograh" and tel["credentials"]["ws_client_name"] == "dograh"
    assert tel["is_default_outbound"] is True
    assert "DOGRAH_STASIS_APP=dograh_0123456789ab" in out


def test_second_run_is_a_no_op():
    fake = FakeDograh()
    assert run(fake)[0] == 0
    writes_before = list(fake.writes)
    code, out = run(fake)
    assert code == 0
    assert fake.writes == writes_before, "second run must not write"
    assert "in sync" in out
    assert "DOGRAH_STASIS_APP=dograh_0123456789ab" in out


def test_check_on_converged_instance_exits_zero():
    fake = FakeDograh()
    run(fake)
    code, out = run(fake, "--check")
    assert code == 0 and "in sync" in out


def test_check_detects_base_url_drift_without_writing():
    fake = FakeDograh()
    run(fake)
    (vllm,) = active(fake, "openai")
    vllm["connection_settings"]["base_url"] = "https://api.openai.com/v1"
    writes_before = list(fake.writes)
    code, out = run(fake, "--check")
    assert code == 1
    assert "[drift] update connection 'buzz-vllm'" in out and "api.openai.com" in out
    assert fake.writes == writes_before


def test_check_detects_settings_default_and_telephony_drift():
    fake = FakeDograh()
    run(fake)
    buzz = [c for c in fake.configs.values() if c["name"] == "buzz-local"][0]
    buzz["configuration"]["llm"]["settings"]["temperature"] = 0.9
    fake.default = None
    (tel,) = fake.telephony.values()
    tel["credentials"]["dial_string_template"] = "PJSIP/{number}@zm-trunk"
    code, out = run(fake, "--check")
    assert code == 1
    assert "update model configuration 'buzz-local'" in out
    assert "Organisation Default" in out
    assert "dial_string_template 'PJSIP/{number}@zm-trunk'" in out


def test_apply_repairs_drift():
    fake = FakeDograh()
    run(fake)
    (vllm,) = active(fake, "openai")
    vllm["connection_settings"]["base_url"] = "http://wrong:8000/v1"
    code, _ = run(fake)
    assert code == 0
    assert vllm["connection_settings"]["base_url"] == "http://10.0.0.20:8000/v1"
    assert vllm["revision"] == 2
    assert run(fake, "--check")[0] == 0


def test_dry_run_writes_nothing_on_fresh_instance():
    fake = FakeDograh()
    code, out = run(fake, "--dry-run")
    assert code == 0
    assert fake.writes == []
    assert "[would] create openai connection 'buzz-vllm'" in out
    assert "[would] archive dograh connection 'Dograh'" in out
    assert "will be generated by Dograh" in out


def test_instance_without_bootstrap_dograh_connection():
    # MPS blackholed (PLAN F4): bootstrap never created the Dograh connection.
    fake = FakeDograh(bootstrap_dograh=False)
    code, out = run(fake)
    assert code == 0, out
    assert "archive" not in out


def test_secrets_never_printed_even_on_errors():
    fake = FakeDograh()
    original = fake.handler

    def leaky(request):
        if request.method == "POST" and request.url.path.endswith("/provider-connections"):
            return httpx.Response(422, json={"detail": f"bad key {SECRET_VLLM} for {SECRET_SPEACHES}"})
        return original(request)

    fake.handler = leaky
    code, out = run(fake)
    assert code == 2
    for secret in (SECRET_VLLM, SECRET_SPEACHES, SECRET_ARI, SECRET_ADMIN):
        assert secret not in out
    assert "***" in out


def test_rejects_localhost_inference_host():
    code, out = run(FakeDograh(), env={**ENV, "INFERENCE_HOST": "localhost"})
    assert code == 2 and "never localhost" in out


def test_skip_telephony_does_not_require_ari_password():
    env = {k: v for k, v in ENV.items() if k != "ARI_PASSWORD"}
    fake = FakeDograh()
    code, out = run(fake, "--skip-telephony", env=env)
    assert code == 0, out
    assert fake.telephony == {}


def test_bad_login_is_reported():
    code, out = run(FakeDograh(), env={**ENV, "DOGRAH_ADMIN_PASSWORD": "wrong-password"})
    assert code == 2 and "login: HTTP 401" in out and "wrong-password" not in out


def test_env_file_parsing(tmp_path):
    f = tmp_path / ".env"
    f.write_text('# c\nexport A=1\nB="two words"\nC=3 # trailing\nD=\n', encoding="utf-8")
    assert cp.load_env_file(f) == {"A": "1", "B": "two words", "C": "3", "D": ""}
    merged = cp.merged_env([f], {"A": "env-wins"})
    assert merged["A"] == "env-wins" and merged["B"] == "two words"


@pytest.mark.parametrize("stored,desired,equal", [
    ({"version": 3, "mode": "pipeline", "realtime": None, "llm": {"provider_connection_uuid": "a", "settings": {"model": "m"}}},
     {"version": 3, "mode": "pipeline", "llm": {"provider_connection_uuid": "a", "settings": {"model": "m"}}}, True),
    ({"llm": {"provider_connection_uuid": "a", "settings": {"model": "m"}}},
     {"llm": {"provider_connection_uuid": "b", "settings": {"model": "m"}}}, False),
])
def test_normalise_configuration(stored, desired, equal):
    assert (cp.normalise_configuration(stored) == cp.normalise_configuration(desired)) is equal
