"""API integration tests for rate limiting, failed-auth lockout, and body/point caps."""

from typing import Dict, List

from fastapi.testclient import TestClient
import pytest

from store.api import create_app
from store.engine import TimeSeriesStore

SAMPLE_POINT: Dict[str, object] = {
    "name": "page_load_ms",
    "value": 120,
    "ts": "2026-10-06T10:00:00Z",
    "tags": {"site": "site-a", "env": "prod"},
}


def test_auth_fail_lockout_and_xff_cannot_bypass() -> None:
    now: List[float] = [1000.0]
    admin_key = "fake-admin-key-0123456789"
    app = create_app(
        TimeSeriesStore(),
        api_key=admin_key,
        auth_fail_limit=3,
        limiter_now=lambda: now[0],
    )
    client = TestClient(app)
    query_params = {
        "name": "page_load_ms",
        "from": "2026-10-06T00:00:00Z",
        "to": "2026-10-07T00:00:00Z",
    }

    # Three wrong keys from one Fly-Client-IP with rotating X-Forwarded-For headers
    for i in range(3):
        resp = client.get(
            "/query",
            params=query_params,
            headers={
                "Fly-Client-IP": "198.51.100.10",
                "X-Forwarded-For": f"203.0.113.{i + 1}",
                "X-API-Key": "wrong-key",
            },
        )
        assert resp.status_code == 401

    # Fourth request from the same Fly-Client-IP (even with correct key and new X-Forwarded-For) -> 429
    resp_locked = client.get(
        "/query",
        params=query_params,
        headers={
            "Fly-Client-IP": "198.51.100.10",
            "X-Forwarded-For": "203.0.113.99",
            "X-API-Key": admin_key,
        },
    )
    assert resp_locked.status_code == 429
    assert resp_locked.json() == {"error": "too many requests"}
    assert int(resp_locked.headers["Retry-After"]) >= 1
    assert admin_key not in resp_locked.text
    assert "wrong-key" not in resp_locked.text

    # A different Fly-Client-IP is not locked out
    resp_other_ip = client.get(
        "/query",
        params=query_params,
        headers={
            "Fly-Client-IP": "198.51.100.11",
            "X-API-Key": admin_key,
        },
    )
    assert resp_other_ip.status_code == 200

    # Advance fake clock past 600s lockout -> original Fly-Client-IP works again
    now[0] += 601.0
    resp_unlocked = client.get(
        "/query",
        params=query_params,
        headers={
            "Fly-Client-IP": "198.51.100.10",
            "X-API-Key": admin_key,
        },
    )
    assert resp_unlocked.status_code == 200


def test_request_rate_limit_per_ip_and_health_exempt() -> None:
    now: List[float] = [500.0]
    app = create_app(
        TimeSeriesStore(),
        rate_limit=5,
        limiter_now=lambda: now[0],
    )
    client = TestClient(app)
    query_params = {
        "name": "page_load_ms",
        "from": "2026-10-06T00:00:00Z",
        "to": "2026-10-07T00:00:00Z",
    }

    for _ in range(5):
        r = client.get(
            "/query",
            params=query_params,
            headers={"Fly-Client-IP": "198.51.100.20"},
        )
        assert r.status_code == 200

    # 6th request gets 429
    r6 = client.get(
        "/query",
        params=query_params,
        headers={"Fly-Client-IP": "198.51.100.20"},
    )
    assert r6.status_code == 429
    assert r6.json() == {"error": "too many requests"}
    assert "Retry-After" in r6.headers

    # /health is never limited even for the rate-limited IP
    for _ in range(10):
        rh = client.get("/health", headers={"Fly-Client-IP": "198.51.100.20"})
        assert rh.status_code == 200

    # A second Fly-Client-IP is unaffected
    r_other = client.get(
        "/query",
        params=query_params,
        headers={"Fly-Client-IP": "198.51.100.21"},
    )
    assert r_other.status_code == 200


