"""Tests for FastAPI time-series store API."""

from fastapi.testclient import TestClient
from store.api import create_app
from store.engine import TimeSeriesStore


def test_ingest_two_valid_points():
    client = TestClient(create_app(TimeSeriesStore()))
    points = [
        {"name": "page_load_ms", "value": 100, "ts": "2026-10-06T10:00:00Z"},
        {"name": "page_load_ms", "value": 200, "ts": "2026-10-06T11:00:00Z"},
    ]
    resp = client.post("/ingest", json={"points": points})
    assert resp.status_code == 200
    assert resp.json() == {"accepted": 2}


def test_points_out_of_order_sorted_and_range():
    client = TestClient(create_app(TimeSeriesStore()))
    points = [
        {"name": "page_load_ms", "value": 300, "ts": "2026-10-06T12:00:00Z"},
        {"name": "page_load_ms", "value": 100, "ts": "2026-10-06T10:00:00Z"},
        {"name": "page_load_ms", "value": 200, "ts": "2026-10-06T11:00:00Z"},
    ]
    resp = client.post("/ingest", json={"points": points})
    assert resp.status_code == 200

    resp = client.get(
        "/query",
        params={
            "name": "page_load_ms",
            "from": "2026-10-06T10:00:00Z",
            "to": "2026-10-06T12:00:00Z",
        },
    )
    assert resp.status_code == 200
    data = resp.json()["points"]
    assert len(data) == 2
    assert [p["ts"] for p in data] == [
        "2026-10-06T10:00:00Z",
        "2026-10-06T11:00:00Z",
    ]
    assert [p["value"] for p in data] == [100.0, 200.0]


def test_tags_subset_match():
    client = TestClient(create_app(TimeSeriesStore()))
    points = [
        {
            "name": "page_load_ms",
            "value": 150,
            "ts": "2026-10-06T10:00:00Z",
            "tags": {"client": "acme", "env": "prod"},
        },
        {
            "name": "page_load_ms",
            "value": 250,
            "ts": "2026-10-06T11:00:00Z",
            "tags": {"client": "globex", "env": "prod"},
        },
    ]
    resp = client.post("/ingest", json={"points": points})
    assert resp.status_code == 200

    resp = client.get(
        "/query",
        params={
            "name": "page_load_ms",
            "from": "2026-10-06T00:00:00Z",
            "to": "2026-10-07T00:00:00Z",
            "tags": "client:acme",
        },
    )
    assert resp.status_code == 200
    pts = resp.json()["points"]
    assert len(pts) == 1
    assert pts[0]["value"] == 150.0
    assert pts[0]["tags"] == {"client": "acme", "env": "prod"}


def test_query_without_tags_returns_all_tag_sets():
    client = TestClient(create_app(TimeSeriesStore()))
    points = [
        {
            "name": "page_load_ms",
            "value": 100,
            "ts": "2026-10-06T10:00:00Z",
            "tags": {"client": "acme"},
        },
        {
            "name": "page_load_ms",
            "value": 200,
            "ts": "2026-10-06T11:00:00Z",
            "tags": {"client": "globex"},
        },
    ]
    client.post("/ingest", json={"points": points})

    resp = client.get(
        "/query",
        params={
            "name": "page_load_ms",
            "from": "2026-10-06T00:00:00Z",
            "to": "2026-10-07T00:00:00Z",
        },
    )
    assert resp.status_code == 200
    pts = resp.json()["points"]
    assert len(pts) == 2


def test_unknown_metric_returns_empty_points():
    client = TestClient(create_app(TimeSeriesStore()))
    resp = client.get(
        "/query",
        params={
            "name": "unknown_metric",
            "from": "2026-10-06T00:00:00Z",
            "to": "2026-10-07T00:00:00Z",
        },
    )
    assert resp.status_code == 200
    assert resp.json() == {"points": []}


def test_invalid_point_returns_400_and_nothing_stored():
    client = TestClient(create_app(TimeSeriesStore()))
    resp = client.post(
        "/ingest",
        json={"points": [{"name": "Page_load_ms", "value": 100, "ts": "2026-10-06T10:00:00Z"}]},
    )
    assert resp.status_code == 400
    assert "name" in resp.json()["error"]

    health = client.get("/health")
    assert health.status_code == 200
    assert health.json()["points"] == 0


