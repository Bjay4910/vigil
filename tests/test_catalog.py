"""Tests for TimeSeriesStore.catalog and GET /catalog."""

from fastapi.testclient import TestClient

from store.api import create_app
from store.engine import TimeSeriesStore

ADMIN_KEY = "admin-key-0123456789"
KEY_A = "key-a-0123456789abcdef"
KEY_B = "key-b-0123456789abcdef"


def _seed_store() -> TimeSeriesStore:
    store = TimeSeriesStore()
    store.write([
        {
            "name": "uptime_up",
            "value": 1,
            "ts": "2026-10-15T00:00:00Z",
            "tags": {"site": "site-a", "env": "prod", "service": "uptime", "client": "acme"},
        },
        {
            "name": "uptime_up",
            "value": 1,
            "ts": "2026-10-15T00:05:00Z",
            "tags": {"site": "site-a", "env": "prod", "service": "uptime", "client": "acme"},
        },
        {
            "name": "page_load_ms",
            "value": 240,
            "ts": "2026-10-15T00:02:00Z",
            "tags": {"site": "site-a", "env": "staging", "service": "web", "client": "acme"},
        },
        {
            "name": "uptime_up",
            "value": 1,
            "ts": "2026-10-15T00:10:00Z",
            "tags": {"site": "site-b", "env": "prod", "service": "uptime", "client": "acme"},
        },
        {
            "name": "api_response_ms",
            "value": 85,
            "ts": "2026-10-15T00:03:00Z",
            "tags": {"env": "prod", "client": "acme"},
        },
    ])
    return store


def test_store_catalog_returns_sorted_series_and_empty_for_empty_store() -> None:
    empty_store = TimeSeriesStore()
    assert empty_store.catalog() == []

    store = TimeSeriesStore()
    store.write([
        {
            "name": "uptime_up",
            "value": 1,
            "ts": "2026-10-15T00:00:00Z",
            "tags": {"site": "site-b", "env": "prod"},
        },
        {
            "name": "page_load_ms",
            "value": 120,
            "ts": "2026-10-15T00:01:00Z",
            "tags": {"site": "site-a", "env": "prod"},
        },
        {
            "name": "page_load_ms",
            "value": 130,
            "ts": "2026-10-15T00:04:00Z",
            "tags": {"site": "site-a", "env": "prod"},
        },
        {
            "name": "page_load_ms",
            "value": 150,
            "ts": "2026-10-15T00:02:00Z",
            "tags": {"site": "site-a", "env": "staging"},
        },
    ])

    cat = store.catalog()
    assert cat == [
        {
            "name": "page_load_ms",
            "tags": {"env": "prod", "site": "site-a"},
            "points": 2,
            "last_ts": "2026-10-15T00:04:00Z",
        },
        {
            "name": "page_load_ms",
            "tags": {"env": "staging", "site": "site-a"},
            "points": 1,
            "last_ts": "2026-10-15T00:02:00Z",
        },
        {
            "name": "uptime_up",
            "tags": {"env": "prod", "site": "site-b"},
            "points": 1,
            "last_ts": "2026-10-15T00:00:00Z",
        },
    ]


def test_catalog_endpoint_open_mode_groups_sites_and_excludes_client_tags() -> None:
    store = _seed_store()
    # Add a second env/service for uptime_up on site-a to test sorting across series
    store.write([
        {
            "name": "uptime_up",
            "value": 1,
            "ts": "2026-10-15T00:01:00Z",
            "tags": {"site": "site-a", "env": "dev", "service": "api", "client": "acme"},
        }
    ])
    client = TestClient(create_app(store))
    resp = client.get("/catalog")
    assert resp.status_code == 200
    body = resp.json()

    assert body == {
        "sites": [
            {
                "site": "(untagged)",
                "metrics": [
                    {
                        "name": "api_response_ms",
                        "envs": ["prod"],
                        "services": [],
                        "last_ts": "2026-10-15T00:03:00Z",
                    }
                ],
            },
            {
                "site": "site-a",
                "metrics": [
                    {
                        "name": "page_load_ms",
                        "envs": ["staging"],
                        "services": ["web"],
                        "last_ts": "2026-10-15T00:02:00Z",
                    },
                    {
                        "name": "uptime_up",
                        "envs": ["dev", "prod"],
                        "services": ["api", "uptime"],
                        "last_ts": "2026-10-15T00:05:00Z",
                    },
                ],
            },
            {
                "site": "site-b",
                "metrics": [
                    {
                        "name": "uptime_up",
                        "envs": ["prod"],
                        "services": ["uptime"],
                        "last_ts": "2026-10-15T00:10:00Z",
                    }
                ],
            },
        ]
    }
    assert "acme" not in resp.text
    assert "client" not in resp.text


def test_catalog_auth_admin_and_project_key_scoping_and_no_key_leak() -> None:
    store = _seed_store()
    app = create_app(
        store,
        api_key=ADMIN_KEY,
        project_keys={"site-a": KEY_A, "site-b": KEY_B},
    )
    client = TestClient(app)

    unauth = client.get("/catalog")
    assert unauth.status_code == 401
    assert unauth.json() == {"error": "unauthorized"}

    wrong = client.get("/catalog", headers={"X-API-Key": "wrong-key-0123456789abcdef"})
    assert wrong.status_code == 401
    assert wrong.json() == {"error": "unauthorized"}

    admin_resp = client.get("/catalog", headers={"X-API-Key": ADMIN_KEY})
    assert admin_resp.status_code == 200
    admin_sites = [s["site"] for s in admin_resp.json()["sites"]]
    assert admin_sites == ["(untagged)", "site-a", "site-b"]

    proj_a_resp = client.get("/catalog", headers={"X-API-Key": KEY_A})
    assert proj_a_resp.status_code == 200
    proj_a_sites = [s["site"] for s in proj_a_resp.json()["sites"]]
    assert proj_a_sites == ["site-a"]
    assert "(untagged)" not in proj_a_resp.text

    for resp in (unauth, wrong, admin_resp, proj_a_resp):
        for secret in (ADMIN_KEY, KEY_A, KEY_B):
            assert secret not in resp.text


def test_dashboard_contains_datalist_binary_and_no_innerhtml_or_localstorage() -> None:
    client = TestClient(create_app(TimeSeriesStore()))
    resp = client.get("/dashboard")
    assert resp.status_code == 200
    html = resp.text
    assert "<datalist" in html
    assert 'value="binary"' in html
    assert "innerHTML" not in html
    assert "localStorage" not in html

