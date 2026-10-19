"""Latency statistics and the report table."""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence


def percentile(values: Iterable[float], p: float) -> float:
    """Percentile with linear interpolation between closest ranks.

    Same definition as numpy.percentile's default ("linear"):
    rank = p/100 * (n - 1).
    """
    xs = sorted(values)
    if not xs:
        raise ValueError("percentile of an empty sequence")
    if not 0 <= p <= 100:
        raise ValueError("p must be between 0 and 100")
    rank = (p / 100) * (len(xs) - 1)
    lo = math.floor(rank)
    hi = math.ceil(rank)
    if lo == hi:
        return xs[lo]
    return xs[lo] + (xs[hi] - xs[lo]) * (rank - lo)


def summarise(latencies_s: Sequence[float], errors: int = 0) -> dict:
    """p50/p95/mean/min/max in milliseconds, plus counts."""
    ok = len(latencies_s)
    out: dict = {"n": ok + errors, "ok": ok, "errors": errors}
    if ok:
        ms = [v * 1000 for v in latencies_s]
        out.update(
            p50_ms=round(percentile(ms, 50), 1),
            p95_ms=round(percentile(ms, 95), 1),
            mean_ms=round(sum(ms) / ok, 1),
            min_ms=round(min(ms), 1),
            max_ms=round(max(ms), 1),
        )
    else:
        out.update(p50_ms=None, p95_ms=None, mean_ms=None, min_ms=None, max_ms=None)
    return out


COLUMNS = (
    ("target", "target", "<"),
    ("metric", "metric", "<"),
    ("concurrency", "conc", ">"),
    ("n", "n", ">"),
    ("errors", "err", ">"),
    ("p50_ms", "p50 ms", ">"),
    ("p95_ms", "p95 ms", ">"),
    ("mean_ms", "mean ms", ">"),
    ("max_ms", "max ms", ">"),
)


def format_table(rows: Sequence[dict]) -> str:
    """Plain-text table, one row per (target, metric, concurrency)."""

    def cell(v) -> str:
        if v is None:
            return "-"
        if isinstance(v, float):
            return f"{v:.1f}"
        return str(v)

    body = [[cell(r.get(key)) for key, _, _ in COLUMNS] for r in rows]
    widths = [
        max(len(title), *(len(b[i]) for b in body)) if body else len(title)
        for i, (_, title, _) in enumerate(COLUMNS)
    ]

    def line(cells: Sequence[str]) -> str:
        parts = []
        for (_, _, align), w, c in zip(COLUMNS, widths, cells):
            parts.append(f"{c:{align}{w}}")
        return "  ".join(parts).rstrip()

    out = [line([t for _, t, _ in COLUMNS]), line(["-" * w for w in widths])]
    out += [line(b) for b in body]
    return "\n".join(out)