def test_batch_with_one_invalid_stores_nothing():
    client = TestClient(create_app(TimeSeriesStore()))
    batch = [
        {"name": "page_load_ms", "value": 100, "ts": "2026-10-06T10:00:00Z"},
        {"name": "INVALID_NAME", "value": 200, "ts": "2026-10-06T11:00:00Z"},
    ]
    resp = client.post("/ingest", json={"points": batch})
    assert resp.status_code == 400

    health = client.get("/health")
    assert health.status_code == 200
    assert health.json()["points"] == 0


def test_invalid_body_formats_return_400():
    client = TestClient(create_app(TimeSeriesStore()))

    resp = client.post(
        "/ingest",
        content="not-json",
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 400
    assert resp.json() == {"error": "body: must be valid JSON"}

    resp = client.post("/ingest", json={"other": 123})
    assert resp.status_code == 400
    assert resp.json() == {"error": "points: must be a list"}

    resp = client.post("/ingest", json={"points": "not-a-list"})
    assert resp.status_code == 400
    assert resp.json() == {"error": "points: must be a list"}


def test_missing_query_parameters_return_400():
    client = TestClient(create_app(TimeSeriesStore()))

    resp = client.get(
        "/query",
        params={"from": "2026-10-06T00:00:00Z", "to": "2026-10-07T00:00:00Z"},
    )
    assert resp.status_code == 400
    assert resp.json() == {"error": "name: required"}

    resp = client.get(
        "/query",
        params={"name": "page_load_ms", "to": "2026-10-07T00:00:00Z"},
    )
    assert resp.status_code == 400
    assert resp.json() == {"error": "from: required"}

    resp = client.get(
        "/query",
        params={"name": "page_load_ms", "from": "2026-10-06T00:00:00Z"},
    )
    assert resp.status_code == 400
    assert resp.json() == {"error": "to: required"}


def test_tags_without_colon_returns_400():
    client = TestClient(create_app(TimeSeriesStore()))
    resp = client.get(
        "/query",
        params={
            "name": "page_load_ms",
            "from": "2026-10-06T00:00:00Z",
            "to": "2026-10-07T00:00:00Z",
            "tags": "client",
        },
    )
    assert resp.status_code == 400
    assert resp.json()["error"].startswith("tags")


def test_bad_from_timestamp_returns_400():
    client = TestClient(create_app(TimeSeriesStore()))
    resp = client.get(
        "/query",
        params={
            "name": "page_load_ms",
            "from": "invalid-ts",
            "to": "2026-10-07T00:00:00Z",
        },
    )
    assert resp.status_code == 400


def test_health_returns_ok_and_point_count():
    client = TestClient(create_app(TimeSeriesStore()))
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "points": 0}

    client.post(
        "/ingest",
        json={"points": [{"name": "page_load_ms", "value": 100, "ts": "2026-10-06T10:00:00Z"}]},
    )
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "points": 1}


def test_persistence_across_apps(tmp_path):
    path = str(tmp_path / "persistence_store.jsonl")
    app1 = create_app(TimeSeriesStore(path=path))
    client1 = TestClient(app1)

    points = [
        {"name": "page_load_ms", "value": 100, "ts": "2026-10-06T10:00:00Z", "tags": {"client": "acme"}},
        {"name": "page_load_ms", "value": 200, "ts": "2026-10-06T11:00:00Z", "tags": {"client": "globex"}},
    ]
    resp1 = client1.post("/ingest", json={"points": points})
    assert resp1.status_code == 200
    assert resp1.json() == {"accepted": 2}

    app2 = create_app(TimeSeriesStore(path=path))
    client2 = TestClient(app2)

    query_params = {
        "name": "page_load_ms",
        "from": "2026-10-06T00:00:00Z",
        "to": "2026-10-07T00:00:00Z",
    }
    resp_q1 = client1.get("/query", params=query_params)
    resp_q2 = client2.get("/query", params=query_params)

    assert resp_q1.status_code == 200
    assert resp_q2.status_code == 200
    assert resp_q1.json() == resp_q2.json()
    assert len(resp_q2.json()["points"]) == 2
