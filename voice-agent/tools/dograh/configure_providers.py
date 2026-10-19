#!/usr/bin/env python3
"""Idempotent bootstrap of Buzz's local model providers and ARI telephony in Dograh.

Targets Dograh v1.49.0 (git tag ``dograh-v1.49.0``). Every route, field and
provider ID below was read from that tag's source; citations are of the form
``api/<path>:<line>`` relative to the Dograh repository root.

What it converges (PLAN.md section 5 and section 6):

1. Provider connection ``buzz-vllm``: provider ``openai`` with
   ``base_url=http://${INFERENCE_HOST}:8000/v1`` and ``api_key=${VLLM_API_KEY}``.
2. Provider connection ``buzz-speaches``: provider ``speaches`` with
   ``base_url=http://${INFERENCE_HOST}:8001/v1`` and ``api_key=${SPEACHES_API_KEY}``.
   One connection serves both STT and TTS; the role is chosen in the
   configuration, not the connection.
3. Named model configuration ``buzz-local`` (pipeline mode): LLM ``buzz-llm`` at
   temperature 0.3, STT and TTS on Speaches with the models and voice from env.
4. ``buzz-local`` as the Organisation Default (``PUT /default``).
5. Archive every active ``dograh`` provider connection, after first archiving
   the named configurations that reference it (Dograh refuses otherwise).
6. ARI telephony configuration ``buzz-asterisk`` (default outbound), and print
   the Stasis application name Dograh generates for it (DOGRAH_STASIS_APP).

Modes: default applies changes; ``--dry-run`` prints the plan and writes
nothing; ``--check`` prints drift and exits 1 if anything differs (for cron or
CI). Secrets are never printed: every message passes through ``Redactor``.

Authentication (Dograh local auth):
- ``POST /api/v1/auth/login`` with ``{"email", "password"}`` returns
  ``{"token", "user"}`` (api/routes/auth.py:85-120, api/schemas/auth.py:17-32).
  The API prefix ``/api/v1`` is set in api/app.py:51 and :180.
- Subsequent calls send ``Authorization: Bearer <token>``
  (api/services/auth/depends.py:254-268). Alternatively an org API key in
  ``X-API-Key`` takes precedence (api/services/auth/depends.py:33-43, :293).

Exit codes: 0 converged / no drift, 1 drift found in --check, 2 error.
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

import httpx

API_PREFIX = "/api/v1"  # api/app.py:51

# Route constants. Prefixes: model-connections router api/routes/model_connections.py:60-62,
# organizations router api/routes/organization.py:89.
R_LOGIN = f"{API_PREFIX}/auth/login"  # api/routes/auth.py:85
R_CONNECTIONS = f"{API_PREFIX}/model-connections/provider-connections"  # :104 (GET), :118 (POST)
R_CONFIGS = f"{API_PREFIX}/model-connections/model-configurations"  # :216 (GET), :230 (POST)
R_DEFAULT = f"{API_PREFIX}/model-connections/default"  # :344 (GET), :356 (PUT)
R_TELEPHONY = f"{API_PREFIX}/organizations/telephony-configs"  # api/routes/organization.py:592, :640

# Provider IDs: api/services/configuration/registry.py:88-127 (ServiceProviders),
# :409 OPENAI_PROVIDER = Provider("openai", ...), :424 DOGRAH_PROVIDER, :456 SPEACHES_PROVIDER.
PROVIDER_OPENAI = "openai"
PROVIDER_SPEACHES = "speaches"
PROVIDER_DOGRAH = "dograh"

# Configuration roles: api/schemas/model_connections.py:69-84 (ModelConfigurationSpec, version 3).
ROLES = ("llm", "llm_fallback", "stt", "tts", "realtime", "embeddings")


class ConfigError(Exception):
    """Missing or invalid local configuration."""


class ApiError(Exception):
    """Dograh returned an unexpected status."""


# --------------------------------------------------------------------------
# Environment
# --------------------------------------------------------------------------


def load_env_file(path: Path) -> dict[str, str]:
    """Parse a dotenv file (KEY=VALUE, optional quotes, # comments, optional 'export ')."""
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export ") :]
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        elif " #" in value:
            value = value.split(" #", 1)[0].rstrip()
        values[key] = value
    return values


def merged_env(env_files: list[Path], environ: dict[str, str] | None = None) -> dict[str, str]:
    """Process environment wins over .env files; later files win over earlier."""
    env: dict[str, str] = {}
    for path in env_files:
        env.update(load_env_file(path))
    env.update(environ if environ is not None else os.environ)
    return env


@dataclass
class Settings:
    api_url: str
    inference_host: str
    vllm_api_key: str
    speaches_api_key: str
    admin_email: str | None = None
    admin_password: str | None = field(default=None, repr=False)
    api_key: str | None = field(default=None, repr=False)
    llm_model: str = "buzz-llm"
    llm_temperature: float = 0.3
    stt_model: str = "deepdml/faster-whisper-large-v3-turbo-ct2"
    stt_language: str = "en"
    tts_model: str = "speaches-ai/Kokoro-82M-v1.0-ONNX"
    tts_voice: str = "bf_emma"
    tts_speed: float = 1.0
    llm_connection_name: str = "buzz-vllm"
    speaches_connection_name: str = "buzz-speaches"
    configuration_name: str = "buzz-local"
    configure_telephony: bool = True
    telephony_name: str = "buzz-asterisk"
    ari_endpoint: str = "http://172.30.0.1:8088"
    ari_user: str = "dograh"
    ari_password: str | None = field(default=None, repr=False)
    ari_ws_client_name: str = "dograh"
    ari_dial_string_template: str = "Local/{number}@buzz-outbound/n"

    @property
    def llm_base_url(self) -> str:
        return f"http://{self.inference_host}:8000/v1"

    @property
    def speaches_base_url(self) -> str:
        return f"http://{self.inference_host}:8001/v1"

    def secrets(self) -> list[str]:
        return [
            s
            for s in (
                self.vllm_api_key,
                self.speaches_api_key,
                self.admin_password,
                self.api_key,
                self.ari_password,
            )
            if s
        ]

    @classmethod
    def from_env(cls, env: dict[str, str], *, telephony: bool = True) -> "Settings":
        def req(name: str) -> str:
            value = env.get(name, "").strip()
            if not value:
                raise ConfigError(f"{name} is not set (environment or .env)")
            return value

        def opt(name: str, default: str) -> str:
            return env.get(name, "").strip() or default

        host = req("INFERENCE_HOST")
        if host in {"localhost", "127.0.0.1", "::1"}:
            # PLAN.md section 5: inside the api container, localhost:8000 is Dograh itself.
            raise ConfigError("INFERENCE_HOST must be the inference server's LAN address, never localhost")
        api_key = env.get("DOGRAH_API_KEY", "").strip() or None
        email = env.get("DOGRAH_ADMIN_EMAIL", "").strip() or None
        password = env.get("DOGRAH_ADMIN_PASSWORD", "") or None
        if not api_key and not (email and password):
            raise ConfigError("set DOGRAH_ADMIN_EMAIL and DOGRAH_ADMIN_PASSWORD (or DOGRAH_API_KEY)")
        settings = cls(
            api_url=opt("DOGRAH_API_URL", "http://127.0.0.1:8000").rstrip("/"),
            inference_host=host,
            vllm_api_key=req("VLLM_API_KEY"),
            speaches_api_key=req("SPEACHES_API_KEY"),
            admin_email=email,
            admin_password=password,
            api_key=api_key,
            llm_model=opt("LLM_MODEL_NAME", "buzz-llm"),
            llm_temperature=float(opt("LLM_TEMPERATURE", "0.3")),
            stt_model=opt("STT_MODEL", cls.stt_model),
            stt_language=opt("STT_LANGUAGE", "en"),
            tts_model=opt("TTS_MODEL", cls.tts_model),
            tts_voice=opt("TTS_VOICE", "bf_emma"),
            tts_speed=float(opt("TTS_SPEED", "1.0")),
            configure_telephony=telephony,
            telephony_name=opt("DOGRAH_TELEPHONY_CONFIG_NAME", "buzz-asterisk"),
            ari_endpoint=opt("ARI_ENDPOINT", "http://172.30.0.1:8088").rstrip("/"),
            ari_user=opt("ARI_USER", "dograh"),
            ari_password=env.get("ARI_PASSWORD") or None,
            ari_ws_client_name=opt("ARI_WS_CLIENT_NAME", "dograh"),
            ari_dial_string_template=opt("ARI_DIAL_STRING_TEMPLATE", "Local/{number}@buzz-outbound/n"),
        )
        if telephony and not settings.ari_password:
            raise ConfigError("ARI_PASSWORD is not set (or pass --skip-telephony)")
        return settings


class Redactor:
    """Replaces every known secret with *** before anything is printed."""

    def __init__(self, secrets: list[str]):
        self._secrets = sorted({s for s in secrets if s and len(s) >= 4}, key=len, reverse=True)

    def __call__(self, text: str) -> str:
        for secret in self._secrets:
            text = text.replace(secret, "***")
        return text


# --------------------------------------------------------------------------
# HTTP client
# --------------------------------------------------------------------------


class DograhClient:
    def __init__(self, settings: Settings, *, transport: httpx.BaseTransport | None = None, timeout: float = 30.0):
        self._settings = settings
        self._redact = Redactor(settings.secrets())
        self._http = httpx.Client(base_url=settings.api_url, transport=transport, timeout=timeout)

    def close(self) -> None:
        self._http.close()

    def login(self) -> None:
        if self._settings.api_key:
            # X-API-Key takes precedence in get_user (api/services/auth/depends.py:40-41).
            self._http.headers["X-API-Key"] = self._settings.api_key
            return
        # LoginRequest {email, password} -> AuthResponse {token, user} (api/schemas/auth.py:17-32).
        response = self._http.post(
            R_LOGIN,
            json={"email": self._settings.admin_email, "password": self._settings.admin_password},
        )
        if response.status_code == 404:
            raise ApiError("login route returned 404: is AUTH_PROVIDER=local? (api/services/auth/depends.py:21-30)")
        self._expect(response, 200, "login")
        self._http.headers["Authorization"] = f"Bearer {response.json()['token']}"

    def _expect(self, response: httpx.Response, status: int | tuple[int, ...], what: str) -> httpx.Response:
        wanted = (status,) if isinstance(status, int) else status
        if response.status_code not in wanted:
            try:
                detail: Any = response.json().get("detail")
            except Exception:
                detail = response.text[:300]
            raise ApiError(self._redact(f"{what}: HTTP {response.status_code}: {detail}"))
        return response

    def get(self, path: str, what: str, **params: Any) -> Any:
        return self._expect(self._http.get(path, params=params or None), 200, what).json()

    def post(self, path: str, what: str, body: Any = None, status: int | tuple[int, ...] = (200, 201)) -> Any:
        response = self._expect(self._http.post(path, json=body), status, what)
        return response.json() if response.content else None

    def patch(self, path: str, what: str, body: Any) -> Any:
        return self._expect(self._http.patch(path, json=body), 200, what).json()

    def put(self, path: str, what: str, body: Any) -> Any:
        return self._expect(self._http.put(path, json=body), 200, what).json()

    def delete(self, path: str, what: str) -> None:
        self._expect(self._http.delete(path), (200, 204), what)


# --------------------------------------------------------------------------
# Desired state
# --------------------------------------------------------------------------


def desired_connections(s: Settings) -> list[dict[str, Any]]:
    """ProviderConnectionCreate bodies (api/schemas/model_connections.py:125-129).

    ``credentials`` may hold only CREDENTIAL_FIELDS and ``connection_settings``
    only CONNECTION_FIELDS (api/schemas/model_connections.py:10-25), each
    restricted to fields the provider's registry classes declare
    (api/services/configuration/model_connections.py:159-200).
    - openai declares ``base_url`` (registry.py:541-553) and ``api_key`` (registry.py:151-198).
    - speaches declares ``base_url`` and optional ``api_key`` for LLM, STT and TTS
      (registry.py:740-766, :2155-2186, :1614-1650).
    Creating an ``openai`` connection makes Dograh call ``GET {base_url}/models``
    with the key (api/services/configuration/check_validity.py:301-312), so vLLM
    must be up and the key correct when this runs.
    """
    return [
        {
            "name": s.llm_connection_name,
            "provider": PROVIDER_OPENAI,
            "credentials": {"api_key": s.vllm_api_key},
            "connection_settings": {"base_url": s.llm_base_url},
        },
        {
            "name": s.speaches_connection_name,
            "provider": PROVIDER_SPEACHES,
            "credentials": {"api_key": s.speaches_api_key},
            "connection_settings": {"base_url": s.speaches_base_url},
        },
    ]


def desired_configuration(s: Settings, llm_uuid: str, speaches_uuid: str) -> dict[str, Any]:
    """ModelConfigurationSpec (api/schemas/model_connections.py:69-84).

    Each role is a ServiceSelection {provider_connection_uuid, settings}
    (:39-53). ``settings`` must not contain credentials, connection fields or
    ``provider`` (:42-49). Setting names are the registry fields:
    - openai LLM: ``model``, ``temperature`` (registry.py:219-257, :541-553)
    - speaches STT: ``model``, ``language`` (registry.py:2155-2180)
    - speaches TTS: ``model``, ``voice``, ``speed`` (registry.py:1614-1645)
    """
    return {
        "version": 3,
        "mode": "pipeline",
        "llm": {
            "provider_connection_uuid": llm_uuid,
            "settings": {"model": s.llm_model, "temperature": s.llm_temperature},
        },
        "stt": {
            "provider_connection_uuid": speaches_uuid,
            "settings": {"model": s.stt_model, "language": s.stt_language},
        },
        "tts": {
            "provider_connection_uuid": speaches_uuid,
            "settings": {"model": s.tts_model, "voice": s.tts_voice, "speed": s.tts_speed},
        },
    }


def desired_ari_config(s: Settings) -> dict[str, Any]:
    """ARIConfigurationRequest (api/services/telephony/providers/ari/config.py:74-121).

    ``app_name`` is the ARI *username* (ari.conf section); the Stasis app name
    is generated server-side as ``dograh_<12 hex>`` on create and preserved on
    update (api/services/telephony/providers/ari/__init__.py:21-64).
    """
    return {
        "provider": "ari",
        "ari_endpoint": s.ari_endpoint,
        "app_name": s.ari_user,
        "app_password": s.ari_password,
        "ws_client_name": s.ari_ws_client_name,
        "dial_string_template": s.ari_dial_string_template,
    }


# --------------------------------------------------------------------------
# Comparison helpers
# --------------------------------------------------------------------------


def normalise_configuration(config: dict[str, Any]) -> dict[str, Any]:
    """Drop null roles and default keys so stored and desired specs compare cleanly."""
    out: dict[str, Any] = {"version": config.get("version", 3), "mode": config.get("mode", "pipeline")}
    for role in ROLES:
        value = config.get(role)
        if value is None:
            continue
        if isinstance(value, dict) and "provider_connection_uuid" in value:
            out[role] = {
                "provider_connection_uuid": str(value["provider_connection_uuid"]),
                "settings": {k: v for k, v in (value.get("settings") or {}).items()},
            }
        else:
            out[role] = value
    return out


def referenced_connection_uuids(config: dict[str, Any]) -> set[str]:
    found: set[str] = set()

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "provider_connection_uuid" and value:
                    found.add(str(value))
                else:
                    walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(config)
    return found


def _same_url(a: str | None, b: str | None) -> bool:
    return (a or "").rstrip("/") == (b or "").rstrip("/")


# --------------------------------------------------------------------------
# Plan / apply
# --------------------------------------------------------------------------


@dataclass
class Action:
    description: str
    run: Callable[[], Any] | None = None


@dataclass
class Result:
    actions: list[Action] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    stasis_app_name: str | None = None


class Bootstrapper:
    def __init__(self, client: DograhClient, settings: Settings, *, apply: bool, rotate_credentials: bool = False):
        self.c = client
        self.s = settings
        self.apply = apply
        self.rotate = rotate_credentials
        self.result = Result()

    def _do(self, description: str, run: Callable[[], Any]) -> Any:
        self.result.actions.append(Action(description, run))
        if self.apply:
            return run()
        return None

    # --- provider connections ---------------------------------------------
    def converge_connections(self) -> dict[str, str]:
        """Returns {connection name: uuid}; uuid is a placeholder in dry-run for new rows."""
        # ProviderConnectionResponse: uuid, name, provider, connection_settings,
        # configured_credentials (names only, never values), revision, is_active
        # (api/schemas/model_connections.py:145-152).
        existing = self.c.get(R_CONNECTIONS, "list provider connections")
        uuids: dict[str, str] = {}
        for want in desired_connections(self.s):
            matches = [row for row in existing if row["name"] == want["name"] and row.get("is_active", True)]
            if len(matches) > 1:
                self.result.notes.append(f"several active connections named {want['name']!r}; using the first")
            if not matches:
                created = self._do(
                    f"create {want['provider']} connection {want['name']!r} -> {want['connection_settings']['base_url']}",
                    lambda want=want: self.c.post(R_CONNECTIONS, f"create connection {want['name']}", want, status=201),
                )
                uuids[want["name"]] = created["uuid"] if created else f"<new:{want['name']}>"
                continue
            row = matches[0]
            uuids[want["name"]] = str(row["uuid"])
            if row["provider"] != want["provider"]:
                raise ApiError(
                    f"connection {want['name']!r} has provider {row['provider']!r}, expected {want['provider']!r}; "
                    "archive or rename it by hand (provider is immutable: ProviderConnectionUpdate, "
                    "api/schemas/model_connections.py:132-142)"
                )
            # ProviderConnectionUpdate {name?, credentials?, connection_settings?, revision?};
            # omitted credential keys are kept (api/schemas/model_connections.py:28-32, :132-142;
            # PATCH handler api/routes/model_connections.py:144-185).
            patch: dict[str, Any] = {}
            reasons = []
            if not _same_url(row["connection_settings"].get("base_url"), want["connection_settings"]["base_url"]) or set(
                row["connection_settings"]
            ) != set(want["connection_settings"]):
                patch["connection_settings"] = want["connection_settings"]
                reasons.append(
                    f"base_url {row['connection_settings'].get('base_url')!r} -> {want['connection_settings']['base_url']!r}"
                )
            if "api_key" not in row.get("configured_credentials", []):
                patch["credentials"] = want["credentials"]
                reasons.append("api_key missing")
            elif self.rotate:
                patch["credentials"] = want["credentials"]
                reasons.append("credential rotation requested")
            if patch:
                patch["revision"] = row["revision"]
                self._do(
                    f"update connection {want['name']!r}: {', '.join(reasons)}",
                    lambda row=row, patch=patch: self.c.patch(
                        f"{R_CONNECTIONS}/{row['uuid']}", f"update connection {row['name']}", patch
                    ),
                )
        return uuids

    # --- named configuration + default ------------------------------------
    def converge_configuration(self, uuids: dict[str, str]) -> str:
        want = desired_configuration(self.s, uuids[self.s.llm_connection_name], uuids[self.s.speaches_connection_name])
        # NamedModelConfigurationResponse {uuid, name, configuration, revision, is_active}
        # (api/schemas/model_connections.py:172-177).
        existing = self.c.get(R_CONFIGS, "list model configurations")
        matches = [row for row in existing if row["name"] == self.s.configuration_name and row.get("is_active", True)]
        if not matches:
            created = self._do(
                f"create model configuration {self.s.configuration_name!r}",
                lambda: self.c.post(
                    R_CONFIGS,
                    "create model configuration",
                    {"name": self.s.configuration_name, "configuration": want},
                    status=201,
                ),
            )
            config_uuid = created["uuid"] if created else f"<new:{self.s.configuration_name}>"
        else:
            row = matches[0]
            config_uuid = str(row["uuid"])
            if normalise_configuration(row["configuration"]) != normalise_configuration(want):
                # NamedModelConfigurationUpdate {name?, configuration?, revision?}
                # (api/schemas/model_connections.py:160-169; handler routes/model_connections.py:264-303).
                self._do(
                    f"update model configuration {self.s.configuration_name!r} (settings or connections drifted)",
                    lambda row=row: self.c.patch(
                        f"{R_CONFIGS}/{row['uuid']}",
                        "update model configuration",
                        {"configuration": want, "revision": row["revision"]},
                    ),
                )
        # DefaultModelConfigurationResponse {model_configuration_uuid|null}
        # (api/schemas/model_connections.py:180-185; routes/model_connections.py:344-382).
        current = self.c.get(R_DEFAULT, "get default configuration").get("model_configuration_uuid")
        if str(current) != config_uuid:
            self._do(
                f"set {self.s.configuration_name!r} as Organisation Default (was {current})",
                lambda: self.c.put(R_DEFAULT, "set default configuration", {"model_configuration_uuid": config_uuid}),
            )
        return config_uuid

    # --- archive the bootstrap Dograh connection ---------------------------
    def archive_dograh(self, buzz_config_uuid: str) -> None:
        connections = self.c.get(R_CONNECTIONS, "list provider connections")
        dograh = [row for row in connections if row["provider"] == PROVIDER_DOGRAH and row.get("is_active", True)]
        if not dograh:
            return
        dograh_uuids = {str(row["uuid"]) for row in dograh}
        # A connection referenced by an active named configuration cannot be
        # archived (api/db/model_connection_client.py:363-373), and the default
        # configuration cannot be archived (:359-362). The bootstrap creates
        # "Organization default" pointing at the "Dograh" connection
        # (api/services/organization_bootstrap.py:31-33, :170-178).
        for config in self.c.get(R_CONFIGS, "list model configurations"):
            if str(config["uuid"]) == buzz_config_uuid or not config.get("is_active", True):
                continue
            if referenced_connection_uuids(config["configuration"]) & dograh_uuids:
                self._do(
                    f"archive model configuration {config['name']!r} (uses the Dograh connection)",
                    lambda config=config: self.c.delete(
                        f"{R_CONFIGS}/{config['uuid']}", f"archive configuration {config['name']}"
                    ),
                )
        for row in dograh:
            # DELETE archives (soft) and returns 204 (api/routes/model_connections.py:188-199).
            # Still refused with 409 if an active workflow pins it (model_connection_client.py:374-397).
            self._do(
                f"archive {PROVIDER_DOGRAH} connection {row['name']!r}",
                lambda row=row: self.c.delete(f"{R_CONNECTIONS}/{row['uuid']}", f"archive connection {row['name']}"),
            )

    # --- telephony ---------------------------------------------------------
    def converge_telephony(self) -> None:
        want = desired_ari_config(self.s)
        # TelephonyConfigurationListResponse {configurations: [ {id, name, provider, is_default_outbound, ...} ]}
        # (api/schemas/telephony_config.py:89-112, :178-179).
        listing = self.c.get(R_TELEPHONY, "list telephony configurations")["configurations"]
        matches = [row for row in listing if row["name"] == self.s.telephony_name]
        if not matches:
            # TelephonyConfigurationCreateRequest {name, is_default_outbound, config}
            # (api/schemas/telephony_config.py:68-79; handler api/routes/organization.py:640-685).
            created = self._do(
                f"create ARI telephony configuration {self.s.telephony_name!r} -> {want['ari_endpoint']}",
                lambda: self.c.post(
                    R_TELEPHONY,
                    "create telephony configuration",
                    {"name": self.s.telephony_name, "is_default_outbound": True, "config": want},
                ),
            )
            if created:
                self.result.stasis_app_name = (created.get("credentials") or {}).get("stasis_app_name")
            return
        row = matches[0]
        if row["provider"] != "ari":
            raise ApiError(f"telephony configuration {self.s.telephony_name!r} is provider {row['provider']!r}, not ari")
        # TelephonyConfigurationDetail.credentials is masked for sensitive fields only
        # (api/schemas/telephony_config.py:152-175; masking api/routes/organization.py:831-851;
        # app_password is the only sensitive ARI field, ari/__init__.py:98-103; stasis_app_name
        # deliberately stays readable, ari/__init__.py:248-252).
        detail = self.c.get(f"{R_TELEPHONY}/{row['id']}", "get telephony configuration")
        creds = detail.get("credentials") or {}
        self.result.stasis_app_name = creds.get("stasis_app_name")
        drift = [
            f"{key} {creds.get(key)!r} -> {want[key]!r}"
            for key in ("ari_endpoint", "app_name", "ws_client_name", "dial_string_template")
            if (creds.get(key) or "") != want[key]
        ]
        if self.rotate:
            drift.append("credential rotation requested")
        if drift:
            # PUT replaces credentials wholesale; the Stasis name is carried over
            # server-side (ari/__init__.py:41-64; handler api/routes/organization.py:704-764).
            self._do(
                f"update telephony configuration {self.s.telephony_name!r}: {'; '.join(drift)}",
                lambda: self.c.put(
                    f"{R_TELEPHONY}/{row['id']}", "update telephony configuration", {"config": want}
                ),
            )
        if not row.get("is_default_outbound"):
            self._do(
                f"make {self.s.telephony_name!r} the default outbound configuration",
                lambda: self.c.post(
                    f"{R_TELEPHONY}/{row['id']}/set-default-outbound", "set default outbound", None
                ),  # api/routes/organization.py:766-780
            )

    def run(self) -> Result:
        self.c.login()
        uuids = self.converge_connections()
        config_uuid = self.converge_configuration(uuids)
        self.archive_dograh(config_uuid)
        if self.s.configure_telephony:
            self.converge_telephony()
        return self.result


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="report drift only; exit 1 if any")
    mode.add_argument("--dry-run", action="store_true", help="print the plan; change nothing")
    parser.add_argument(
        "--env-file",
        action="append",
        type=Path,
        default=None,
        help="dotenv file(s) to read; default ./.env if present. The process environment wins.",
    )
    parser.add_argument("--skip-telephony", action="store_true", help="do not touch the ARI configuration")
    parser.add_argument(
        "--rotate-credentials",
        action="store_true",
        help="re-send API keys and the ARI password even if nothing else drifted (Dograh never returns them, "
        "so key drift cannot be detected)",
    )
    return parser.parse_args(argv)


