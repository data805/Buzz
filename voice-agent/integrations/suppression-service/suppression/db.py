"""SQLite storage: suppressions, a hashed decision log and a removal audit.

stdlib sqlite3 in WAL mode with ``synchronous=FULL``: an opt-out that has been
acknowledged with 201 must survive a power cut. One short-lived connection
per operation; SQLite handles the locking (``busy_timeout``).

Numbers in the suppression list are stored in plain E.164 because the check
must match them exactly. Nothing else stores a number in plain text: the
decision log keeps only a keyed hash (HMAC-SHA-256, truncated), so log rows
can be correlated ("the same number was denied three times") without
revealing the number. A plain SHA-256 would not do: there are only about 10^9
Zambian numbers, so an unkeyed hash is reversible by brute force.
"""

from __future__ import annotations

import hashlib
import hmac
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Iterable, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS suppressions (
    e164        TEXT PRIMARY KEY,
    source      TEXT NOT NULL,
    call_id     TEXT,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS suppressions_created_at ON suppressions (created_at);

-- Every decision of /v1/check and /v1/dialplan/check. No plain numbers.
CREATE TABLE IF NOT EXISTS decision_log (
    id           INTEGER PRIMARY KEY,
    ts           TEXT NOT NULL,
    endpoint     TEXT NOT NULL,
    decision     TEXT NOT NULL,
    reason       TEXT NOT NULL,
    number_hash  TEXT
);
CREATE INDEX IF NOT EXISTS decision_log_ts ON decision_log (ts);

-- Opt-outs are never deleted through the API. A manual DELETE (the audited
-- removal procedure in the README) is recorded here automatically.
CREATE TABLE IF NOT EXISTS suppression_removals (
    id          INTEGER PRIMARY KEY,
    e164        TEXT NOT NULL,
    source      TEXT NOT NULL,
    call_id     TEXT,
    created_at  TEXT NOT NULL,
    removed_at  TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS suppressions_audit_delete
AFTER DELETE ON suppressions
BEGIN
    INSERT INTO suppression_removals (e164, source, call_id, created_at, removed_at)
    VALUES (OLD.e164, OLD.source, OLD.call_id, OLD.created_at,
            strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));
END;
CREATE TRIGGER IF NOT EXISTS suppressions_no_update
BEFORE UPDATE OF e164 ON suppressions
BEGIN
    SELECT RAISE(ABORT, 'suppressions.e164 is immutable');
END;
"""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def number_hash(key: bytes, e164_or_raw: str | None) -> str | None:
    if not e164_or_raw:
        return None
    return hmac.new(key, e164_or_raw.encode("utf-8"), hashlib.sha256).hexdigest()[:16]


class Store:
    def __init__(self, path: str, *, log_retention_days: int = 90) -> None:
        self.path = path
        self.log_retention_days = log_retention_days
        self._writes_since_prune = 0

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=5.0, isolation_level=None)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout = 5000")
            conn.execute("PRAGMA synchronous = FULL")
            yield conn
        finally:
            conn.close()

    def init(self) -> None:
        with self.connect() as conn:
            mode = conn.execute("PRAGMA journal_mode = WAL").fetchone()[0]
            if str(mode).lower() != "wal":
                raise RuntimeError(f"could not enable WAL mode (got {mode!r})")
            conn.executescript(SCHEMA)
        self.prune_log()

    def ping(self) -> None:
        with self.connect() as conn:
            conn.execute("SELECT 1 FROM suppressions LIMIT 1").fetchall()

    # --- suppressions -----------------------------------------------------

    def is_suppressed(self, e164: str) -> bool:
        with self.connect() as conn:
            row = conn.execute("SELECT 1 FROM suppressions WHERE e164 = ?", (e164,)).fetchone()
        return row is not None

    def add(self, e164: str, source: str, call_id: str | None) -> bool:
        """Insert an opt-out. Returns True if new, False if already present."""
        with self.connect() as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO suppressions (e164, source, call_id, created_at) "
                "VALUES (?, ?, ?, ?)",
                (e164, source, call_id, iso(utcnow())),
            )
            return cur.rowcount == 1

    def add_many(self, rows: Iterable[tuple[str, str, str | None]]) -> tuple[int, int]:
        """Insert many in one transaction. Returns (added, already_present)."""
        added = dup = 0
        now = iso(utcnow())
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                for e164, source, call_id in rows:
                    cur = conn.execute(
                        "INSERT OR IGNORE INTO suppressions (e164, source, call_id, created_at) "
                        "VALUES (?, ?, ?, ?)",
                        (e164, source, call_id, now),
                    )
                    if cur.rowcount == 1:
                        added += 1
                    else:
                        dup += 1
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
        return added, dup

    def list(self, since: str | None = None) -> list[dict]:
        with self.connect() as conn:
            if since:
                rows = conn.execute(
                    "SELECT e164, source, call_id, created_at FROM suppressions "
                    "WHERE created_at >= ? ORDER BY created_at, e164",
                    (since,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT e164, source, call_id, created_at FROM suppressions "
                    "ORDER BY created_at, e164"
                ).fetchall()
        return [dict(r) for r in rows]

    def count(self) -> int:
        with self.connect() as conn:
            return conn.execute("SELECT COUNT(*) FROM suppressions").fetchone()[0]

    # --- decision log -----------------------------------------------------

    def log_decision(self, endpoint: str, decision: str, reason: str, nhash: str | None) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO decision_log (ts, endpoint, decision, reason, number_hash) "
                "VALUES (?, ?, ?, ?, ?)",
                (iso(utcnow()), endpoint, decision, reason, nhash),
            )
        self._writes_since_prune += 1
        if self._writes_since_prune >= 500:
            self.prune_log()

    def prune_log(self) -> int:
        self._writes_since_prune = 0
        cutoff = iso(utcnow() - timedelta(days=self.log_retention_days))
        with self.connect() as conn:
            return conn.execute("DELETE FROM decision_log WHERE ts < ?", (cutoff,)).rowcount
