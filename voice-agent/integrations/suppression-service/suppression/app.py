"""Buzz suppression service: the compliance gate for every outbound dial.

API contract: docs/INTERFACES.md, "Suppression service API". Extra endpoint
(not yet in INTERFACES.md): POST /v1/redact, used by n8n.

Fail-closed design
------------------
* The dialplan only dials when the body starts with exactly ``ALLOW +260``.
  Any error path (bad key, database error, crash, timeout, empty body) yields
  a body that does not, so Asterisk hangs up.
* The process refuses to start without SUPPRESSION_API_KEY, or if the
  database cannot be opened in WAL mode.
* A database error while checking the suppression list is an error (HTTP
  503, body ``ERROR``), never an ALLOW.
* The number rules only allow ranges ZICTA has *assigned*; new ranges are
  denied until the code is updated (see numbers.py).

Privacy (DPIA)
--------------
* uvicorn's access log is switched off because it prints query strings,
  which contain phone numbers. Our own access log line has no query string.
* The decision log stores a keyed hash of the number, never the number.
* Request bodies (opt-outs, imports, redaction input) are never logged.
"""

from __future__ import annotations

import csv
import hmac
import io
import logging
import os
import time
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Literal

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse
from prometheus_client import CollectorRegistry, Counter as PCounter, Gauge, Histogram
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel, Field, field_validator

from buzz_redaction import redact

from . import numbers
from .db import Store, iso, number_hash

log = logging.getLogger("suppression")

REASONS = ("invalid", "international", "premium", "short_code", "suppressed")
MIN_KEY_LENGTH = 24
MAX_IMPORT_BYTES = 5 * 1024 * 1024
MAX_IMPORT_ERRORS = 1000
MAX_REDACT_BYTES = 2 * 1024 * 1024

_CATEGORY_REASON = {
    numbers.Category.INVALID: "invalid",
    numbers.Category.INTERNATIONAL: "international",
    numbers.Category.PREMIUM: "premium",
    numbers.Category.SHORT_CODE: "short_code",
}


class Settings:
    def __init__(self, env: dict[str, str] | None = None) -> None:
        env = dict(os.environ if env is None else env)
        key = env.get("SUPPRESSION_API_KEY", "")
        if not key:
            raise RuntimeError("SUPPRESSION_API_KEY is not set; refusing to start")
        if len(key) < MIN_KEY_LENGTH:
            raise RuntimeError(
                f"SUPPRESSION_API_KEY must be at least {MIN_KEY_LENGTH} characters"
            )
        self.api_key = key.encode("utf-8")
        self.db_path = env.get("SUPPRESSION_DB_PATH", "/var/lib/suppression/suppression.db")
        hash_key = env.get("SUPPRESSION_LOG_HASH_KEY") or ("buzz-log-hash:" + key)
        self.log_hash_key = hash_key.encode("utf-8")
        self.log_retention_days = int(env.get("SUPPRESSION_LOG_RETENTION_DAYS", "90"))


class Metrics:
    def __init__(self, store: Store) -> None:
        self.registry = CollectorRegistry()
        self.checks = PCounter(
            "suppression_checks",
            "Number checks by endpoint, decision and reason",
            ["endpoint", "decision", "reason"],
            registry=self.registry,
        )
        self.errors = PCounter(
            "suppression_check_errors",
            "Checks that failed closed because of an internal error",
            ["endpoint"],
            registry=self.registry,
        )
        self.opt_outs = PCounter(
            "suppression_opt_outs",
            "Opt-out requests; result is new or duplicate",
            ["result"],
            registry=self.registry,
        )
        self.import_rows = PCounter(
            "suppression_import_rows",
            "CSV import rows by result",
            ["result"],
            registry=self.registry,
        )
        self.redactions = PCounter(
            "suppression_redactions",
            "Items redacted by POST /v1/redact, by kind",
            ["kind"],
            registry=self.registry,
        )
        self.latency = Histogram(
            "suppression_request_seconds",
            "Request latency by route",
            ["route"],
            registry=self.registry,
            buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5),
        )
        listed = Gauge(
            "suppression_listed_numbers",
            "Numbers currently on the suppression list",
            registry=self.registry,
        )
        listed.set_function(lambda: float(store.count()))
        for endpoint in ("check", "dialplan"):
            self.checks.labels(endpoint, "allow", "ok")
            for r in REASONS:
                self.checks.labels(endpoint, "deny", r)
            self.errors.labels(endpoint)
        for r in ("new", "duplicate"):
            self.opt_outs.labels(r)


