"""Periodic HTTP uptime checker for the Vigil store."""

import asyncio
import inspect
import ipaddress
import math
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Dict, List, Optional, Tuple

from store.engine import format_ts

_SITE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
_MAX_CHECKS = 20
_MAX_BODY_BYTES = 65536
_USER_AGENT = "Vigil-uptime-check/1.0"
_PRIVATE_HOST_SUFFIXES = (".internal", ".local", ".localhost", ".lan")


def _host_rejection_reason(host: str) -> Optional[str]:
    if not isinstance(host, str) or not host:
        return "must be a public hostname"
    cleaned = host.lower()
    if cleaned.endswith("."):
        cleaned = cleaned[:-1]
    if not cleaned:
        return "must be a public hostname"

    ip_candidate = (
        cleaned[1:-1]
        if (cleaned.startswith("[") and cleaned.endswith("]"))
        else cleaned
    )
    ip_candidate = ip_candidate.split("%", 1)[0]
    try:
        ipaddress.ip_address(ip_candidate)
        return "must not be an IP address"
    except ValueError:
        pass

    if cleaned == "localhost":
        return "must not be localhost"

    if "." not in cleaned:
        return "must contain a dot"

    if cleaned.endswith(_PRIVATE_HOST_SUFFIXES):
        return "must be a public hostname"

    return None


def _host_is_public(host: str) -> bool:
    """Return True if host is a public domain name and not an IP or local/internal name."""
    return _host_rejection_reason(host) is None


def _validate_url(site: str, url: str) -> str:
    if not isinstance(url, str) or not url:
        raise ValueError(f"VIGIL_CHECKS: empty URL for site '{site}'")
    if any(ch.isspace() for ch in url):
        raise ValueError(f"VIGIL_CHECKS: invalid URL for site '{site}'")

    try:
        parsed = urllib.parse.urlsplit(url)
    except ValueError as exc:
        raise ValueError(f"VIGIL_CHECKS: invalid URL for site '{site}'") from exc

    if parsed.scheme.lower() != "https":
        raise ValueError(f"VIGIL_CHECKS: URL for site '{site}' must use https")

    if parsed.username is not None or parsed.password is not None or "@" in (parsed.netloc or ""):
        raise ValueError(f"VIGIL_CHECKS: URL for site '{site}' must not contain credentials")

    host = parsed.hostname
    if not host:
        raise ValueError(f"VIGIL_CHECKS: URL for site '{site}' has no host")

    if not _host_is_public(host):
        reason = _host_rejection_reason(host) or "must be a public hostname"
        raise ValueError(f"VIGIL_CHECKS: host for site '{site}' {reason}")

    return url


