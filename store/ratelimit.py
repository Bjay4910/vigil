"""Per-client request rate limiting, failed-auth lockout, and request size parsers."""

from collections import OrderedDict
import ipaddress
import math
import re
import time
from typing import Any, Callable, List, Optional, Tuple

_INT_RE = re.compile(r"^-?\d+$")


def _parse_bounded_int(
    raw: Optional[Any],
    var_name: str,
    default: int,
    min_val: int,
    max_val: int,
) -> int:
    if raw is None:
        return default
    if isinstance(raw, bool):
        raise ValueError(f"{var_name}: must be a valid integer")
    if isinstance(raw, int):
        val = raw
    elif isinstance(raw, str):
        stripped = raw.strip()
        if not stripped:
            return default
        if not _INT_RE.match(stripped):
            raise ValueError(f"{var_name}: must be a valid integer")
        val = int(stripped)
    else:
        raise ValueError(f"{var_name}: must be a valid integer")

    if val < min_val or val > max_val:
        raise ValueError(f"{var_name}: value out of allowed range")
    return val


def parse_rate_limit(raw: Optional[Any]) -> int:
    """Parse VIGIL_RATE_LIMIT (default 300; 0 disables; allowed 0..100000)."""
    return _parse_bounded_int(raw, "VIGIL_RATE_LIMIT", 300, 0, 100000)


def parse_auth_fail_limit(raw: Optional[Any]) -> int:
    """Parse VIGIL_AUTH_FAIL_LIMIT (default 10; 0 disables; allowed 0..1000)."""
    return _parse_bounded_int(raw, "VIGIL_AUTH_FAIL_LIMIT", 10, 0, 1000)


def parse_max_body(raw: Optional[Any]) -> int:
    """Parse VIGIL_MAX_BODY_BYTES (default 1000000; allowed 1000..50000000)."""
    return _parse_bounded_int(raw, "VIGIL_MAX_BODY_BYTES", 1000000, 1000, 50000000)


def parse_max_points(raw: Optional[Any]) -> int:
    """Parse VIGIL_MAX_POINTS (default 5000; allowed 1..100000)."""
    return _parse_bounded_int(raw, "VIGIL_MAX_POINTS", 5000, 1, 100000)


def client_key(
    fly_client_ip: Optional[str],
    peer_host: Optional[str],
) -> str:
    """Derive the rate-limiting bucket key from Fly-Client-IP or peer_host."""
    if isinstance(fly_client_ip, str):
        stripped_fly = fly_client_ip.strip()
        if stripped_fly:
            candidate = stripped_fly.split("%", 1)[0]
            try:
                addr = ipaddress.ip_address(candidate)
                if isinstance(addr, ipaddress.IPv4Address):
                    return str(addr)
                net64 = ipaddress.IPv6Network((addr, 64), strict=False)
                return str(net64)
            except ValueError:
                pass

    if isinstance(peer_host, str):
        stripped_peer = peer_host.strip()
        if stripped_peer:
            return stripped_peer

    return "unknown"


class RateLimiter:
    """In-memory per-client request rate limiter and failed-authentication lockout."""

    def __init__(
        self,
        cap_per_minute: int,
        fail_limit: int,
        now_fn: Optional[Callable[[], float]] = None,
        max_entries: int = 10000,
        fail_window: float = 300.0,
        lockout: float = 600.0,
    ) -> None:
        self.cap_per_minute = cap_per_minute
        self.fail_limit = fail_limit
        self.now_fn: Callable[[], float] = (
            now_fn if now_fn is not None else time.monotonic
        )
        self.max_entries = max_entries
        self.fail_window = fail_window
        self.lockout = lockout
        self._lockouts: "OrderedDict[str, float]" = OrderedDict()
        self._windows: "OrderedDict[str, Tuple[float, int]]" = OrderedDict()
        self._failures: "OrderedDict[str, List[float]]" = OrderedDict()

    def _put_bounded(
        self,
        table: "OrderedDict[str, Any]",
        key: str,
        value: Any,
    ) -> None:
        if key in table:
            table.move_to_end(key)
            table[key] = value
            return
        while len(table) >= self.max_entries:
            table.popitem(last=False)
        table[key] = value

    def check(self, key: str) -> Optional[int]:
        """Return Retry-After seconds (>= 1) if locked out or over cap, else None."""
        now = float(self.now_fn())

        if key in self._lockouts:
            until = self._lockouts[key]
            if now < until:
                remaining = int(math.ceil(until - now))
                return max(1, remaining)
            del self._lockouts[key]

        if self.cap_per_minute > 0:
            if key in self._windows:
                win_start, count = self._windows[key]
                if (now - win_start) >= 60.0 or now < win_start:
                    win_start = now
                    count = 1
                else:
                    count += 1
            else:
                win_start = now
                count = 1

            self._put_bounded(self._windows, key, (win_start, count))
            if count > self.cap_per_minute:
                until_end = (win_start + 60.0) - now
                retry_secs = int(math.ceil(until_end))
                return max(1, retry_secs)

        return None

    def record_failure(self, key: str) -> None:
        """Record an authentication failure and start a lockout when reaching fail_limit."""
        if self.fail_limit <= 0:
            return

        now = float(self.now_fn())
        cutoff = now - self.fail_window
        prev = self._failures.get(key, [])
        recent = [t for t in prev if t > cutoff]
        recent.append(now)

        if len(recent) >= self.fail_limit:
            self._failures.pop(key, None)
            self._put_bounded(self._lockouts, key, now + self.lockout)
        else:
            self._put_bounded(self._failures, key, recent)
