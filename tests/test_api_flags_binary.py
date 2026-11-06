"""Tests for GET /flags with rule=binary."""

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

from fastapi.testclient import TestClient

from store.api import create_app
from store.engine import TimeSeriesStore

START = "2026-10-15T00:00:00Z"
END = "2026-10-15T03:00:00Z"
ADMIN_KEY = "admin-key-0123456789"
KEY_A = "key-a-0123456789abcdef"
KEY_B = "key-b-0123456789abcdef"


def _make_uptime_outage_points(site: str = "site-a") -> List[Dict[str, Any]]:
    base = datetime(2026, 10, 15, 0, 0, 0, tzinfo=timezone.utc)
    values = [1] * 30 + [0] * 10 + [1] * 20
    points: List[Dict[str, Any]] = []
    for i, val in enumerate(values):
        ts = (base + timedelta(minutes=i)).strftime("%Y-%m-%dT%H:%M:%SZ")
        points.append({
            "name": "uptime_up",
            "value": val,
            "ts": ts,
            "tags": {"site": site, "client": "acme"},
        })
    return points


def _make_spike_points() -> List[Dict[str, Any]]:
    base = datetime(2026, 10, 15, 0, 0, 0, tzinfo=timezone.utc)
    points: List[Dict[str, Any]] = []
    for i in range(120):
        ts = (base + timedelta(minutes=i)).strftime("%Y-%m-%dT%H:%M:%SZ")
        points.append({
            "name": "page_load_ms",
            "value": 100 + (i % 5),
            "ts": ts,
            "tags": {"site": "site-a", "client": "acme"},
        })
    spike_ts = (base + timedelta(minutes=120)).strftime("%Y-%m-%dT%H:%M:%SZ")
    points.append({
        "name": "page_load_ms",
        "value": 1000,
        "ts": spike_ts,
        "tags": {"site": "site-a", "client": "acme"},
    })
    return points


def test_binary_rule_returns_outage_flag() -> None:
    client = TestClient(create_app(TimeSeriesStore()))
    ingest_resp = client.post("/ingest", json={"points": _make_uptime_outage_points()})
    assert ingest_resp.status_code == 200

    resp = client.get(
        "/flags",
        params={"name": "uptime_up", "from": START, "to": END, "rule": "binary"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["rule"] == "binary"
    assert len(body["flags"]) == 1
    flag = body["flags"][0]
    assert flag["name"] == "uptime_up"
    assert flag["tags"] == {"site": "site-a", "client": "acme"}
    assert flag["ts"] == "2026-10-15T00:30:00Z"
    assert flag["value"] == 0.0
    assert flag["score"] == 10.0
    assert flag["rule"] == "binary"


def test_threshold_does_not_change_binary_result() -> None:
    client = TestClient(create_app(TimeSeriesStore()))
    client.post("/ingest", json={"points": _make_uptime_outage_points()})

    resp_1 = client.get(
        "/flags",
        params={
            "name": "uptime_up",
            "from": START,
            "to": END,
            "rule": "binary",
            "threshold": "1",
        },
    )
    resp_100 = client.get(
        "/flags",
        params={
            "name": "uptime_up",
            "from": START,
            "to": END,
            "rule": "binary",
            "threshold": "100",
        },
    )
    assert resp_1.status_code == 200
    assert resp_100.status_code == 200
    assert resp_1.json()["flags"] == resp_100.json()["flags"]
    assert len(resp_1.json()["flags"]) == 1


def test_binary_rule_on_page_load_ms_returns_empty_flags() -> None:
    client = TestClient(create_app(TimeSeriesStore()))
    client.post("/ingest", json={"points": _make_spike_points()})

    resp = client.get(
        "/flags",
        params={"name": "page_load_ms", "from": START, "to": END, "rule": "binary"},
    )
    assert resp.status_code == 200
    assert resp.json()["flags"] == []


def test_bogus_rule_returns_400_mentioning_binary() -> None:
    client = TestClient(create_app(TimeSeriesStore()))
    resp = client.get(
        "/flags",
        params={"name": "uptime_up", "from": START, "to": END, "rule": "bogus"},
    )
    assert resp.status_code == 400
    err = resp.json()["error"]
    assert err.startswith("rule")
    assert "binary" in err


def test_zscore_and_ewma_still_work_on_same_app() -> None:
    client = TestClient(create_app(TimeSeriesStore()))
    client.post("/ingest", json={"points": _make_spike_points()})

    for rule_name in ("zscore", "ewma"):
        resp = client.get(
            "/flags",
            params={"name": "page_load_ms", "from": START, "to": END, "rule": rule_name},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["rule"] == rule_name
        assert len(body["flags"]) == 1
        assert body["flags"][0]["rule"] == rule_name


def test_project_keys_scope_binary_flags_by_site() -> None:
    app = create_app(
        TimeSeriesStore(),
        api_key=ADMIN_KEY,
        project_keys={"site-a": KEY_A, "site-b": KEY_B},
    )
    client = TestClient(app)

    ingest_resp = client.post(
        "/ingest",
        json={"points": _make_uptime_outage_points(site="site-a")},
        headers={"X-API-Key": ADMIN_KEY},
    )
    assert ingest_resp.status_code == 200

    resp_b = client.get(
        "/flags",
        params={"name": "uptime_up", "from": START, "to": END, "rule": "binary"},
        headers={"X-API-Key": KEY_B},
    )
    assert resp_b.status_code == 200
    assert resp_b.json()["flags"] == []

    resp_a = client.get(
        "/flags",
        params={"name": "uptime_up", "from": START, "to": END, "rule": "binary"},
        headers={"X-API-Key": KEY_A},
    )
    assert resp_a.status_code == 200
    flags_a = resp_a.json()["flags"]
    assert len(flags_a) == 1
    assert flags_a[0]["tags"]["site"] == "site-a"
    assert flags_a[0]["score"] == 10.0


def test_dashboard_contains_binary_rule_option() -> None:
    client = TestClient(create_app(TimeSeriesStore()))
    resp = client.get("/dashboard")
    assert resp.status_code == 200
    assert 'value="binary"' in resp.text

