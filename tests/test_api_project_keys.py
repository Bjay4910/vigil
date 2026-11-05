from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

import pytest
from fastapi.testclient import TestClient

from store.api import create_app, parse_project_keys
from store.engine import TimeSeriesStore

ADMIN_KEY = "admin-key-0123456789"
KEY_A = "key-a-0123456789abcdef"
KEY_B = "key-b-0123456789abcdef"


def _make_client() -> TestClient:
    app = create_app(
        TimeSeriesStore(),
        api_key=ADMIN_KEY,
        project_keys={"site-a": KEY_A, "site-b": KEY_B},
    )
    return TestClient(app)


def _point(
    site: str = "site-a",
    value: float = 100.0,
    ts: str = "2026-10-15T00:00:00Z",
) -> Dict[str, Any]:
    return {
        "name": "page_load_ms",
        "value": value,
        "ts": ts,
        "tags": {"site": site, "client": "acme"},
    }


def test_parse_project_keys() -> None:
    assert parse_project_keys(None) == {}
    assert parse_project_keys("") == {}
    assert parse_project_keys("   ") == {}

    parsed = parse_project_keys(
        f"site-a:{KEY_A}, site-b:{KEY_B}",
        admin_key=ADMIN_KEY,
    )
    assert parsed == {"site-a": KEY_A, "site-b": KEY_B}

    # Key containing colons is split only on the first colon
    colon_key = "key-a:with:colons:0123456789"
    assert parse_project_keys(f"site-a:{colon_key}") == {"site-a": colon_key}

    short_secret = "short-secret-15"
    invalid_cases = [
        (f"site-a-{KEY_A}", KEY_A),                  # missing colon
        (f":{KEY_A}", KEY_A),                        # empty site
        ("site-a:", "site-a"),                       # empty key
        (f"Site-A:{KEY_A}", KEY_A),                  # uppercase site
        (f"site-a:{short_secret}", short_secret),    # key shorter than 16 chars
        (f"site-a:{KEY_A},site-a:{KEY_B}", KEY_A),   # duplicate site
        (f"site-a:{KEY_A},site-b:{KEY_A}", KEY_A),   # duplicate key
        (f"site-a:{ADMIN_KEY}", ADMIN_KEY),          # key equal to admin key
    ]

    for raw, secret_not_to_leak in invalid_cases:
        with pytest.raises(ValueError) as exc_info:
            parse_project_keys(raw, admin_key=ADMIN_KEY)
        msg = str(exc_info.value)
        assert msg.startswith("VIGIL_PROJECT_KEYS:")
        if secret_not_to_leak != "site-a":
            assert secret_not_to_leak not in msg
        assert KEY_A not in msg
        assert KEY_B not in msg
        assert ADMIN_KEY not in msg


def test_project_key_can_ingest_own_site() -> None:
    client = _make_client()
    resp = client.post(
        "/ingest",
        json={"points": [_point(site="site-a")]},
        headers={"X-API-Key": KEY_A},
    )
    assert resp.status_code == 200
    assert resp.json() == {"accepted": 1}


def test_project_key_cannot_ingest_other_site() -> None:
    client = _make_client()
    resp = client.post(
        "/ingest",
        json={"points": [_point(site="site-b")]},
        headers={"X-API-Key": KEY_A},
    )
    assert resp.status_code == 403
    assert resp.json() == {
        "error": "forbidden: this key can only write points tagged with its own site"
    }
    health = client.get("/health")
    assert health.status_code == 200
    assert health.json()["points"] == 0


def test_project_key_mixed_batch_rejected_atomically() -> None:
    client = _make_client()
    resp = client.post(
        "/ingest",
        json={
            "points": [
                _point(site="site-a", ts="2026-10-15T00:00:00Z"),
                _point(site="site-b", ts="2026-10-15T00:01:00Z"),
            ]
        },
        headers={"X-API-Key": KEY_A},
    )
    assert resp.status_code == 403
    assert resp.json() == {
        "error": "forbidden: this key can only write points tagged with its own site"
    }
    health = client.get("/health")
    assert health.status_code == 200
    assert health.json()["points"] == 0


def test_project_key_missing_tags_or_site_returns_403() -> None:
    client = _make_client()
    no_tags_point = {
        "name": "page_load_ms",
        "value": 100,
        "ts": "2026-10-15T00:00:00Z",
    }
    resp1 = client.post(
        "/ingest",
        json={"points": [no_tags_point]},
        headers={"X-API-Key": KEY_A},
    )
    assert resp1.status_code == 403
    assert resp1.json() == {
        "error": "forbidden: this key can only write points tagged with its own site"
    }

    no_site_point = {
        "name": "page_load_ms",
        "value": 100,
        "ts": "2026-10-15T00:00:00Z",
        "tags": {"client": "acme"},
    }
    resp2 = client.post(
        "/ingest",
        json={"points": [no_site_point]},
        headers={"X-API-Key": KEY_A},
    )
    assert resp2.status_code == 403
    assert resp2.json() == {
        "error": "forbidden: this key can only write points tagged with its own site"
    }


