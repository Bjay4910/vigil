"""Tests for GET /flags endpoint on the store API."""

import json
from typing import Any, Dict, List

from fastapi.testclient import TestClient
from store.api import create_app
from store.engine import TimeSeriesStore, format_ts, parse_ts

START = "2026-10-15T00:00:00Z"
END = "2026-10-15T03:00:00Z"


def build_test_points(n_normal: int = 120, include_spike: bool = True) -> List[Dict[str, Any]]:
    """Build 1 point/min starting at 2026-10-15T00:00:00Z with value 100 + (i % 5), plus optional 1000 spike."""
    start_epoch = parse_ts(START)
    points: List[Dict[str, Any]] = []
    for i in range(n_normal):
        points.append({
            "name": "page_load_ms",
            "value": 100 + (i % 5),
            "ts": format_ts(start_epoch + i * 60),
            "tags": {"client": "acme"},
        })
    if include_spike:
        points.append({
            "name": "page_load_ms",
            "value": 1000,
            "ts": format_ts(start_epoch + n_normal * 60),
            "tags": {"client": "acme"},
        })
    return points


def test_zscore_returns_spike() -> None:
    store = TimeSeriesStore()
    store.write(build_test_points())
    client = TestClient(create_app(store))

    resp = client.get(
        "/flags",
        params={"name": "page_load_ms", "from": START, "to": END},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["rule"] == "zscore"
    assert body["threshold"] == 3.0
    assert len(body["flags"]) == 1

    flag = body["flags"][0]
    assert set(flag.keys()) == {"name", "tags", "ts", "value", "score", "rule"}
    assert flag["name"] == "page_load_ms"
    assert flag["tags"] == {"client": "acme"}
    assert flag["ts"] == "2026-10-15T02:00:00Z"
    assert flag["value"] == 1000.0
    assert flag["rule"] == "zscore"
    assert flag["score"] > 3.0


def test_ewma_returns_spike() -> None:
    store = TimeSeriesStore()
    store.write(build_test_points())
    client = TestClient(create_app(store))

    resp = client.get(
        "/flags",
        params={"name": "page_load_ms", "from": START, "to": END, "rule": "ewma"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["rule"] == "ewma"
    assert len(body["flags"]) == 1
    flag = body["flags"][0]
    assert flag["value"] == 1000.0
    assert flag["rule"] == "ewma"
    assert flag["ts"] == "2026-10-15T02:00:00Z"


def test_high_threshold_returns_empty_flags() -> None:
    store = TimeSeriesStore()
    store.write(build_test_points())
    client = TestClient(create_app(store))

    resp = client.get(
        "/flags",
        params={"name": "page_load_ms", "from": START, "to": END, "threshold": "1000"},
    )
    assert resp.status_code == 200
    assert resp.json()["flags"] == []


def test_unknown_metric_returns_200_and_empty_flags() -> None:
    store = TimeSeriesStore()
    store.write(build_test_points())
    client = TestClient(create_app(store))

    resp = client.get(
        "/flags",
        params={"name": "unknown_metric", "from": START, "to": END},
    )
    assert resp.status_code == 200
    assert resp.json()["flags"] == []


def test_fewer_points_than_window_returns_empty_flags() -> None:
    store = TimeSeriesStore()
    store.write(build_test_points(n_normal=10, include_spike=False))
    client = TestClient(create_app(store))

    resp = client.get(
        "/flags",
        params={"name": "page_load_ms", "from": START, "to": END},
    )
    assert resp.status_code == 200
    assert resp.json()["flags"] == []


def test_invalid_rule_and_thresholds_return_400() -> None:
    client = TestClient(create_app(TimeSeriesStore()))
    base = {"name": "page_load_ms", "from": START, "to": END}

    resp_rule = client.get("/flags", params={**base, "rule": "bogus"})
    assert resp_rule.status_code == 400
    assert resp_rule.json()["error"].startswith("rule")

    for bad_threshold in ("abc", "0", "-1"):
        resp_thresh = client.get("/flags", params={**base, "threshold": bad_threshold})
        assert resp_thresh.status_code == 400
        assert resp_thresh.json()["error"].startswith("threshold")


def test_missing_required_parameters_return_400() -> None:
    client = TestClient(create_app(TimeSeriesStore()))

    resp_no_name = client.get("/flags", params={"from": START, "to": END})
    assert resp_no_name.status_code == 400
    assert "name" in resp_no_name.json()["error"]

    resp_no_from = client.get("/flags", params={"name": "page_load_ms", "to": END})
    assert resp_no_from.status_code == 400
    assert "from" in resp_no_from.json()["error"]

    resp_no_to = client.get("/flags", params={"name": "page_load_ms", "from": START})
    assert resp_no_to.status_code == 400
    assert "to" in resp_no_to.json()["error"]


def test_flags_requires_api_key_when_configured() -> None:
    store = TimeSeriesStore()
    store.write(build_test_points())
    client = TestClient(create_app(store, api_key="test-key-123"))
    params = {"name": "page_load_ms", "from": START, "to": END}

    resp_unauth = client.get("/flags", params=params)
    assert resp_unauth.status_code == 401
    assert resp_unauth.json() == {"error": "unauthorized"}

    resp_auth = client.get(
        "/flags",
        params=params,
        headers={"X-API-Key": "test-key-123"},
    )
    assert resp_auth.status_code == 200
    assert len(resp_auth.json()["flags"]) == 1


def test_flags_sorted_oldest_first_and_plain_json_numbers() -> None:
    store = TimeSeriesStore()
    pts_acme = build_test_points(n_normal=120, include_spike=True)
    # Add a second series (globex) with an earlier spike at minute 80
    start_epoch = parse_ts(START)
    pts_globex: List[Dict[str, Any]] = []
    for i in range(120):
        val = 1000 if i == 80 else 100 + (i % 5)
        pts_globex.append({
            "name": "page_load_ms",
            "value": val,
            "ts": format_ts(start_epoch + i * 60),
            "tags": {"client": "globex"},
        })
    store.write(pts_acme + pts_globex)

    client = TestClient(create_app(store))
    resp = client.get(
        "/flags",
        params={"name": "page_load_ms", "from": START, "to": END},
    )
    assert resp.status_code == 200
    raw_text = resp.text
    parsed = json.loads(raw_text)

    flags = parsed["flags"]
    assert len(flags) == 2
    assert flags[0]["ts"] < flags[1]["ts"]
    assert flags[0]["tags"] == {"client": "globex"}
    assert flags[1]["tags"] == {"client": "acme"}

    for flag in flags:
        assert type(flag["value"]) is float
        assert type(flag["score"]) is float

