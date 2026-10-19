import sqlite3

import pytest
from fastapi.testclient import TestClient

from suppression.app import create_app

KEY = "k" * 32
AUTH = {"X-API-Key": KEY}


@pytest.fixture
def env(tmp_path):
    return {"SUPPRESSION_API_KEY": KEY, "SUPPRESSION_DB_PATH": str(tmp_path / "s.db")}


@pytest.fixture
def client(env):
    with TestClient(create_app(env)) as c:
        yield c


# --- start-up -----------------------------------------------------------------


def test_refuses_to_start_without_key(tmp_path):
    with pytest.raises(RuntimeError, match="SUPPRESSION_API_KEY"):
        create_app({"SUPPRESSION_DB_PATH": str(tmp_path / "s.db")})


def test_refuses_short_key(tmp_path):
    with pytest.raises(RuntimeError, match="at least"):
        create_app({"SUPPRESSION_API_KEY": "short", "SUPPRESSION_DB_PATH": str(tmp_path / "s.db")})


def test_database_is_wal(client, env):
    conn = sqlite3.connect(env["SUPPRESSION_DB_PATH"])
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


# --- auth -----------------------------------------------------------------------

PROTECTED = [
    ("get", "/v1/dialplan/check?number=0971234567", None),
    ("get", "/v1/check?number=0971234567", None),
    ("post", "/v1/opt-out", {"phone_number": "0971234567", "source": "test"}),
    ("get", "/v1/suppressions", None),
    ("post", "/v1/suppressions/import", None),
    ("post", "/v1/redact", {"value": "x"}),
]


@pytest.mark.parametrize("method, path, body", PROTECTED)
@pytest.mark.parametrize("headers", [{}, {"X-API-Key": "wrong"}, {"X-API-Key": KEY + "x"}, {"X-API-Key": ""}])
def test_protected_endpoints_need_the_key(client, method, path, body, headers):
    r = getattr(client, method)(path, headers=headers, **({"json": body} if body else {}))
    assert r.status_code == 401
    assert not r.text.startswith("ALLOW")


def test_health_and_metrics_are_open(client):
    r = client.get("/health")
    assert r.status_code == 200 and r.text == "ok"
    r = client.get("/metrics")
    assert r.status_code == 200
    assert "suppression_checks_total" in r.text


def test_no_openapi_docs(client):
    assert client.get("/docs").status_code == 404
    assert client.get("/openapi.json").status_code == 404


# --- dialplan text format ------------------------------------------------------

DIALPLAN = [
    ("0971234567", "ALLOW +260971234567"),
    ("+260971234567", "ALLOW +260971234567"),
    ("0211378200", "ALLOW +260211378200"),
    ("0631234567", "ALLOW +260631234567"),
    ("abc", "DENY invalid"),
    ("", "DENY invalid"),
    ("0551234567", "DENY invalid"),
    ("+27821234567", "DENY international"),
    ("0901234567", "DENY premium"),
    ("0800123456", "DENY premium"),
    ("112", "DENY short_code"),
    ("*115#", "DENY short_code"),
    ("0" * 200, "DENY invalid"),
]


@pytest.mark.parametrize("raw, body", DIALPLAN)
def test_dialplan_check_exact_body(client, raw, body):
    r = client.get("/v1/dialplan/check", params={"number": raw}, headers=AUTH)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain")
    assert r.text == body  # exact: one line, no trailing newline
    assert "\n" not in r.text and "\r" not in r.text


def test_dialplan_missing_param_is_deny_invalid(client):
    r = client.get("/v1/dialplan/check", headers=AUTH)
    assert r.text == "DENY invalid"


def test_dialplan_plus_encoding(client):
    # Properly URI-encoded '+' (what ${URIENCODE()} produces)
    r = client.get("/v1/dialplan/check?number=%2B260971234567", headers=AUTH)
    assert r.text == "ALLOW +260971234567"
    # Unencoded '+' decodes to a space; still recognised as Zambian
    r = client.get("/v1/dialplan/check?number=+260971234567", headers=AUTH)
    assert r.text == "ALLOW +260971234567"


