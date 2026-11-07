"""Unit tests for uptime check retry_delay confirmation re-check in store/checks.py."""

import asyncio
from typing import Dict, Tuple

from store.checks import run_round
from store.engine import TimeSeriesStore


def test_retry_succeeds_on_second_attempt_records_up() -> None:
    ts_store = TimeSeriesStore()
    checks = {"site-a": "https://a.example.com"}
    calls: Dict[str, int] = {"site-a": 0}

    def stub_check(url: str, timeout: float = 10.0) -> Tuple[bool, float]:
        calls["site-a"] += 1
        if calls["site-a"] == 1:
            return (False, 50.0)
        return (True, 35.0)

    asyncio.run(
        run_round(
            ts_store,
            checks,
            env="prod",
            check_fn=stub_check,
            retry_delay=0.01,
        )
    )
    assert calls["site-a"] == 2
    pts = ts_store.query(
        "uptime_up",
        "2020-01-01T00:00:00Z",
        "2035-01-01T00:00:00Z",
        tags={"site": "site-a"},
    )
    assert len(pts) == 1
    assert pts[0]["value"] == 1.0


def test_retry_always_failing_records_down_and_up_sites_called_once() -> None:
    ts_store = TimeSeriesStore()
    checks = {
        "site-a": "https://a.example.com",
        "site-b": "https://b.example.com",
    }
    calls: Dict[str, int] = {"site-a": 0, "site-b": 0}

    def stub_check(url: str, timeout: float = 10.0) -> Tuple[bool, float]:
        if "a.example.com" in url:
            calls["site-a"] += 1
            return (False, 80.0)
        calls["site-b"] += 1
        return (True, 20.0)

    asyncio.run(
        run_round(
            ts_store,
            checks,
            env="prod",
            check_fn=stub_check,
            retry_delay=0.01,
        )
    )
    assert calls["site-a"] == 2
    assert calls["site-b"] == 1

    pts_a = ts_store.query(
        "uptime_up",
        "2020-01-01T00:00:00Z",
        "2035-01-01T00:00:00Z",
        tags={"site": "site-a"},
    )
    pts_b = ts_store.query(
        "uptime_up",
        "2020-01-01T00:00:00Z",
        "2035-01-01T00:00:00Z",
        tags={"site": "site-b"},
    )
    assert len(pts_a) == 1 and pts_a[0]["value"] == 0.0
    assert len(pts_b) == 1 and pts_b[0]["value"] == 1.0


def test_default_retry_delay_calls_failing_stub_once() -> None:
    ts_store = TimeSeriesStore()
    checks = {"site-a": "https://a.example.com"}
    calls = 0

    def stub_check(url: str, timeout: float = 10.0) -> Tuple[bool, float]:
        nonlocal calls
        calls += 1
        return (False, 40.0)

    asyncio.run(
        run_round(
            ts_store,
            checks,
            env="prod",
            check_fn=stub_check,
        )
    )
    assert calls == 1