def main(
    argv: list[str] | None = None,
    *,
    environ: dict[str, str] | None = None,
    transport: httpx.BaseTransport | None = None,
    out=None,
) -> int:
    out = out or sys.stdout
    args = parse_args(argv)
    env_files = args.env_file if args.env_file is not None else [Path(".env")]
    try:
        settings = Settings.from_env(merged_env(env_files, environ), telephony=not args.skip_telephony)
    except ConfigError as exc:
        print(f"error: {exc}", file=out)
        return 2
    redact = Redactor(settings.secrets())
    apply = not (args.check or args.dry_run)
    client = DograhClient(settings, transport=transport)
    try:
        result = Bootstrapper(client, settings, apply=apply, rotate_credentials=args.rotate_credentials).run()
    except (ApiError, httpx.HTTPError) as exc:
        print(redact(f"error: {exc}"), file=out)
        return 2
    finally:
        client.close()

    verb = "applied" if apply else ("drift" if args.check else "would")
    for action in result.actions:
        print(redact(f"[{verb}] {action.description}"), file=out)
    for note in result.notes:
        print(redact(f"[note] {note}"), file=out)
    if not result.actions:
        print("in sync: nothing to do", file=out)
    if settings.configure_telephony:
        if result.stasis_app_name:
            print(f"DOGRAH_STASIS_APP={result.stasis_app_name}", file=out)
        elif not apply:
            print("DOGRAH_STASIS_APP will be generated by Dograh when the ARI configuration is created", file=out)
    if args.check and result.actions:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