def _redirect_allowed(old_url: str, new_url: str) -> bool:
    """Return True if redirecting from old_url to new_url is permitted."""
    if not isinstance(old_url, str) or not isinstance(new_url, str):
        return False
    if not old_url or not new_url:
        return False
    if any(ch.isspace() for ch in old_url) or any(ch.isspace() for ch in new_url):
        return False

    try:
        old_parsed = urllib.parse.urlsplit(old_url)
        old_scheme = old_parsed.scheme.lower()
        old_host = (old_parsed.hostname or "").lower().rstrip(".")
        old_port = old_parsed.port or (
            443 if old_scheme == "https" else (80 if old_scheme == "http" else None)
        )
    except ValueError:
        return False

    if old_scheme not in ("http", "https") or not old_host:
        return False

    resolved_new = (
        urllib.parse.urljoin(old_url, new_url)
        if (new_url.startswith("/") and not new_url.startswith("//"))
        else new_url
    )

    try:
        new_parsed = urllib.parse.urlsplit(resolved_new)
        new_scheme = new_parsed.scheme.lower()
        new_host = (new_parsed.hostname or "").lower().rstrip(".")
        new_port = new_parsed.port or (
            443 if new_scheme == "https" else (80 if new_scheme == "http" else None)
        )
    except ValueError:
        return False

    if new_scheme not in ("http", "https") or not new_host:
        return False
    if (
        new_parsed.username is not None
        or new_parsed.password is not None
        or "@" in (new_parsed.netloc or "")
    ):
        return False

    if (new_scheme, new_host, new_port) == (old_scheme, old_host, old_port):
        return True

    if new_scheme == "https" and _host_is_public(new_host):
        return True

    return False


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Redirect handler that restricts targets with _redirect_allowed and caps hops at 5."""

    max_redirections = 5

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> Optional[urllib.request.Request]:
        if not _redirect_allowed(req.full_url, newurl):
            return None
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def parse_checks(raw: Optional[str]) -> Dict[str, str]:
    """Parse and validate comma-separated site=url pairs from VIGIL_CHECKS."""
    if raw is None:
        return {}
    stripped = raw.strip()
    if not stripped:
        return {}

    result: Dict[str, str] = {}
    for part in stripped.split(","):
        item = part.strip()
        if not item:
            continue
        if "=" not in item:
            raise ValueError("VIGIL_CHECKS: entry is missing '=' separator")
        site_part, url_part = item.split("=", 1)
        site = site_part.strip()
        url = url_part.strip()

        if not site or not _SITE_RE.match(site):
            raise ValueError("VIGIL_CHECKS: invalid site name")
        if site in result:
            raise ValueError(f"VIGIL_CHECKS: duplicate site '{site}'")
        if len(result) >= _MAX_CHECKS:
            raise ValueError(f"VIGIL_CHECKS: at most {_MAX_CHECKS} sites are allowed")

        result[site] = _validate_url(site, url)

    return result


def validate_checks_dict(checks: Dict[str, str]) -> Dict[str, str]:
    """Validate a checks dict by round-tripping each entry through parse_checks."""
    if not isinstance(checks, dict):
        raise ValueError("VIGIL_CHECKS: checks must be a dict")
    if len(checks) > _MAX_CHECKS:
        raise ValueError(f"VIGIL_CHECKS: at most {_MAX_CHECKS} sites are allowed")

    result: Dict[str, str] = {}
    for site, url in checks.items():
        if not isinstance(site, str) or not isinstance(url, str):
            raise ValueError("VIGIL_CHECKS: site and URL must be strings")
        clean_site = site.strip()
        if not clean_site or "=" in clean_site or "," in clean_site or not _SITE_RE.match(clean_site):
            raise ValueError("VIGIL_CHECKS: invalid site name")
        if "," in url or not url.strip():
            raise ValueError(f"VIGIL_CHECKS: invalid URL for site '{clean_site}'")
        if clean_site in result:
            raise ValueError(f"VIGIL_CHECKS: duplicate site '{clean_site}'")
        parsed_one = parse_checks(f"{clean_site}={url.strip()}")
        result.update(parsed_one)

    return result


def parse_interval(raw: Optional[str]) -> float:
    """Parse VIGIL_CHECK_INTERVAL in seconds (default 300.0, minimum 30.0)."""
    if raw is None:
        return 300.0
    stripped = raw.strip()
    if not stripped:
        return 300.0
    try:
        val = float(stripped)
    except (TypeError, ValueError) as exc:
        raise ValueError("VIGIL_CHECK_INTERVAL: must be a number") from exc
    if math.isnan(val) or math.isinf(val) or val < 30.0:
        raise ValueError("VIGIL_CHECK_INTERVAL: must be at least 30 seconds")
    return val


def check_url(url: str, timeout: float = 10.0) -> Tuple[bool, float]:
    """Perform an HTTP GET and return (is_up, elapsed_ms). Never raises."""
    start = time.perf_counter()
    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": _USER_AGENT},
            method="GET",
        )
        opener = urllib.request.build_opener(_SafeRedirectHandler())
        with opener.open(req, timeout=timeout) as resp:
            resp.read(_MAX_BODY_BYTES)
            status = getattr(resp, "status", None)
            if status is None:
                status = resp.getcode()
        elapsed_ms = (time.perf_counter() - start) * 1000.0
        is_up = status is not None and 200 <= int(status) <= 399
        return (is_up, elapsed_ms)
    except Exception:
        elapsed_ms = (time.perf_counter() - start) * 1000.0
        return (False, elapsed_ms)


def _call_check_fn(
    fn: Callable[..., Tuple[bool, float]],
    url: str,
    timeout: float,
) -> Tuple[bool, float]:
    try:
        sig = inspect.signature(fn)
        pos_params = [
            p
            for p in sig.parameters.values()
            if p.kind
            in (
                inspect.Parameter.POSITIONAL_ONLY,
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
            )
        ]
        has_varargs = any(
            p.kind == inspect.Parameter.VAR_POSITIONAL
            for p in sig.parameters.values()
        )
        if len(pos_params) >= 2 or has_varargs:
            return fn(url, timeout)
        return fn(url)
    except (ValueError, TypeError):
        return fn(url, timeout)


async def _run_single_check(
    fn: Callable[..., Tuple[bool, float]],
    url: str,
    timeout: float,
) -> Tuple[bool, float]:
    return await asyncio.wait_for(
        asyncio.to_thread(_call_check_fn, fn, url, timeout),
        timeout + 5.0,
    )


def _parse_check_result(res: Any) -> Tuple[bool, float]:
    if isinstance(res, tuple) and len(res) == 2:
        try:
            is_up = bool(res[0])
            ms_val = float(res[1])
            if math.isnan(ms_val) or math.isinf(ms_val) or ms_val < 0.0:
                ms_val = 0.0
            return (is_up, ms_val)
        except Exception:
            return (False, 0.0)
    return (False, 0.0)


async def run_round(
    store: Any,
    checks: Dict[str, str],
    env: str,
    check_fn: Optional[Callable[..., Tuple[bool, float]]] = None,
    timeout: float = 10.0,
    retry_delay: float = 0.0,
) -> None:
    """Check all configured sites concurrently and record uptime_up and response_ms."""
    if not checks:
        return

    fn = check_url if check_fn is None else check_fn
    items: List[Tuple[str, str]] = list(checks.items())

    tasks = [
        _run_single_check(fn, url, timeout)
        for _, url in items
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    parsed_results: List[Tuple[bool, float]] = [
        _parse_check_result(res) for res in results
    ]

    if retry_delay > 0.0:
        down_indices = [
            idx for idx, (is_up, _) in enumerate(parsed_results) if not is_up
        ]
        if down_indices:
            await asyncio.sleep(retry_delay)
            retry_tasks = [
                _run_single_check(fn, items[idx][1], timeout)
                for idx in down_indices
            ]
            retry_results = await asyncio.gather(*retry_tasks, return_exceptions=True)
            for idx, retry_res in zip(down_indices, retry_results):
                parsed_results[idx] = _parse_check_result(retry_res)

    now_ts = format_ts(int(time.time()))
    points: List[Dict[str, Any]] = []
    for (site, _), (is_up, ms_val) in zip(items, parsed_results):
        tags = {"site": site, "env": env, "service": "uptime"}
        points.append({
            "name": "uptime_up",
            "value": 1 if is_up else 0,
            "ts": now_ts,
            "tags": dict(tags),
        })
        points.append({
            "name": "response_ms",
            "value": int(round(ms_val)),
            "ts": now_ts,
            "tags": dict(tags),
        })

    store.write(points)


async def run_checks(
    store: Any,
    checks: Dict[str, str],
    interval: float,
    env: str,
    stop: asyncio.Event,
    check_fn: Optional[Callable[..., Tuple[bool, float]]] = None,
    retry_delay: float = 0.0,
) -> None:
    """Loop running uptime rounds until stop is set."""
    while not stop.is_set():
        try:
            await run_round(
                store,
                checks,
                env,
                check_fn=check_fn,
                retry_delay=retry_delay,
            )
        except Exception:
            print("uptime round failed", file=sys.stderr)

        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
            break
        except asyncio.TimeoutError:
            pass