def test_project_key_query_scopes_to_own_site() -> None:
    client = _make_client()
    ingest_resp = client.post(
        "/ingest",
        json={
            "points": [
                _point(site="site-a", value=110, ts="2026-10-15T00:00:00Z"),
                _point(site="site-b", value=220, ts="2026-10-15T00:01:00Z"),
            ]
        },
        headers={"X-API-Key": ADMIN_KEY},
    )
    assert ingest_resp.status_code == 200

    query_resp = client.get(
        "/query",
        params={
            "name": "page_load_ms",
            "from": "2026-10-15T00:00:00Z",
            "to": "2026-10-15T01:00:00Z",
        },
        headers={"X-API-Key": KEY_A},
    )
    assert query_resp.status_code == 200
    pts = query_resp.json()["points"]
    assert len(pts) == 1
    assert pts[0]["tags"]["site"] == "site-a"
    assert pts[0]["value"] == 110


def test_project_key_query_other_site_returns_403_and_own_site_200() -> None:
    client = _make_client()
    client.post(
        "/ingest",
        json={"points": [_point(site="site-a"), _point(site="site-b")]},
        headers={"X-API-Key": ADMIN_KEY},
    )

    forbidden_resp = client.get(
        "/query",
        params={
            "name": "page_load_ms",
            "from": "2026-10-15T00:00:00Z",
            "to": "2026-10-15T01:00:00Z",
            "tags": "site:site-b",
        },
        headers={"X-API-Key": KEY_A},
    )
    assert forbidden_resp.status_code == 403
    assert forbidden_resp.json() == {
        "error": "forbidden: this key can only read its own site"
    }

    ok_resp = client.get(
        "/query",
        params={
            "name": "page_load_ms",
            "from": "2026-10-15T00:00:00Z",
            "to": "2026-10-15T01:00:00Z",
            "tags": "site:site-a",
        },
        headers={"X-API-Key": KEY_A},
    )
    assert ok_resp.status_code == 200
    pts = ok_resp.json()["points"]
    assert len(pts) == 1
    assert pts[0]["tags"]["site"] == "site-a"


def test_project_key_flags_scopes_to_own_site() -> None:
    client = _make_client()
    base = datetime(2026, 10, 15, 0, 0, 0, tzinfo=timezone.utc)
    points: List[Dict[str, Any]] = []
    for site in ("site-a", "site-b"):
        for i in range(120):
            ts = (base + timedelta(minutes=i)).strftime("%Y-%m-%dT%H:%M:%SZ")
            points.append(_point(site=site, value=100.0 + (i % 5), ts=ts))
        spike_ts = (base + timedelta(minutes=120)).strftime("%Y-%m-%dT%H:%M:%SZ")
        points.append(_point(site=site, value=1000.0, ts=spike_ts))

    ingest_resp = client.post(
        "/ingest",
        json={"points": points},
        headers={"X-API-Key": ADMIN_KEY},
    )
    assert ingest_resp.status_code == 200

    flags_resp = client.get(
        "/flags",
        params={
            "name": "page_load_ms",
            "from": "2026-10-15T00:00:00Z",
            "to": "2026-10-15T03:00:00Z",
        },
        headers={"X-API-Key": KEY_A},
    )
    assert flags_resp.status_code == 200
    flags = flags_resp.json()["flags"]
    assert len(flags) >= 1
    assert all(f["tags"]["site"] == "site-a" for f in flags)

    forbidden_flags = client.get(
        "/flags",
        params={
            "name": "page_load_ms",
            "from": "2026-10-15T00:00:00Z",
            "to": "2026-10-15T03:00:00Z",
            "tags": "site:site-b",
        },
        headers={"X-API-Key": KEY_A},
    )
    assert forbidden_flags.status_code == 403
    assert forbidden_flags.json() == {
        "error": "forbidden: this key can only read its own site"
    }


def test_admin_key_reads_and_writes_all_sites() -> None:
    client = _make_client()
    resp = client.post(
        "/ingest",
        json={
            "points": [
                _point(site="site-a", value=100, ts="2026-10-15T00:00:00Z"),
                _point(site="site-b", value=200, ts="2026-10-15T00:01:00Z"),
            ]
        },
        headers={"X-API-Key": ADMIN_KEY},
    )
    assert resp.status_code == 200
    assert resp.json() == {"accepted": 2}

    q = client.get(
        "/query",
        params={
            "name": "page_load_ms",
            "from": "2026-10-15T00:00:00Z",
            "to": "2026-10-15T01:00:00Z",
        },
        headers={"X-API-Key": ADMIN_KEY},
    )
    assert q.status_code == 200
    sites = {p["tags"]["site"] for p in q.json()["points"]}
    assert sites == {"site-a", "site-b"}


