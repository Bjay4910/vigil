"""Tests for the z-score detector."""

import pandas as pd

from detection.detector import to_frame, zscore_flags

ACME = {"client": "acme", "env": "prod"}
STAGING = {"client": "acme", "env": "staging"}


def make_points(values, tags, start="2026-10-06T00:00:00Z"):
    """Build one point per minute from a list of values."""
    ts = pd.date_range(start, periods=len(values), freq="min", tz="UTC")
    return [
        {"name": "page_load_ms", "value": v, "ts": t.strftime("%Y-%m-%dT%H:%M:%SZ"), "tags": tags}
        for v, t in zip(values, ts)
    ]


def normal_values(n):
    """Steady values that wobble between 840 and 860."""
    return [850 + (10 if i % 2 else -10) for i in range(n)]


def test_to_frame_gives_one_series_key_per_tag_combo():
    df = to_frame(make_points([1, 2], ACME) + make_points([3], STAGING))
    assert df["series"].nunique() == 2
    assert str(df["ts"].dt.tz) == "UTC"


def test_flags_a_spike():
    values = normal_values(100)
    values[80] = 4000
    flags = zscore_flags(to_frame(make_points(values, ACME)))
    assert len(flags) == 1
    flag = flags[0]
    assert flag["value"] == 4000
    assert flag["rule"] == "zscore"
    assert flag["tags"] == ACME
    assert set(flag) == {"name", "tags", "ts", "value", "score", "rule"}


def test_no_flags_on_normal_data():
    assert zscore_flags(to_frame(make_points(normal_values(200), ACME))) == []


def test_spike_inside_warmup_is_not_scored():
    values = normal_values(100)
    values[30] = 4000
    assert zscore_flags(to_frame(make_points(values, ACME))) == []


def test_flat_window_does_not_divide_by_zero():
    values = [850] * 100
    values[80] = 4000
    assert zscore_flags(to_frame(make_points(values, ACME))) == []


def test_series_are_scored_separately():
    prod = normal_values(100)
    staging = [v - 600 for v in normal_values(100)]
    df = to_frame(make_points(prod, ACME) + make_points(staging, STAGING))
    assert zscore_flags(df) == []