def test_zero_rate_limit_and_zero_auth_fail_limit_disable_limiting() -> None:
    now: List[float] = [200.0]
    admin_key = "fake-admin-key-0123456789"
    app = create_app(
        TimeSeriesStore(),
        api_key=admin_key,
        rate_limit=0,
        auth_fail_limit=0,
        limiter_now=lambda: now[0],
    )
    client = TestClient(app)

    for _ in range(25):
        r = client.post(
            "/ingest",
            json={"points": [SAMPLE_POINT]},
            headers={"X-API-Key": "wrong-key"},
        )
        assert r.status_code == 401

    r_ok = client.post(
        "/ingest",
        json={"points": [SAMPLE_POINT]},
        headers={"X-API-Key": admin_key},
    )
    assert r_ok.status_code == 200


def test_ingest_body_size_and_content_length_checks() -> None:
    ts_store = TimeSeriesStore()
    app = create_app(ts_store, max_body_bytes=1000)
    client = TestClient(app)

    # 5000-byte body is rejected with 413 and stores nothing
    huge_payload = '{"points": [' + ",".join(
        '{"name":"page_load_ms","value":1,"ts":"2026-10-06T10:00:00Z"}'
        for _ in range(100)
    ) + "]}"
    assert len(huge_payload.encode("utf-8")) > 5000

    resp_huge = client.post(
        "/ingest",
        content=huge_payload,
        headers={"Content-Type": "application/json"},
    )
    assert resp_huge.status_code == 413
    assert resp_huge.json() == {"error": "body: too large (maximum 1000 bytes)"}
    assert len(ts_store) == 0

    # Content-Length claiming more than the cap is rejected with 413
    resp_cl_large = client.post(
        "/ingest",
        content='{"points": []}',
        headers={
            "Content-Type": "application/json",
            "Content-Length": "2500",
        },
    )
    assert resp_cl_large.status_code == 413
    assert resp_cl_large.json() == {"error": "body: too large (maximum 1000 bytes)"}

    # Non-numeric Content-Length returns 400
    resp_cl_bad = client.post(
        "/ingest",
        content='{"points": []}',
        headers={
            "Content-Type": "application/json",
            "Content-Length": "not-a-number",
        },
    )
    assert resp_cl_bad.status_code == 400
    assert resp_cl_bad.json() == {"error": "body: invalid Content-Length"}


def test_ingest_max_points_and_auth_precedence() -> None:
    ts_store = TimeSeriesStore()
    admin_key = "fake-admin-key-0123456789"
    app = create_app(
        ts_store,
        api_key=admin_key,
        max_body_bytes=1000,
        max_points=3,
    )
    client = TestClient(app)

    # Unauthorized huge body gets 401 not 413 (auth runs before reading body)
    huge_body = "x" * 5000
    resp_unauth = client.post(
        "/ingest",
        content=huge_body,
        headers={"Content-Type": "application/json"},
    )
    assert resp_unauth.status_code == 401
    assert resp_unauth.json() == {"error": "unauthorized"}

    # 4 valid points rejected with 413 and stores none
    four_pts = [
        {
            "name": "page_load_ms",
            "value": i,
            "ts": f"2026-10-06T10:00:0{i}Z",
            "tags": {"site": "site-a", "env": "prod"},
        }
        for i in range(4)
    ]
    resp_4 = client.post(
        "/ingest",
        json={"points": four_pts},
        headers={"X-API-Key": admin_key},
    )
    assert resp_4.status_code == 413
    assert resp_4.json() == {
        "error": "points: too many points in one request (maximum 3)"
    }
    assert len(ts_store) == 0

    # 3 valid points accepted
    resp_3 = client.post(
        "/ingest",
        json={"points": four_pts[:3]},
        headers={"X-API-Key": admin_key},
    )
    assert resp_3.status_code == 200
    assert resp_3.json() == {"accepted": 3}
    assert len(ts_store) == 3


def test_default_limits_preserve_normal_ingest_and_query() -> None:
    ts_store = TimeSeriesStore()
    client = TestClient(create_app(ts_store))

    r_in = client.post("/ingest", json={"points": [SAMPLE_POINT]})
    assert r_in.status_code == 200
    assert r_in.json() == {"accepted": 1}

    r_q = client.get(
        "/query",
        params={
            "name": "page_load_ms",
            "from": "2026-10-06T00:00:00Z",
            "to": "2026-10-07T00:00:00Z",
        },
    )
    assert r_q.status_code == 200
    assert len(r_q.json()["points"]) == 1

