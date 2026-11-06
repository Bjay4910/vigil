"""API integration tests for optional uptime checks in store/api.py."""

import time
from typing import Tuple

from fastapi.testclient import TestClient
import pytest

import store.checks
from store.api import create_app
from store.engine import TimeSeriesStore


def test_no_checks_configured_leaves_store_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("VIGIL_CHECKS", raising=False)
    app = create_app(TimeSeriesStore())
    with TestClient(app) as client:
        time.sleep(0.1)
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json()["points"] == 0


def test_checks_run_during_lifespan_and_stop_on_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def stub_check_url(url: str, timeout: float = 10.0) -> Tuple[bool, float]:
        return (True, 12.0)

    monkeypatch.setattr(store.checks, "check_url", stub_check_url)

    ts_store = TimeSeriesStore()
    app = create_app(
        ts_store,
        checks={"site-a": "https://example.com"},
        check_interval=0.05,
    )

    with TestClient(app) as client:
        time.sleep(0.4)
        health = client.get("/health")
        assert health.status_code == 200
        assert health.json()["points"] > 0

        query = client.get(
            "/query",
            params={
                "name": "uptime_up",
                "from": "2020-01-01T00:00:00Z",
                "to": "2035-01-01T00:00:00Z",
                "tags": "site:site-a",
            },
        )
        assert query.status_code == 200
        pts = query.json()["points"]
        assert len(pts) > 0
        assert all(p["value"] == 1.0 for p in pts)

    # d. After exiting the "with" block, the background task is finished and no points are added
    count_after_exit = len(ts_store)
    time.sleep(0.2)
    assert len(ts_store) == count_after_exit


def test_invalid_vigil_checks_env_makes_create_app_raise(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VIGIL_CHECKS", "site-a=http://example.com")
    with pytest.raises(ValueError) as exc_info:
        create_app(TimeSeriesStore())
    assert str(exc_info.value).startswith("VIGIL_CHECKS:")


def test_existing_auth_behavior_unchanged_with_checks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        store.checks, "check_url", lambda url, timeout=10.0: (True, 10.0)
    )
    app = create_app(
        TimeSeriesStore(),
        api_key="admin-key-0123456789",
        checks={"site-a": "https://example.com"},
        check_interval=60.0,
    )
    with TestClient(app) as client:
        resp = client.get(
            "/query",
            params={
                "name": "uptime_up",
                "from": "2020-01-01T00:00:00Z",
                "to": "2035-01-01T00:00:00Z",
            },
        )
        assert resp.status_code == 401
        assert resp.json() == {"error": "unauthorized"}