class OptOutIn(BaseModel):
    phone_number: str = Field(min_length=1, max_length=64)
    source: str = Field(min_length=1, max_length=64)
    # Dograh's body template keeps native types, so {{initial_context.workflow_run_id}}
    # arrives as an integer.
    call_id: str | int | None = Field(default=None)

    @field_validator("call_id")
    @classmethod
    def _call_id(cls, v):
        if v is None:
            return None
        v = str(v).strip()
        if len(v) > 128:
            raise ValueError("call_id longer than 128 characters")
        return v or None


class OptOutOut(BaseModel):
    e164: str
    suppressed: Literal[True] = True


class CheckOut(BaseModel):
    allow: bool
    e164: str | None
    reason: str


class RedactIn(BaseModel):
    value: Any
    phones: bool = False


def create_app(env: dict[str, str] | None = None) -> FastAPI:
    settings = Settings(env)
    store = Store(settings.db_path, log_retention_days=settings.log_retention_days)
    store.init()
    metrics = Metrics(store)

    app = FastAPI(
        title="Buzz suppression service",
        version="1.0.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.settings = settings
    app.state.store = store
    app.state.metrics = metrics

    def require_key(request: Request) -> None:
        supplied = request.headers.get("x-api-key", "")
        if not hmac.compare_digest(supplied.encode("utf-8"), settings.api_key):
            raise HTTPException(status_code=401, detail="unauthorised")

    @app.middleware("http")
    async def access_log(request: Request, call_next):
        start = time.perf_counter()
        status = 500
        try:
            response = await call_next(request)
            status = response.status_code
            return response
        finally:
            elapsed = time.perf_counter() - start
            route = request.scope.get("route")
            path = getattr(route, "path", "unmatched")
            metrics.latency.labels(path).observe(elapsed)
            # Path template only, never the query string or body.
            level = logging.DEBUG if path in ("/health", "/metrics") else logging.INFO
            log.log(level, "%s %s %s %.1fms", request.method, path, status, elapsed * 1000)

    def decide(raw: str, endpoint: str) -> tuple[bool, str | None, str]:
        c = numbers.classify(raw)
        nhash = number_hash(settings.log_hash_key, c.e164 or raw)
        if c.callable:
            assert c.e164 is not None
            if store.is_suppressed(c.e164):
                allow, reason = False, "suppressed"
            else:
                allow, reason = True, "ok"
        else:
            allow, reason = False, _CATEGORY_REASON[c.category]
        decision = "allow" if allow else "deny"
        metrics.checks.labels(endpoint, decision, reason).inc()
        try:
            store.log_decision(endpoint, decision, reason, nhash)
        except Exception:  # the log must never turn a DENY into an error or vice versa
            log.exception("decision log write failed")
        return allow, c.e164, reason

    # --- open endpoints ---------------------------------------------------

    @app.get("/health", response_class=PlainTextResponse)
    def health() -> PlainTextResponse:
        try:
            store.ping()
        except Exception:
            log.exception("health check failed")
            return PlainTextResponse("unavailable", status_code=503)
        return PlainTextResponse("ok")

    @app.get("/metrics")
    def prometheus() -> Response:
        return Response(generate_latest(metrics.registry), media_type=CONTENT_TYPE_LATEST)

    # --- protected endpoints ----------------------------------------------

    @app.get(
        "/v1/dialplan/check",
        response_class=PlainTextResponse,
        dependencies=[Depends(require_key)],
    )
    def dialplan_check(number: str = Query("")):
        # One line, NO trailing newline: "ALLOW +260971234567" or "DENY suppressed".
        try:
            allow, e164, reason = decide(number, "dialplan")
        except Exception:
            metrics.errors.labels("dialplan").inc()
            log.exception("dialplan check failed; failing closed")
            return PlainTextResponse("ERROR", status_code=503)
        return PlainTextResponse(f"ALLOW {e164}" if allow else f"DENY {reason}")

    @app.get("/v1/check", response_model=CheckOut, dependencies=[Depends(require_key)])
    def check(number: str = Query("")):
        try:
            allow, e164, reason = decide(number, "check")
        except Exception:
            metrics.errors.labels("check").inc()
            log.exception("check failed; failing closed")
            return JSONResponse({"detail": "unavailable"}, status_code=503)
        return CheckOut(allow=allow, e164=e164, reason=reason)

    @app.post(
        "/v1/opt-out",
        status_code=201,
        response_model=OptOutOut,
        dependencies=[Depends(require_key)],
    )
    def opt_out(body: OptOutIn):
        c = numbers.classify(body.phone_number)
        if c.e164 is None:
            # Short codes and unparseable input cannot be suppressed.
            raise HTTPException(
                status_code=422,
                detail={"reason": _CATEGORY_REASON.get(c.category, "invalid")},
            )
        new = store.add(c.e164, body.source.strip(), body.call_id)
        metrics.opt_outs.labels("new" if new else "duplicate").inc()
        return OptOutOut(e164=c.e164)

    @app.get("/v1/suppressions", dependencies=[Depends(require_key)])
    def list_suppressions(since: str | None = Query(None, max_length=40)):
        since_iso = None
        if since:
            try:
                dt = datetime.fromisoformat(since.replace("Z", "+00:00"))
            except ValueError:
                raise HTTPException(status_code=422, detail="since must be an ISO 8601 date or time")
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            since_iso = iso(dt)
        return store.list(since_iso)

    @app.post("/v1/suppressions/import", dependencies=[Depends(require_key)])
    async def import_csv(
        request: Request,
        source: str = Query("import", min_length=1, max_length=64),
        dry_run: bool = Query(False),
    ):
        raw = await request.body()
        if len(raw) > MAX_IMPORT_BYTES:
            raise HTTPException(status_code=413, detail="CSV larger than 5 MB")
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            raise HTTPException(status_code=400, detail="CSV must be UTF-8")
        reader = csv.DictReader(io.StringIO(text))
        if reader.fieldnames is None:
            raise HTTPException(status_code=400, detail="CSV is empty")
        columns = {(f or "").strip().lower(): f for f in reader.fieldnames}
        if "phone_number" not in columns:
            raise HTTPException(status_code=400, detail="CSV needs a phone_number column")
        phone_col = columns["phone_number"]
        source_col = columns.get("source")
        call_col = columns.get("call_id")

        counts: Counter = Counter()
        errors: list[dict] = []
        to_add: list[tuple[str, str, str | None]] = []
        seen: set[str] = set()
        try:
            for row in reader:
                line = reader.line_num
                value = (row.get(phone_col) or "").strip()
                if not value and not any((v or "").strip() for v in row.values() if isinstance(v, str)):
                    counts["blank"] += 1
                    continue
                counts["rows"] += 1
                c = numbers.classify(value)
                if c.e164 is None:
                    counts["invalid"] += 1
                    if len(errors) < MAX_IMPORT_ERRORS:
                        # Line number and reason only; never echo the number.
                        errors.append({"line": line, "reason": _CATEGORY_REASON.get(c.category, "invalid")})
                    continue
                if c.e164 in seen:
                    counts["duplicate_in_file"] += 1
                    continue
                seen.add(c.e164)
                row_source = ((row.get(source_col) if source_col else None) or source).strip()[:64]
                row_call = ((row.get(call_col) if call_col else None) or "").strip()[:128] or None
                to_add.append((c.e164, row_source, row_call))
        except csv.Error as exc:
            raise HTTPException(status_code=400, detail=f"CSV parse error near line {reader.line_num}: {exc}")

        if dry_run:
            existing = sum(1 for e, _, _ in to_add if store.is_suppressed(e))
            added, already = len(to_add) - existing, existing
        else:
            added, already = store.add_many(to_add)
            metrics.import_rows.labels("added").inc(added)
            metrics.import_rows.labels("already_suppressed").inc(already)
            metrics.import_rows.labels("invalid").inc(counts["invalid"])

        return {
            "dry_run": dry_run,
            "rows": counts["rows"],
            "added": added,
            "already_suppressed": already,
            "duplicate_in_file": counts["duplicate_in_file"],
            "invalid": counts["invalid"],
            "blank": counts["blank"],
            "errors": errors,
            "errors_truncated": counts["invalid"] > len(errors),
        }

    @app.post("/v1/redact", dependencies=[Depends(require_key)])
    async def redact_endpoint(request: Request):
        raw = await request.body()
        if len(raw) > MAX_REDACT_BYTES:
            raise HTTPException(status_code=413, detail="body larger than 2 MB")
        try:
            body = RedactIn.model_validate_json(raw)
        except Exception:
            raise HTTPException(status_code=422, detail="expected JSON {\"value\": ..., \"phones\": bool}")
        counts: Counter = Counter()
        value = redact(body.value, phones=body.phones, counts=counts)
        for kind, n in counts.items():
            metrics.redactions.labels(kind).inc(n)
        return {"value": value, "counts": dict(counts), "total": sum(counts.values())}

    return app
