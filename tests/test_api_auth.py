"""Tests for store API key authentication."""

import pytest
from fastapi.testclient import TestClient
from store.api import create_app
from store.engine import TimeSeriesStore

SAMPLE_POINT = {
    "name": "page_load_ms",
    "value": 120,
    "ts": "2026-10-06T10:00:00Z",
    "tags": {"client": "acme", "env": "prod"},
}


def test_ingest_without_key_returns_401_and_stores_nothing() -> None:
    client = TestClient(create_app(TimeSeriesStore(), api_key="test-key-123"))
    resp = client.post("/ingest", json={"points": [SAMPLE_POINT]})
    assert resp.status_code == 401
    assert resp.json() == {"error": "unauthorized"}

    health = client.get("/health")
    assert health.status_code == 200
    assert health.json()["points"] == 0


def test_ingest_with_wrong_key_returns_401() -> None:
    client = TestClient(create_app(TimeSeriesStore(), api_key="test-key-123"))
    resp = client.post(
        "/ingest",
        json={"points": [SAMPLE_POINT]},
        headers={"X-API-Key": "wrong-key"},
    )
    assert resp.status_code == 401
    assert resp.json() == {"error": "unauthorized"}


def test_ingest_with_right_key_returns_200() -> None:
    client = TestClient(create_app(TimeSeriesStore(), api_key="test-key-123"))
    resp = client.post(
        "/ingest",
        json={"points": [SAMPLE_POINT]},
        headers={"X-API-Key": "test-key-123"},
    )
    assert resp.status_code == 200
    assert resp.json() == {"accepted": 1}


def test_query_requires_valid_key() -> None:
    client = TestClient(create_app(TimeSeriesStore(), api_key="test-key-123"))
    client.post(
        "/ingest",
        json={"points": [SAMPLE_POINT]},
        headers={"X-API-Key": "test-key-123"},
    )
    params = {
        "name": "page_load_ms",
        "from": "2026-10-06T00:00:00Z",
        "to": "2026-10-07T00:00:00Z",
    }

    resp_no_key = client.get("/query", params=params)
    assert resp_no_key.status_code == 401
    assert resp_no_key.json() == {"error": "unauthorized"}

    resp_wrong_key = client.get(
        "/query",
        params=params,
        headers={"X-API-Key": "wrong-key"},
    )
    assert resp_wrong_key.status_code == 401
    assert resp_wrong_key.json() == {"error": "unauthorized"}

    resp_right_key = client.get(
        "/query",
        params=params,
        headers={"X-API-Key": "test-key-123"},
    )
    assert resp_right_key.status_code == 200
    assert len(resp_right_key.json()["points"]) == 1


def test_health_works_without_key() -> None:
    client = TestClient(create_app(TimeSeriesStore(), api_key="test-key-123"))
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "points": 0}


def test_unauthorized_malformed_json_returns_401_not_400() -> None:
    client = TestClient(create_app(TimeSeriesStore(), api_key="test-key-123"))
    resp = client.post(
        "/ingest",
        content="{not-valid-json",
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 401
    assert resp.json() == {"error": "unauthorized"}


def test_no_key_configured_leaves_endpoints_open() -> None:
    client = TestClient(create_app(TimeSeriesStore(), api_key=None))
    resp_ingest = client.post("/ingest", json={"points": [SAMPLE_POINT]})
    assert resp_ingest.status_code == 200
    assert resp_ingest.json() == {"accepted": 1}

    resp_query = client.get(
        "/query",
        params={
            "name": "page_load_ms",
            "from": "2026-10-06T00:00:00Z",
            "to": "2026-10-07T00:00:00Z",
        },
    )
    assert resp_query.status_code == 200
    assert len(resp_query.json()["points"]) == 1


def test_key_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VIGIL_API_KEY", "env-key-456")
    client = TestClient(create_app(TimeSeriesStore()))

    resp_no_key = client.post("/ingest", json={"points": [SAMPLE_POINT]})
    assert resp_no_key.status_code == 401
    assert resp_no_key.json() == {"error": "unauthorized"}

    resp_with_key = client.post(
        "/ingest",
        json={"points": [SAMPLE_POINT]},
        headers={"X-API-Key": "env-key-456"},
    )
    assert resp_with_key.status_code == 200
    assert resp_with_key.json() == {"accepted": 1}


def test_empty_env_key_means_open(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VIGIL_API_KEY", "")
    client = TestClient(create_app(TimeSeriesStore()))

    resp = client.post("/ingest", json={"points": [SAMPLE_POINT]})
    assert resp.status_code == 200
    assert resp.json() == {"accepted": 1}


def test_401_response_never_contains_key() -> None:
    secret = "test-key-123"
    client = TestClient(create_app(TimeSeriesStore(), api_key=secret))

    resp_missing = client.post("/ingest", json={"points": [SAMPLE_POINT]})
    assert resp_missing.status_code == 401
    assert secret not in resp_missing.text

    resp_wrong = client.get(
        "/query",
        params={
            "name": "page_load_ms",
            "from": "2026-10-06T00:00:00Z",
            "to": "2026-10-07T00:00:00Z",
        },
        headers={"X-API-Key": "wrong-key"},
    )
    assert resp_wrong.status_code == 401
    assert secret not in resp_wrong.text

