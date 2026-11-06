"""Tests for non-ASCII X-API-Key headers on the store API."""

from typing import Any, Dict

from fastapi.testclient import TestClient

from store.api import create_app
from store.engine import TimeSeriesStore

ADMIN_KEY = "admin-key-0123456789"
KEY_A = "key-a-0123456789abcdef"
NON_ASCII_HEADER: Dict[str, Any] = {"X-API-Key": "caf\u00e9".encode("utf-8")}

SAMPLE_POINT = {
    "name": "page_load_ms",
    "value": 100.0,
    "ts": "2026-10-15T00:00:00Z",
    "tags": {"site": "site-a", "client": "acme"},
}

QUERY_PARAMS = {
    "name": "page_load_ms",
    "from": "2026-10-15T00:00:00Z",
    "to": "2026-10-15T01:00:00Z",
}


def test_nonascii_header_with_admin_key_returns_401() -> None:
    client = TestClient(create_app(TimeSeriesStore(), api_key=ADMIN_KEY))

    r_ingest = client.post(
        "/ingest", json={"points": [SAMPLE_POINT]}, headers=NON_ASCII_HEADER
    )
    assert r_ingest.status_code == 401
    assert r_ingest.json() == {"error": "unauthorized"}

    r_query = client.get("/query", params=QUERY_PARAMS, headers=NON_ASCII_HEADER)
    assert r_query.status_code == 401
    assert r_query.json() == {"error": "unauthorized"}

    r_flags = client.get("/flags", params=QUERY_PARAMS, headers=NON_ASCII_HEADER)
    assert r_flags.status_code == 401
    assert r_flags.json() == {"error": "unauthorized"}


def test_nonascii_header_with_only_project_keys_returns_401() -> None:
    app = create_app(
        TimeSeriesStore(),
        api_key=None,
        project_keys={"site-a": KEY_A},
    )
    client = TestClient(app)

    r_ingest = client.post(
        "/ingest", json={"points": [SAMPLE_POINT]}, headers=NON_ASCII_HEADER
    )
    assert r_ingest.status_code == 401
    assert r_ingest.json() == {"error": "unauthorized"}

    r_query = client.get("/query", params=QUERY_PARAMS, headers=NON_ASCII_HEADER)
    assert r_query.status_code == 401
    assert r_query.json() == {"error": "unauthorized"}

    r_flags = client.get("/flags", params=QUERY_PARAMS, headers=NON_ASCII_HEADER)
    assert r_flags.status_code == 401
    assert r_flags.json() == {"error": "unauthorized"}


def test_valid_ascii_keys_still_work() -> None:
    app = create_app(
        TimeSeriesStore(),
        api_key=ADMIN_KEY,
        project_keys={"site-a": KEY_A},
    )
    client = TestClient(app)

    r_ingest = client.post(
        "/ingest",
        json={"points": [SAMPLE_POINT]},
        headers={"X-API-Key": KEY_A},
    )
    assert r_ingest.status_code == 200
    assert r_ingest.json() == {"accepted": 1}

    r_query = client.get(
        "/query",
        params=QUERY_PARAMS,
        headers={"X-API-Key": ADMIN_KEY},
    )
    assert r_query.status_code == 200
    assert len(r_query.json()["points"]) == 1


def test_nonascii_prefix_followed_by_valid_key_is_rejected_with_401() -> None:
    app = create_app(
        TimeSeriesStore(),
        api_key=ADMIN_KEY,
        project_keys={"site-a": KEY_A},
    )
    client = TestClient(app)

    for key_text in (ADMIN_KEY, KEY_A):
        prefixed_header: Dict[str, Any] = {
            "X-API-Key": f"\u00e9{key_text}".encode("utf-8")
        }
        r_ingest = client.post(
            "/ingest", json={"points": [SAMPLE_POINT]}, headers=prefixed_header
        )
        assert r_ingest.status_code == 401
        assert r_ingest.json() == {"error": "unauthorized"}

        r_query = client.get("/query", params=QUERY_PARAMS, headers=prefixed_header)
        assert r_query.status_code == 401
        assert r_query.json() == {"error": "unauthorized"}

        r_flags = client.get("/flags", params=QUERY_PARAMS, headers=prefixed_header)
        assert r_flags.status_code == 401
        assert r_flags.json() == {"error": "unauthorized"}


def test_401_response_body_never_contains_configured_keys() -> None:
    app = create_app(
        TimeSeriesStore(),
        api_key=ADMIN_KEY,
        project_keys={"site-a": KEY_A},
    )
    client = TestClient(app)

    for resp in (
        client.post("/ingest", json={"points": [SAMPLE_POINT]}, headers=NON_ASCII_HEADER),
        client.get("/query", params=QUERY_PARAMS, headers=NON_ASCII_HEADER),
        client.get("/flags", params=QUERY_PARAMS, headers=NON_ASCII_HEADER),
    ):
        assert resp.status_code == 401
        assert ADMIN_KEY not in resp.text
        assert KEY_A not in resp.text