def test_suppressed_number_is_denied_in_any_format(client):
    r = client.post("/v1/opt-out", json={"phone_number": "0971234567", "source": "test"}, headers=AUTH)
    assert r.status_code == 201
    for raw in ("0971234567", "+260 97 123 4567", "00260971234567", "971234567"):
        r = client.get("/v1/dialplan/check", params={"number": raw}, headers=AUTH)
        assert r.text == "DENY suppressed"
    r = client.get("/v1/check", params={"number": "971234567"}, headers=AUTH)
    assert r.json() == {"allow": False, "e164": "+260971234567", "reason": "suppressed"}


def test_dialplan_fails_closed_on_db_error(client, monkeypatch):
    store = client.app.state.store

    def boom(_):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(store, "is_suppressed", boom)
    r = client.get("/v1/dialplan/check", params={"number": "0971234567"}, headers=AUTH)
    assert r.status_code == 503
    assert r.text == "ERROR"
    r = client.get("/v1/check", params={"number": "0971234567"}, headers=AUTH)
    assert r.status_code == 503
    assert "allow" not in r.json()
    assert 'suppression_check_errors_total{endpoint="dialplan"} 1.0' in client.get("/metrics").text


def test_decision_log_write_failure_does_not_change_decision(client, monkeypatch):
    def boom(*a, **k):
        raise sqlite3.OperationalError("locked")

    monkeypatch.setattr(client.app.state.store, "log_decision", boom)
    r = client.get("/v1/dialplan/check", params={"number": "0971234567"}, headers=AUTH)
    assert r.text == "ALLOW +260971234567"


# --- JSON check: every reason code -------------------------------------------

CHECK = [
    ("0971234567", {"allow": True, "e164": "+260971234567", "reason": "ok"}),
    ("garbage", {"allow": False, "e164": None, "reason": "invalid"}),
    ("+447946000000", {"allow": False, "e164": "+447946000000", "reason": "international"}),
    ("0901234567", {"allow": False, "e164": "+260901234567", "reason": "premium"}),
    ("993", {"allow": False, "e164": None, "reason": "short_code"}),
]


@pytest.mark.parametrize("raw, expected", CHECK)
def test_check_json(client, raw, expected):
    r = client.get("/v1/check", params={"number": raw}, headers=AUTH)
    assert r.status_code == 200
    assert r.json() == expected


# --- opt-out ----------------------------------------------------------------------


def test_opt_out_is_idempotent(client):
    body = {"phone_number": "+260 96 123 4567", "source": "dograh_opt_out", "call_id": "run-42"}
    first = client.post("/v1/opt-out", json=body, headers=AUTH)
    second = client.post("/v1/opt-out", json={**body, "phone_number": "0961234567", "source": "staff"}, headers=AUTH)
    assert first.status_code == second.status_code == 201
    assert first.json() == second.json() == {"e164": "+260961234567", "suppressed": True}
    rows = client.get("/v1/suppressions", headers=AUTH).json()
    assert len(rows) == 1
    # First write wins: the original source and call_id are kept.
    assert rows[0]["source"] == "dograh_opt_out" and rows[0]["call_id"] == "run-42"
    assert rows[0]["created_at"].endswith("Z")
    m = client.get("/metrics").text
    assert 'suppression_opt_outs_total{result="new"} 1.0' in m
    assert 'suppression_opt_outs_total{result="duplicate"} 1.0' in m
    assert "suppression_listed_numbers 1.0" in m


def test_opt_out_integer_call_id_from_dograh(client):
    r = client.post("/v1/opt-out", json={"phone_number": "0771234567", "source": "dograh_opt_out", "call_id": 1842}, headers=AUTH)
    assert r.status_code == 201
    assert client.get("/v1/suppressions", headers=AUTH).json()[0]["call_id"] == "1842"


def test_opt_out_without_call_id(client):
    r = client.post("/v1/opt-out", json={"phone_number": "0771234567", "source": "staff"}, headers=AUTH)
    assert r.status_code == 201
    assert client.get("/v1/suppressions", headers=AUTH).json()[0]["call_id"] is None


def test_opt_out_international_is_stored(client):
    r = client.post("/v1/opt-out", json={"phone_number": "+27821234567", "source": "inbound"}, headers=AUTH)
    assert r.status_code == 201 and r.json()["e164"] == "+27821234567"


