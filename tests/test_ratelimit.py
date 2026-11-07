"""Unit tests for rate limiting, lockout, client_key, and config parsers in store/ratelimit.py."""

import inspect
from typing import List

import pytest

from store.ratelimit import (
    RateLimiter,
    client_key,
    parse_auth_fail_limit,
    parse_max_body,
    parse_max_points,
    parse_rate_limit,
)


def test_parse_functions_defaults_bounds_and_safe_errors() -> None:
    assert parse_rate_limit(None) == 300
    assert parse_rate_limit("") == 300
    assert parse_rate_limit("0") == 0
    assert parse_rate_limit("100000") == 100000
    for bad_rate in ("-1", "100001", "not-a-number-secret-1"):
        with pytest.raises(ValueError) as exc_info:
            parse_rate_limit(bad_rate)
        msg = str(exc_info.value)
        assert msg.startswith("VIGIL_RATE_LIMIT:")
        assert bad_rate not in msg

    assert parse_auth_fail_limit(None) == 10
    assert parse_auth_fail_limit("   ") == 10
    assert parse_auth_fail_limit("0") == 0
    assert parse_auth_fail_limit("1000") == 1000
    for bad_fail in ("-1", "1001", "bad-fail-val"):
        with pytest.raises(ValueError) as exc_info:
            parse_auth_fail_limit(bad_fail)
        msg = str(exc_info.value)
        assert msg.startswith("VIGIL_AUTH_FAIL_LIMIT:")
        assert bad_fail not in msg

    assert parse_max_body(None) == 1000000
    assert parse_max_body("") == 1000000
    assert parse_max_body("1000") == 1000
    assert parse_max_body("50000000") == 50000000
    for bad_body in ("0", "999", "50000001", "bad-body-val"):
        with pytest.raises(ValueError) as exc_info:
            parse_max_body(bad_body)
        msg = str(exc_info.value)
        assert msg.startswith("VIGIL_MAX_BODY_BYTES:")
        assert bad_body not in msg

    assert parse_max_points(None) == 5000
    assert parse_max_points("") == 5000
    assert parse_max_points("1") == 1
    assert parse_max_points("100000") == 100000
    for bad_pts in ("0", "-5", "100001", "bad-pts-val"):
        with pytest.raises(ValueError) as exc_info:
            parse_max_points(bad_pts)
        msg = str(exc_info.value)
        assert msg.startswith("VIGIL_MAX_POINTS:")
        assert bad_pts not in msg


def test_client_key_ipv4_ipv6_fallback_and_no_xff() -> None:
    sig = inspect.signature(client_key)
    assert list(sig.parameters.keys()) == ["fly_client_ip", "peer_host"]

    # Valid IPv4 Fly-Client-IP is normalized and preferred over peer_host
    assert client_key(" 198.51.100.42 ", "127.0.0.1") == "198.51.100.42"

    # IPv6 addresses in the same /64 collapse to the same bucket key
    k1 = client_key("2001:db8:abcd:0012::1", "127.0.0.1")
    k2 = client_key("2001:db8:abcd:0012:ffff:ffff:ffff:ffff%eth0", "127.0.0.1")
    k3 = client_key("2001:db8:abcd:0013::1", "127.0.0.1")
    assert k1 == k2 == "2001:db8:abcd:12::/64"
    assert k3 == "2001:db8:abcd:13::/64"
    assert k1 != k3

    # Invalid Fly-Client-IP falls back to peer_host
    assert client_key("not-an-ip", " 192.0.2.9 ") == "192.0.2.9"
    assert client_key("", "127.0.0.1") == "127.0.0.1"
    assert client_key(None, "127.0.0.1") == "127.0.0.1"

    # No header and no peer gives "unknown"
    assert client_key(None, None) == "unknown"
    assert client_key("   ", "   ") == "unknown"
    assert client_key("invalid-ip", None) == "unknown"


def test_rate_limiter_per_minute_cap_and_independent_keys() -> None:
    now: List[float] = [100.0]
    limiter = RateLimiter(cap_per_minute=3, fail_limit=5, now_fn=lambda: now[0])

    assert limiter.check("k1") is None
    assert limiter.check("k1") is None
    assert limiter.check("k1") is None
    retry = limiter.check("k1")
    assert retry == 60

    # A second key is independent
    assert limiter.check("k2") is None

    # Advance 25s -> still in same 60s window
    now[0] += 25.0
    assert limiter.check("k1") == 35

    # Advance to next minute (100.0 + 60.0 = 160.0) -> resets
    now[0] = 160.0
    assert limiter.check("k1") is None


def test_rate_limiter_lockout_and_fail_window_expiry() -> None:
    now: List[float] = [1000.0]
    limiter = RateLimiter(
        cap_per_minute=100,
        fail_limit=3,
        now_fn=lambda: now[0],
        fail_window=300.0,
        lockout=600.0,
    )

    # Two failures at t=1000, then advance past fail_window (301s) -> forgotten
    limiter.record_failure("k1")
    limiter.record_failure("k1")
    assert limiter.check("k1") is None

    now[0] += 301.0
    limiter.record_failure("k1")
    assert limiter.check("k1") is None

    # Reach 3 failures within fail_window -> lockout starts for 600s
    limiter.record_failure("k1")
    limiter.record_failure("k1")
    assert limiter.check("k1") == 600

    now[0] += 599.2
    assert limiter.check("k1") == 1

    # After 600s lockout expires
    now[0] += 1.0
    assert limiter.check("k1") is None


def test_rate_limiter_zero_caps_disable_limits() -> None:
    now: List[float] = [50.0]
    limiter = RateLimiter(cap_per_minute=0, fail_limit=0, now_fn=lambda: now[0])

    for _ in range(50):
        limiter.record_failure("k1")
        assert limiter.check("k1") is None


def test_rate_limiter_bounded_tables_evict_oldest() -> None:
    now: List[float] = [10.0]
    max_entries = 10000
    limiter = RateLimiter(
        cap_per_minute=2,
        fail_limit=2,
        now_fn=lambda: now[0],
        max_entries=max_entries,
    )

    # Exhaust k-0 so it has 2 requests and 1 failure recorded
    assert limiter.check("k-0") is None
    assert limiter.check("k-0") is None
    assert limiter.check("k-0") == 60
    limiter.record_failure("k-0")

    for i in range(1, 20001):
        k = f"k-{i}"
        limiter.check(k)
        limiter.record_failure(k)

    assert len(limiter._windows) <= max_entries
    assert len(limiter._failures) <= max_entries
    assert len(limiter._lockouts) <= max_entries

    # k-0 was evicted from _windows and _failures, so it starts fresh
    assert limiter.check("k-0") is None
    limiter.record_failure("k-0")
    assert limiter.check("k-0") is None

