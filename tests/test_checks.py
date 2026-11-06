"""Unit tests for store/checks.py."""

import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import re
import socket
import threading
import time
from typing import Dict, List, Tuple

import pytest

from store.checks import (
    check_url,
    parse_checks,
    parse_interval,
    run_checks,
    run_round,
)
from store.engine import TimeSeriesStore


def test_parse_checks_valid_and_invalid_cases() -> None:
    assert parse_checks(None) == {}
    assert parse_checks("") == {}
    assert parse_checks("   ") == {}

    valid_raw = (
        "site-a=https://acme.example.com/health?probe=1, "
        "site_1=https://sub.acme.example.com/status"
    )
    assert parse_checks(valid_raw) == {
        "site-a": "https://acme.example.com/health?probe=1",
        "site_1": "https://sub.acme.example.com/status",
    }

    many_entries = ",".join(
        f"site-{i}=https://s{i}.example.com" for i in range(21)
    )

    invalid_cases: List[Tuple[str, str]] = [
        ("site-a-https://acme.example.com", "https://acme.example.com"),
        ("Bad_Site=https://acme.example.com", "https://acme.example.com"),
        ("-bad=https://acme.example.com", "https://acme.example.com"),
        ("=https://acme.example.com", "https://acme.example.com"),
        (
            "site-a=https://a.example.com,site-a=https://b.example.com",
            "https://a.example.com",
        ),
        (many_entries, "https://s0.example.com"),
        ("site-a=http://acme.example.com", "http://acme.example.com"),
        ("site-a=https:///path-only", "https:///path-only"),
        ("site-a=https://user:pass@acme.example.com", "https://user:pass@acme.example.com"),
        ("site-a=https://user@acme.example.com", "https://user@acme.example.com"),
        ("site-a=https://127.0.0.1/health", "https://127.0.0.1/health"),
        ("site-a=https://[::1]/health", "https://[::1]/health"),
        ("site-a=https://localhost/health", "https://localhost/health"),
        ("site-a=https://intranet/health", "https://intranet/health"),
    ]

    for raw, url_snippet in invalid_cases:
        with pytest.raises(ValueError) as exc_info:
            parse_checks(raw)
        msg = str(exc_info.value)
        assert msg.startswith("VIGIL_CHECKS:")
        assert url_snippet not in msg


class _LocalCheckHandler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        pass

    def do_GET(self) -> None:
        if self.path == "/ok":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")
        elif self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/ok")
            self.end_headers()
        elif self.path == "/error":
            self.send_response(500)
            self.end_headers()
            self.wfile.write(b"err")
        elif self.path == "/missing":
            self.send_response(404)
            self.end_headers()
        elif self.path == "/slow":
            time.sleep(0.5)
            try:
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"slow")
            except Exception:
                pass
        else:
            self.send_response(404)
            self.end_headers()


def test_check_url_against_local_http_server() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _LocalCheckHandler)
    host, port = server.server_address[:2]
    base_url = f"http://{host}:{port}"

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        ok, ms = check_url(f"{base_url}/ok")
        assert ok is True
        assert ms >= 0.0

        redir_ok, redir_ms = check_url(f"{base_url}/redirect")
        assert redir_ok is True
        assert redir_ms >= 0.0

        err_ok, err_ms = check_url(f"{base_url}/error")
        assert err_ok is False
        assert err_ms >= 0.0

        not_found_ok, nf_ms = check_url(f"{base_url}/missing")
        assert not_found_ok is False
        assert nf_ms >= 0.0

        slow_ok, slow_ms = check_url(f"{base_url}/slow", timeout=0.2)
        assert slow_ok is False
        assert slow_ms >= 0.0
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2.0)

    # Closed port check
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        closed_port = s.getsockname()[1]

    closed_ok, closed_ms = check_url(f"http://127.0.0.1:{closed_port}/ok", timeout=0.5)
    assert closed_ok is False
    assert closed_ms >= 0.0


