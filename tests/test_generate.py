"""Tests for the synthetic metric generator."""

import statistics

from fastapi.testclient import TestClient

from detection.generate import batches, generate_series
from store.api import create_app
from store.engine import TimeSeriesStore, validate_point

START = "2026-10-06T00:00:00Z"
TAGS = {"client": "acme", "env": "prod"}


def test_same_seed_gives_same_data():
    a = generate_series("page_load_ms", TAGS, START, 200, seed=7)
    b = generate_series("page_load_ms", TAGS, START, 200, seed=7)
    assert a == b


def test_every_point_passes_store_validation():
    points, _ = generate_series("page_load_ms", TAGS, START, 200)
    for p in points:
        validate_point(p)


def test_spikes_are_counted_and_outside_warmup():
    points, spikes = generate_series("page_load_ms", TAGS, START, 200, n_spikes=3, warmup=60)
    assert len(spikes) == 3
    first_allowed = points[60]["ts"]
    assert all(ts >= first_allowed for ts in spikes)


def test_spikes_are_much_larger_than_normal_points():
    points, spikes = generate_series("page_load_ms", TAGS, START, 500)
    spike_set = set(spikes)
    normal = [p["value"] for p in points if p["ts"] not in spike_set]
    spiked = [p["value"] for p in points if p["ts"] in spike_set]
    assert min(spiked) > statistics.mean(normal) * 2


def test_batches_split_without_losing_points():
    chunks = list(batches(list(range(1201)), size=500))
    assert [len(c) for c in chunks] == [500, 500, 201]


def test_generated_points_round_trip_through_api():
    client = TestClient(create_app(TimeSeriesStore()))
    points, _ = generate_series("page_load_ms", TAGS, START, 120)
    resp = client.post("/ingest", json={"points": points})
    assert resp.json() == {"accepted": 120}

    resp = client.get("/query", params={
        "name": "page_load_ms", "from": START, "to": "2026-10-07T00:00:00Z",
        "tags": "client:acme,env:prod",
    })
    assert len(resp.json()["points"]) == 120