@pytest.mark.parametrize(
    "body, reason",
    [
        ({"phone_number": "112", "source": "x"}, "short_code"),
        ({"phone_number": "nonsense", "source": "x"}, "invalid"),
    ],
)
def test_opt_out_rejects_unstorable(client, body, reason):
    r = client.post("/v1/opt-out", json=body, headers=AUTH)
    assert r.status_code == 422
    assert r.json()["detail"]["reason"] == reason


@pytest.mark.parametrize(
    "body",
    [
        {"source": "x"},
        {"phone_number": "0971234567"},
        {"phone_number": "0971234567", "source": ""},
        {"phone_number": "0971234567", "source": "x" * 65},
        {"phone_number": "0971234567", "source": "x", "call_id": "c" * 129},
        {"phone_number": "0971234567", "source": "x", "call_id": {"a": 1}},
    ],
)
def test_opt_out_validation(client, body):
    assert client.post("/v1/opt-out", json=body, headers=AUTH).status_code == 422


def test_opt_outs_cannot_be_deleted_through_api(client):
    client.post("/v1/opt-out", json={"phone_number": "0971234567", "source": "x"}, headers=AUTH)
    assert client.delete("/v1/opt-out", headers=AUTH).status_code == 405
    assert client.delete("/v1/suppressions", headers=AUTH).status_code == 405


def test_manual_delete_is_audited(client, env):
    client.post("/v1/opt-out", json={"phone_number": "0971234567", "source": "x"}, headers=AUTH)
    conn = sqlite3.connect(env["SUPPRESSION_DB_PATH"])
    conn.execute("DELETE FROM suppressions WHERE e164 = '+260971234567'")
    conn.commit()
    rows = conn.execute("SELECT e164, source FROM suppression_removals").fetchall()
    assert rows == [("+260971234567", "x")]


# --- list ----------------------------------------------------------------------------


def test_list_since(client, env):
    client.post("/v1/opt-out", json={"phone_number": "0971234567", "source": "x"}, headers=AUTH)
    conn = sqlite3.connect(env["SUPPRESSION_DB_PATH"])
    conn.execute("UPDATE suppressions SET created_at = '2026-01-01T00:00:00.000Z'")
    conn.commit()
    client.post("/v1/opt-out", json={"phone_number": "0961234567", "source": "x"}, headers=AUTH)
    assert len(client.get("/v1/suppressions", headers=AUTH).json()) == 2
    recent = client.get("/v1/suppressions", params={"since": "2026-06-01"}, headers=AUTH).json()
    assert [r["e164"] for r in recent] == ["+260961234567"]
    r = client.get("/v1/suppressions", params={"since": "2026-06-01T00:00:00Z"}, headers=AUTH)
    assert len(r.json()) == 1
    assert client.get("/v1/suppressions", params={"since": "yesterday"}, headers=AUTH).status_code == 422


# --- import --------------------------------------------------------------------------

CSV = (
    "﻿Name,Phone_Number,source\n"
    "Alice,0971234567,\n"
    "Bob,+260 96 123 4567,hubspot\n"
    "Carol,not a number,\n"
    "Dan,112,\n"
    "Eve,971234567,\n"  # duplicate of Alice
    ",,\n"
    "Fay,9.71E+08,\n"  # Excel scientific notation
    "Gus,+27821234567,\n"
)


def post_csv(client, text, **params):
    return client.post(
        "/v1/suppressions/import",
        content=text.encode("utf-8"),
        params=params,
        headers={**AUTH, "Content-Type": "text/csv"},
    )


def test_import_with_bad_rows(client):
    client.post("/v1/opt-out", json={"phone_number": "0961234567", "source": "earlier"}, headers=AUTH)
    r = post_csv(client, CSV, source="staff_import")
    assert r.status_code == 200
    j = r.json()
    assert j == {
        "dry_run": False,
        "rows": 7,
        "added": 2,  # Alice, Gus
        "already_suppressed": 1,  # Bob
        "duplicate_in_file": 1,  # Eve
        "invalid": 3,  # Carol, Dan, Fay
        "blank": 1,
        "errors": [
            {"line": 4, "reason": "invalid"},
            {"line": 5, "reason": "short_code"},
            {"line": 8, "reason": "invalid"},
        ],
        "errors_truncated": False,
    }
    assert "0971234567" not in r.text and "not a number" not in r.text  # never echoes input
    rows = {x["e164"]: x for x in client.get("/v1/suppressions", headers=AUTH).json()}
    assert rows["+260971234567"]["source"] == "staff_import"
    assert rows["+260961234567"]["source"] == "earlier"  # existing row kept
    assert set(rows) == {"+260971234567", "+260961234567", "+27821234567"}