def test_run_round_writes_expected_points_and_handles_exceptions() -> None:
    store = TimeSeriesStore()
    checks: Dict[str, str] = {
        "site-a": "https://a.example.com",
        "site-b": "https://b.example.com",
        "site-c": "https://c.example.com",
    }

    def stub_check(url: str, timeout: float = 10.0) -> Tuple[bool, float]:
        if "a.example.com" in url:
            return (True, 42.6)
        if "b.example.com" in url:
            return (False, 118.2)
        raise RuntimeError("unexpected check failure")

    asyncio.run(run_round(store, checks, env="prod", check_fn=stub_check))
    assert len(store) == 6

    start = "2020-01-01T00:00:00Z"
    end = "2035-01-01T00:00:00Z"
    ts_re = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

    up_a = store.query("uptime_up", start, end, tags={"site": "site-a"})
    ms_a = store.query("response_ms", start, end, tags={"site": "site-a"})
    assert len(up_a) == 1 and up_a[0]["value"] == 1.0
    assert len(ms_a) == 1 and ms_a[0]["value"] == 43.0
    assert up_a[0]["tags"] == {"site": "site-a", "env": "prod", "service": "uptime"}
    assert ms_a[0]["tags"] == {"site": "site-a", "env": "prod", "service": "uptime"}
    assert ts_re.match(up_a[0]["ts"])

    up_b = store.query("uptime_up", start, end, tags={"site": "site-b"})
    ms_b = store.query("response_ms", start, end, tags={"site": "site-b"})
    assert len(up_b) == 1 and up_b[0]["value"] == 0.0
    assert len(ms_b) == 1 and ms_b[0]["value"] == 118.0

    up_c = store.query("uptime_up", start, end, tags={"site": "site-c"})
    ms_c = store.query("response_ms", start, end, tags={"site": "site-c"})
    assert len(up_c) == 1 and up_c[0]["value"] == 0.0
    assert len(ms_c) == 1 and ms_c[0]["value"] == 0.0