def test_missing_and_unknown_key_return_401_before_parsing() -> None:
    client = _make_client()
    for headers in ({}, {"X-API-Key": "unknown-key-0123456789abcdef"}):
        r_ingest = client.post(
            "/ingest", json={"points": [_point()]}, headers=headers
        )
        assert r_ingest.status_code == 401
        assert r_ingest.json() == {"error": "unauthorized"}

        r_query = client.get(
            "/query",
            params={
                "name": "page_load_ms",
                "from": "2026-10-15T00:00:00Z",
                "to": "2026-10-15T01:00:00Z",
            },
            headers=headers,
        )
        assert r_query.status_code == 401
        assert r_query.json() == {"error": "unauthorized"}

        r_flags = client.get(
            "/flags",
            params={
                "name": "page_load_ms",
                "from": "2026-10-15T00:00:00Z",
                "to": "2026-10-15T01:00:00Z",
            },
            headers=headers,
        )
        assert r_flags.status_code == 401
        assert r_flags.json() == {"error": "unauthorized"}

    # Malformed JSON body with unknown key must return 401 before body parsing
    bad_json_resp = client.post(
        "/ingest",
        content=b"{not valid json",
        headers={
            "Content-Type": "application/json",
            "X-API-Key": "unknown-key-0123456789abcdef",
        },
    )
    assert bad_json_resp.status_code == 401
    assert bad_json_resp.json() == {"error": "unauthorized"}


def test_key_revocation() -> None:
    app = create_app(
        TimeSeriesStore(),
        api_key=ADMIN_KEY,
        project_keys={"site-a": KEY_A},
    )
    client = TestClient(app)

    revoked_resp = client.post(
        "/ingest",
        json={"points": [_point(site="site-b")]},
        headers={"X-API-Key": KEY_B},
    )
    assert revoked_resp.status_code == 401

    active_resp = client.post(
        "/ingest",
        json={"points": [_point(site="site-a")]},
        headers={"X-API-Key": KEY_A},
    )
    assert active_resp.status_code == 200


def test_health_and_dashboard_open_when_keys_configured() -> None:
    client = _make_client()
    assert client.get("/health").status_code == 200
    assert client.get("/dashboard").status_code == 200


def test_open_mode_when_no_keys_configured() -> None:
    client = TestClient(create_app(TimeSeriesStore()))
    r1 = client.post("/ingest", json={"points": [_point(site="site-a")]})
    assert r1.status_code == 200
    r2 = client.get(
        "/query",
        params={
            "name": "page_load_ms",
            "from": "2026-10-15T00:00:00Z",
            "to": "2026-10-15T01:00:00Z",
        },
    )
    assert r2.status_code == 200
    assert len(r2.json()["points"]) == 1


def test_only_project_keys_configured_no_admin_key() -> None:
    app = create_app(
        TimeSeriesStore(),
        api_key=None,
        project_keys={"site-a": KEY_A},
    )
    client = TestClient(app)

    no_key_resp = client.post("/ingest", json={"points": [_point(site="site-a")]})
    assert no_key_resp.status_code == 401

    ok_resp = client.post(
        "/ingest",
        json={"points": [_point(site="site-a")]},
        headers={"X-API-Key": KEY_A},
    )
    assert ok_resp.status_code == 200


def test_project_keys_from_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VIGIL_PROJECT_KEYS", f"site-a:{KEY_A}")
    client = TestClient(create_app(TimeSeriesStore()))

    unauth = client.post("/ingest", json={"points": [_point(site="site-a")]})
    assert unauth.status_code == 401

    auth = client.post(
        "/ingest",
        json={"points": [_point(site="site-a")]},
        headers={"X-API-Key": KEY_A},
    )
    assert auth.status_code == 200


def test_no_401_or_403_response_contains_configured_keys() -> None:
    client = _make_client()
    responses = [
        client.post("/ingest", json={"points": [_point(site="site-a")]}),
        client.post(
            "/ingest",
            json={"points": [_point(site="site-a")]},
            headers={"X-API-Key": "wrong-key-0123456789abcdef"},
        ),
        client.post(
            "/ingest",
            json={"points": [_point(site="site-b")]},
            headers={"X-API-Key": KEY_A},
        ),
        client.get(
            "/query",
            params={
                "name": "page_load_ms",
                "from": "2026-10-15T00:00:00Z",
                "to": "2026-10-15T01:00:00Z",
                "tags": "site:site-b",
            },
            headers={"X-API-Key": KEY_A},
        ),
        client.get(
            "/flags",
            params={
                "name": "page_load_ms",
                "from": "2026-10-15T00:00:00Z",
                "to": "2026-10-15T01:00:00Z",
                "tags": "site:site-b",
            },
            headers={"X-API-Key": KEY_A},
        ),
    ]
    for resp in responses:
        assert resp.status_code in (401, 403)
        body_text = resp.text
        for secret in (ADMIN_KEY, KEY_A, KEY_B):
            assert secret not in body_text