def test_import_dry_run_writes_nothing(client):
    j = post_csv(client, CSV, dry_run="true").json()
    assert j["dry_run"] is True and j["added"] == 3
    assert client.get("/v1/suppressions", headers=AUTH).json() == []


def test_import_is_idempotent(client):
    post_csv(client, CSV)
    j = post_csv(client, CSV).json()
    assert j["added"] == 0 and j["already_suppressed"] == 3


@pytest.mark.parametrize(
    "text, status",
    [
        ("", 400),
        ("name,number\nA,0971234567\n", 400),
        ("phone_number\n0971234567\n", 200),
    ],
)
def test_import_header_validation(client, text, status):
    assert post_csv(client, text).status_code == status


def test_import_rejects_non_utf8(client):
    r = client.post(
        "/v1/suppressions/import",
        content="phone_number\n0971234567\xff\n".encode("latin-1"),
        headers={**AUTH, "Content-Type": "text/csv"},
    )
    assert r.status_code == 400


# --- metrics and decision log --------------------------------------------------


def test_metrics_count_by_decision_and_reason(client):
    for raw in ("0971234567", "112", "+27821234567"):
        client.get("/v1/dialplan/check", params={"number": raw}, headers=AUTH)
    m = client.get("/metrics").text
    assert 'suppression_checks_total{decision="allow",endpoint="dialplan",reason="ok"} 1.0' in m
    assert 'suppression_checks_total{decision="deny",endpoint="dialplan",reason="short_code"} 1.0' in m
    assert 'suppression_checks_total{decision="deny",endpoint="dialplan",reason="international"} 1.0' in m
    assert 'suppression_checks_total{decision="deny",endpoint="check",reason="suppressed"} 0.0' in m


def test_decision_log_has_hash_not_number(client, env):
    client.get("/v1/dialplan/check", params={"number": "0971234567"}, headers=AUTH)
    client.get("/v1/check", params={"number": "+260971234567"}, headers=AUTH)
    conn = sqlite3.connect(env["SUPPRESSION_DB_PATH"])
    rows = conn.execute("SELECT endpoint, decision, reason, number_hash FROM decision_log").fetchall()
    assert len(rows) == 2
    assert rows[0][3] == rows[1][3]  # same number, same hash, whatever the input format
    assert len(rows[0][3]) == 16
    dump = "\n".join(conn.iterdump())
    assert "971234567" not in dump.replace("suppressions", "")  # no plain number in the log


def test_logs_do_not_contain_numbers(client, caplog):
    caplog.set_level("DEBUG")
    client.get("/v1/dialplan/check", params={"number": "0971234567"}, headers=AUTH)
    client.post("/v1/opt-out", json={"phone_number": "0961234567", "source": "x"}, headers=AUTH)
    # Server-side records only (the test client's own httpx logger prints URLs).
    text = "\n".join(r.getMessage() for r in caplog.records if not r.name.startswith("httpx"))
    assert "/v1/dialplan/check" in text
    assert "971234567" not in text and "961234567" not in text


# --- redact ------------------------------------------------------------------------


def test_redact_endpoint(client):
    r = client.post(
        "/v1/redact",
        json={"value": {"summary": "PIN 1234", "transcript": "NRC 123456/78/1, call 0971234567"}, "phones": True},
        headers=AUTH,
    )
    assert r.status_code == 200
    j = r.json()
    assert j["value"] == {
        "summary": "PIN [REDACTED:PIN]",
        "transcript": "NRC [REDACTED:NRC], call [REDACTED:PHONE]",
    }
    assert j["counts"] == {"pin": 1, "nrc": 1, "phone": 1} and j["total"] == 3
    assert 'suppression_redactions_total{kind="pin"} 1.0' in client.get("/metrics").text


def test_redact_rejects_bad_body(client):
    r = client.post("/v1/redact", content=b"not json", headers={**AUTH, "Content-Type": "application/json"})
    assert r.status_code == 422
    r = client.post("/v1/redact", content=b"x" * (2 * 1024 * 1024 + 1), headers=AUTH)
    assert r.status_code == 413
