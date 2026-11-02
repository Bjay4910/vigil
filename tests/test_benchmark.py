"""Unit tests for pure functions in benchmarks/bench.py."""

import importlib
import json
import math
from typing import Dict, List, Tuple

import pytest

from benchmarks.bench import batches, make_points, percentile, summarize
from store.engine import validate_point


def test_percentile_known_values_and_median() -> None:
    vals = [10.0, 20.0, 30.0, 40.0, 50.0]
    assert percentile(vals, 0) == 10.0
    assert percentile(vals, 50) == 30.0
    assert percentile(vals, 100) == 50.0
    assert percentile(vals, 25) == 20.0
    assert percentile(vals, 75) == 40.0

    even_vals = [10.0, 20.0, 30.0, 40.0]
    assert percentile(even_vals, 50) == 25.0


def test_make_points_count_validation_and_determinism() -> None:
    pts_a = make_points(1000, seed=1)
    assert len(pts_a) == 1000
    for p in pts_a:
        validate_point(p)

    pts_b = make_points(1000, seed=1)
    assert pts_a == pts_b

    pts_c = make_points(1000, seed=2)
    assert [p["value"] for p in pts_a] != [p["value"] for p in pts_c]


def test_make_points_ten_series_and_increasing_timestamps() -> None:
    pts = make_points(1000, seed=7)
    by_series: Dict[Tuple[str, str], List[str]] = {}
    for p in pts:
        key = (p["name"], json.dumps(p["tags"], sort_keys=True))
        by_series.setdefault(key, []).append(p["ts"])

    assert len(by_series) == 10
    for series_key, timestamps in by_series.items():
        assert len(timestamps) == 100, f"Expected 100 points in {series_key}"
        assert len(set(timestamps)) == len(timestamps)
        assert timestamps == sorted(timestamps)


def test_batches_splits_without_losing_points() -> None:
    pts = make_points(1050, seed=3)
    chunked = list(batches(pts, size=500))
    assert [len(c) for c in chunked] == [500, 500, 50]
    flattened = [p for chunk in chunked for p in chunk]
    assert flattened == pts


def test_summarize_returns_median_p95_and_max() -> None:
    # 21 values from 1.0 to 21.0: median (50th) is 11.0, 95th is 20.0, max is 21.0
    latencies = [float(i) for i in range(1, 22)]
    stats = summarize(latencies)
    assert math.isclose(stats["median"], 11.0)
    assert math.isclose(stats["p95"], 20.0)
    assert math.isclose(stats["max"], 21.0)


def test_importing_bench_does_not_perform_network_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import http.client

    def forbid_connect(*args: object, **kwargs: object) -> None:
        raise AssertionError("Network connection attempted on import")

    monkeypatch.setattr(http.client.HTTPConnection, "connect", forbid_connect)
    module = importlib.import_module("benchmarks.bench")
    importlib.reload(module)