def test_run_checks_loops_and_stops_promptly_and_survives_round_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = TimeSeriesStore()
    checks = {"site-a": "https://a.example.com"}
    call_count = 0

    def stub_check(url: str, timeout: float = 10.0) -> Tuple[bool, float]:
        nonlocal call_count
        call_count += 1
        return (True, 15.0)

    async def _exercise_loop() -> None:
        stop = asyncio.Event()
        task = asyncio.create_task(
            run_checks(store, checks, interval=0.05, env="prod", stop=stop, check_fn=stub_check)
        )
        await asyncio.sleep(0.3)
        t0 = time.perf_counter()
        stop.set()
        await asyncio.wait_for(task, timeout=0.5)
        elapsed = time.perf_counter() - t0
        assert elapsed < 0.25

    asyncio.run(_exercise_loop())
    assert call_count >= 2
    assert len(store) >= 4

    # Verify a failure inside run_round on the first round does not stop later rounds
    round_calls = 0
    real_run_round = run_round

    async def flaky_run_round(*args: object, **kwargs: object) -> None:
        nonlocal round_calls
        round_calls += 1
        if round_calls == 1:
            raise RuntimeError("simulated round error")
        await real_run_round(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr("store.checks.run_round", flaky_run_round)

    async def _exercise_flaky() -> None:
        stop = asyncio.Event()
        task = asyncio.create_task(
            run_checks(store, checks, interval=0.05, env="prod", stop=stop, check_fn=stub_check)
        )
        await asyncio.sleep(0.2)
        stop.set()
        await asyncio.wait_for(task, timeout=0.5)

    asyncio.run(_exercise_flaky())
    assert round_calls >= 2


def test_parse_interval() -> None:
    assert parse_interval(None) == 300.0
    assert parse_interval("") == 300.0
    assert parse_interval("60") == 60.0

    for invalid in ("abc", "5", "-1"):
        with pytest.raises(ValueError) as exc_info:
            parse_interval(invalid)
        assert str(exc_info.value).startswith("VIGIL_CHECK_INTERVAL:")


def test_host_is_public() -> None:
    from store.checks import _host_is_public

    assert _host_is_public("example.com") is True
    assert _host_is_public("sub.example.co.uk") is True
    assert _host_is_public("EXAMPLE.COM.") is True

    for non_public in (
        "127.0.0.1",
        "::1",
        "localhost",
        "intranet",
        "db.internal",
        "printer.local",
        "app.localhost",
        "nas.lan",
    ):
        assert _host_is_public(non_public) is False


def test_parse_checks_rejects_private_suffixes_without_leaking_url() -> None:
    for suffix in (".internal", ".local", ".localhost", ".lan"):
        bad_url = f"https://service{suffix}/health"
        with pytest.raises(ValueError) as exc_info:
            parse_checks(f"site-a={bad_url}")
        msg = str(exc_info.value)
        assert msg.startswith("VIGIL_CHECKS:")
        assert msg == "VIGIL_CHECKS: host for site 'site-a' must be a public hostname"
        assert bad_url not in msg


def test_redirect_allowed() -> None:
    from store.checks import _redirect_allowed

    origin = "https://acme.example.com/start"
    assert _redirect_allowed(origin, "https://acme.example.com/next") is True
    assert _redirect_allowed(origin, "/next") is True
    assert _redirect_allowed(origin, "https://example.org/status") is True

    assert _redirect_allowed(origin, "http://example.org/status") is False
    assert _redirect_allowed(origin, "https://127.0.0.1/admin") is False
    assert _redirect_allowed(origin, "https://[::1]/admin") is False
    assert _redirect_allowed(origin, "https://localhost/admin") is False
    assert _redirect_allowed(origin, "https://db.internal/admin") is False
    assert _redirect_allowed(origin, "ftp://example.org/pub") is False
    assert _redirect_allowed(origin, "file:///etc/passwd") is False
    assert _redirect_allowed(origin, ":::not a valid url:::") is False


def test_check_url_redirect_same_origin_vs_different_port() -> None:
    target_server = ThreadingHTTPServer(("127.0.0.1", 0), _LocalCheckHandler)
    _, target_port = target_server.server_address[:2]

    class _CrossPortRedirectHandler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            pass

        def do_GET(self) -> None:
            if self.path == "/ok":
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"ok")
            elif self.path == "/same-origin":
                self.send_response(302)
                self.send_header("Location", "/ok")
                self.end_headers()
            elif self.path == "/diff-port":
                self.send_response(302)
                self.send_header("Location", f"http://127.0.0.1:{target_port}/ok")
                self.end_headers()
            else:
                self.send_response(404)
                self.end_headers()

    origin_server = ThreadingHTTPServer(("127.0.0.1", 0), _CrossPortRedirectHandler)
    origin_host, origin_port = origin_server.server_address[:2]
    origin_base = f"http://{origin_host}:{origin_port}"

    t1 = threading.Thread(target=target_server.serve_forever, daemon=True)
    t2 = threading.Thread(target=origin_server.serve_forever, daemon=True)
    t1.start()
    t2.start()
    try:
        same_ok, same_ms = check_url(f"{origin_base}/same-origin")
        assert same_ok is True
        assert same_ms >= 0.0

        diff_ok, diff_ms = check_url(f"{origin_base}/diff-port")
        assert diff_ok is False
        assert diff_ms >= 0.0
    finally:
        origin_server.shutdown()
        origin_server.server_close()
        target_server.shutdown()
        target_server.server_close()
        t1.join(timeout=2.0)
        t2.join(timeout=2.0)


def test_run_round_uses_wait_for_and_records_timeout_as_down(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import store.checks

    observed_timeouts: List[float] = []

    async def fake_wait_for(coro: object, timeout: float) -> object:
        observed_timeouts.append(timeout)
        if hasattr(coro, "close"):
            coro.close()  # type: ignore[attr-defined]
        raise asyncio.TimeoutError()

    monkeypatch.setattr(store.checks.asyncio, "wait_for", fake_wait_for)

    ts_store = TimeSeriesStore()
    asyncio.run(
        run_round(
            ts_store,
            {"site-a": "https://a.example.com"},
            env="prod",
            check_fn=lambda url, timeout=10.0: (True, 10.0),
            timeout=10.0,
        )
    )
    assert observed_timeouts == [15.0]
    pts = ts_store.query(
        "uptime_up",
        "2020-01-01T00:00:00Z",
        "2035-01-01T00:00:00Z",
        tags={"site": "site-a"},
    )
    assert len(pts) == 1
    assert pts[0]["value"] == 0.0


