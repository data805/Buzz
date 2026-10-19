import pytest

from buzzbench.stats import format_table, percentile, summarise


def test_percentile_matches_numpy_linear():
    xs = list(range(1, 11))  # numpy.percentile(1..10, [50, 95]) == [5.5, 9.55]
    assert percentile(xs, 50) == pytest.approx(5.5)
    assert percentile(xs, 95) == pytest.approx(9.55)


def test_percentile_ignores_input_order():
    assert percentile([10, 1, 7, 3], 50) == pytest.approx(5.0)


def test_percentile_ends_and_single_value():
    xs = [3.0, 9.0, 4.0]
    assert percentile(xs, 0) == 3.0
    assert percentile(xs, 100) == 9.0
    assert percentile([42.0], 95) == 42.0


def test_percentile_exact_rank():
    # n=21: rank for p95 is 19.0 exactly, so no interpolation.
    assert percentile(range(21), 95) == 19


@pytest.mark.parametrize("bad", [-1, 101])
def test_percentile_rejects_bad_p(bad):
    with pytest.raises(ValueError):
        percentile([1, 2], bad)


def test_percentile_rejects_empty():
    with pytest.raises(ValueError):
        percentile([], 50)


def test_summarise_converts_to_ms_and_counts_errors():
    s = summarise([0.1, 0.2, 0.3, 0.4], errors=2)
    assert s["n"] == 6 and s["ok"] == 4 and s["errors"] == 2
    assert s["p50_ms"] == pytest.approx(250.0)
    assert s["p95_ms"] == pytest.approx(385.0)
    assert s["mean_ms"] == pytest.approx(250.0)
    assert s["min_ms"] == pytest.approx(100.0)
    assert s["max_ms"] == pytest.approx(400.0)


def test_summarise_all_failed():
    s = summarise([], errors=3)
    assert s["ok"] == 0 and s["p50_ms"] is None


def test_format_table_has_header_and_rows():
    rows = [
        {"target": "llm", "metric": "ttft", "concurrency": 1, **summarise([0.05, 0.07])},
        {"target": "stt", "metric": "latency", "concurrency": 10, **summarise([], 1)},
    ]
    out = format_table(rows).splitlines()
    assert out[0].split()[:3] == ["target", "metric", "conc"]
    assert len(out) == 4
    assert "llm" in out[2] and "60.0" in out[2]
    assert out[3].rstrip().endswith("-")
