"""FastAPI application for time-series store."""

import asyncio
from contextlib import asynccontextmanager
import hmac
import json
import math
import os
import re
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Dict, List, Optional, Set, Tuple

from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from store.alerts import (
    AlertMonitor,
    Message,
    RateLimitedSender,
    parse_alert_env,
    parse_digest_hour,
    parse_max_per_hour,
    parse_timezone,
    post_webhook,
    run_alerts,
    validate_webhook,
)
from store.checks import parse_checks, parse_interval, run_checks, validate_checks_dict
from store.engine import TimeSeriesStore, ValidationError
from store.ratelimit import (
    RateLimiter,
    client_key,
    parse_auth_fail_limit,
    parse_max_body,
    parse_max_points,
    parse_rate_limit,
)

_SITE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


def error_response(message: str) -> JSONResponse:
    """Return a standard 400 JSON error response."""
    return JSONResponse(status_code=400, content={"error": message})


def unauthorized_response() -> JSONResponse:
    """Return a standard 401 JSON unauthorized response."""
    return JSONResponse(status_code=401, content={"error": "unauthorized"})


def forbidden_response(message: str) -> JSONResponse:
    """Return a standard 403 JSON forbidden response."""
    return JSONResponse(status_code=403, content={"error": message})


def payload_too_large_response(message: str) -> JSONResponse:
    """Return a standard 413 JSON payload-too-large response."""
    return JSONResponse(status_code=413, content={"error": message})


def _validate_project_entry(
    site: str,
    key: str,
    result: Dict[str, str],
    seen_keys: Set[str],
    admin_key: Optional[str],
) -> None:
    if not isinstance(site, str) or not site:
        raise ValueError("VIGIL_PROJECT_KEYS: site cannot be empty")
    if not _SITE_RE.match(site):
        raise ValueError(f"VIGIL_PROJECT_KEYS: invalid site '{site}'")
    if not isinstance(key, str) or not key:
        raise ValueError(f"VIGIL_PROJECT_KEYS: key for site '{site}' cannot be empty")
    if len(key) < 16:
        raise ValueError(f"VIGIL_PROJECT_KEYS: key for site '{site}' must be at least 16 characters")
    if site in result:
        raise ValueError(f"VIGIL_PROJECT_KEYS: duplicate site '{site}'")
    if key in seen_keys:
        raise ValueError(f"VIGIL_PROJECT_KEYS: duplicate key for site '{site}'")
    if admin_key and hmac.compare_digest(key.encode("utf-8"), admin_key.encode("utf-8")):
        raise ValueError(f"VIGIL_PROJECT_KEYS: key for site '{site}' must not equal the admin key")
    result[site] = key
    seen_keys.add(key)


def parse_project_keys(raw: Optional[str], admin_key: Optional[str] = None) -> Dict[str, str]:
    """Parse and validate a comma-separated site:key string into {site: key}."""
    if raw is None:
        return {}
    stripped_raw = raw.strip()
    if not stripped_raw:
        return {}

    result: Dict[str, str] = {}
    seen_keys: Set[str] = set()
    for part in stripped_raw.split(","):
        item = part.strip()
        if not item:
            continue
        if ":" not in item:
            raise ValueError("VIGIL_PROJECT_KEYS: entry is missing ':' separator")
        site_part, key_part = item.split(":", 1)
        site = site_part.strip()
        key = key_part.strip()
        _validate_project_entry(site, key, result, seen_keys, admin_key)

    return result


def _validate_project_keys_dict(
    mapping: Dict[str, str],
    admin_key: Optional[str],
) -> Dict[str, str]:
    if not isinstance(mapping, dict):
        raise ValueError("VIGIL_PROJECT_KEYS: project_keys must be a dict")
    result: Dict[str, str] = {}
    seen_keys: Set[str] = set()
    for site, key in mapping.items():
        _validate_project_entry(site, key, result, seen_keys, admin_key)
    return result


def create_app(
    store: Optional[TimeSeriesStore] = None,
    api_key: Optional[str] = None,
    project_keys: Optional[Dict[str, str]] = None,
    checks: Optional[Dict[str, str]] = None,
    check_interval: Optional[float] = None,
    alert_webhook: Optional[str] = None,
    alert_send: Optional[Callable[[Message], bool]] = None,
    alert_interval: float = 60.0,
    rate_limit: Optional[int] = None,
    auth_fail_limit: Optional[int] = None,
    max_body_bytes: Optional[int] = None,
    max_points: Optional[int] = None,
    limiter_now: Optional[Callable[[], float]] = None,
) -> FastAPI:
    """Create and configure the FastAPI application."""
    if store is None:
        data_path = os.environ.get("VIGIL_DATA_PATH", "data/points.jsonl")
        store = TimeSeriesStore(data_path)

    if api_key is None:
        api_key = os.environ.get("VIGIL_API_KEY")
    effective_key: Optional[str] = api_key if api_key else None

    if project_keys is None:
        effective_project_keys = parse_project_keys(
            os.environ.get("VIGIL_PROJECT_KEYS"),
            effective_key,
        )
    else:
        effective_project_keys = _validate_project_keys_dict(project_keys, effective_key)

    if checks is None:
        effective_checks = parse_checks(os.environ.get("VIGIL_CHECKS"))
    else:
        effective_checks = validate_checks_dict(checks)

    if check_interval is None:
        effective_interval = parse_interval(os.environ.get("VIGIL_CHECK_INTERVAL"))
    else:
        if isinstance(check_interval, bool) or not isinstance(check_interval, (int, float)):
            raise ValueError("VIGIL_CHECK_INTERVAL: must be a positive number")
        effective_interval = float(check_interval)
        if math.isnan(effective_interval) or math.isinf(effective_interval) or effective_interval <= 0.0:
            raise ValueError("VIGIL_CHECK_INTERVAL: must be a positive number")

    raw_check_env = os.environ.get("VIGIL_CHECK_ENV")
    check_env = raw_check_env if (raw_check_env is not None and raw_check_env != "") else "prod"
    if (
        not isinstance(check_env, str)
        or not check_env.strip()
        or check_env != check_env.lower()
        or check_env != check_env.strip()
    ):
        raise ValueError("VIGIL_CHECK_ENV: must be a non-empty lowercase string")

    raw_webhook = (
        os.environ.get("VIGIL_ALERT_WEBHOOK")
        if alert_webhook is None
        else alert_webhook
    )
    effective_webhook: Optional[str] = (
        validate_webhook(raw_webhook)
        if (raw_webhook is not None and raw_webhook != "")
        else None
    )
    digest_hour = parse_digest_hour(os.environ.get("VIGIL_DIGEST_HOUR"))
    digest_tz = parse_timezone(os.environ.get("VIGIL_DIGEST_TZ"))
    max_per_hour = parse_max_per_hour(os.environ.get("VIGIL_ALERT_MAX_PER_HOUR"))
    alert_env = parse_alert_env(os.environ.get("VIGIL_ALERT_ENV"))

    effective_rate_limit = parse_rate_limit(
        os.environ.get("VIGIL_RATE_LIMIT") if rate_limit is None else rate_limit
    )
    effective_auth_fail_limit = parse_auth_fail_limit(
        os.environ.get("VIGIL_AUTH_FAIL_LIMIT")
        if auth_fail_limit is None
        else auth_fail_limit
    )
    effective_max_body = parse_max_body(
        os.environ.get("VIGIL_MAX_BODY_BYTES")
        if max_body_bytes is None
        else max_body_bytes
    )
    effective_max_points = parse_max_points(
        os.environ.get("VIGIL_MAX_POINTS") if max_points is None else max_points
    )
    limiter = RateLimiter(
        cap_per_minute=effective_rate_limit,
        fail_limit=effective_auth_fail_limit,
        now_fn=limiter_now,
    )

    def _request_client_key(request: Request) -> str:
        peer = request.client.host if request.client else None
        return client_key(request.headers.get("Fly-Client-IP"), peer)

    def authenticate(request: Request) -> Tuple[str, Optional[str]]:
        if effective_key is None and not effective_project_keys:
            return ("open", None)
        provided = request.headers.get("X-API-Key")
        if provided is None:
            limiter.record_failure(_request_client_key(request))
            return ("unauthorized", None)

        matched_role = "unauthorized"
        matched_site: Optional[str] = None

        if effective_key is not None:
            if hmac.compare_digest(provided.encode("utf-8"), effective_key.encode("utf-8")):
                matched_role = "admin"

        for site_name, p_key in effective_project_keys.items():
            if hmac.compare_digest(provided.encode("utf-8"), p_key.encode("utf-8")):
                matched_role = "project"
                matched_site = site_name

        if matched_role == "unauthorized":
            limiter.record_failure(_request_client_key(request))

        return (matched_role, matched_site)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        stop_event = asyncio.Event()
        tasks: List[asyncio.Task] = []
        if effective_webhook is not None:
            monitor = AlertMonitor(
                store,
                env=alert_env,
                tz=digest_tz,
                digest_hour=digest_hour,
            )
            monitor.seed()
            raw_send: Callable[[Message], bool] = (
                alert_send
                if alert_send is not None
                else (lambda msg: post_webhook(effective_webhook, msg))
            )
            sender = RateLimitedSender(raw_send, max_per_hour=max_per_hour)
            tasks.append(
                asyncio.create_task(
                    run_alerts(monitor, sender.send, alert_interval, stop_event)
                )
            )
        if effective_checks:
            check_retry_delay = 15.0 if check_interval is None else 0.0
            tasks.append(
                asyncio.create_task(
                    run_checks(
                        store,
                        effective_checks,
                        effective_interval,
                        check_env,
                        stop_event,
                        retry_delay=check_retry_delay,
                    )
                )
            )
        try:
            yield
        finally:
            if tasks:
                stop_event.set()
                await asyncio.gather(*tasks, return_exceptions=True)

    app = FastAPI(lifespan=lifespan)

    @app.middleware("http")
    async def rate_limit_middleware(
        request: Request,
        call_next: Callable[[Request], Any],
    ) -> Any:
        if request.url.path == "/health":
            return await call_next(request)
        key = _request_client_key(request)
        retry_after = limiter.check(key)
        if retry_after is not None:
            return JSONResponse(
                status_code=429,
                content={"error": "too many requests"},
                headers={"Retry-After": str(retry_after)},
            )
        return await call_next(request)

    @app.post("/ingest")
    async def ingest(request: Request) -> Any:
        role, caller_site = authenticate(request)
        if role == "unauthorized":
            return unauthorized_response()

        raw_cl = request.headers.get("Content-Length")
        if raw_cl is not None:
            stripped_cl = raw_cl.strip()
            if not re.match(r"^\d+$", stripped_cl):
                return error_response("body: invalid Content-Length")
            cl_val = int(stripped_cl)
            if cl_val > effective_max_body:
                return payload_too_large_response(
                    f"body: too large (maximum {effective_max_body} bytes)"
                )

        chunks: List[bytes] = []
        total_bytes = 0
        async for chunk in request.stream():
            total_bytes += len(chunk)
            if total_bytes > effective_max_body:
                return payload_too_large_response(
                    f"body: too large (maximum {effective_max_body} bytes)"
                )
            chunks.append(chunk)

        raw_body = b"".join(chunks)
        try:
            body = json.loads(raw_body)
        except Exception:
            return error_response("body: must be valid JSON")

        if not isinstance(body, dict) or "points" not in body or not isinstance(body["points"], list):
            return error_response("points: must be a list")

        points = body["points"]
        if len(points) > effective_max_points:
            return payload_too_large_response(
                f"points: too many points in one request (maximum {effective_max_points})"
            )
        if role == "project":
            for pt in points:
                if isinstance(pt, dict):
                    pt_tags = pt.get("tags")
                    if not isinstance(pt_tags, dict) or pt_tags.get("site") != caller_site:
                        return forbidden_response(
                            "forbidden: this key can only write points tagged with its own site"
                        )

        try:
            count = store.write(points)
        except ValidationError as e:
            return error_response(str(e))

        return {"accepted": count}

    @app.get("/query")
    async def query(
        request: Request,
        name: Optional[str] = Query(default=None),
        start: Optional[str] = Query(default=None, alias="from"),
        end: Optional[str] = Query(default=None, alias="to"),
        tags: Optional[str] = Query(default=None),
    ) -> Any:
        role, caller_site = authenticate(request)
        if role == "unauthorized":
            return unauthorized_response()

        if name is None or name == "":
            return error_response("name: required")
        if start is None or start == "":
            return error_response("from: required")
        if end is None or end == "":
            return error_response("to: required")

        tags_dict: Optional[Dict[str, str]] = None
        if tags is not None and tags != "":
            tags_dict = {}
            for pair in tags.split(","):
                if ":" not in pair:
                    return error_response("tags: expected key:value pairs separated by commas")
                k, v = pair.split(":", 1)
                tags_dict[k] = v

        if role == "project" and caller_site is not None:
            if tags_dict is None:
                tags_dict = {"site": caller_site}
            elif "site" in tags_dict and tags_dict["site"] != caller_site:
                return forbidden_response("forbidden: this key can only read its own site")
            else:
                tags_dict["site"] = caller_site

        try:
            points = store.query(name=name, start=start, end=end, tags=tags_dict)
        except ValidationError as e:
            return error_response(str(e))

        return {"points": points}

    @app.get("/flags")
    async def flags(
        request: Request,
        name: Optional[str] = Query(default=None),
        start: Optional[str] = Query(default=None, alias="from"),
        end: Optional[str] = Query(default=None, alias="to"),
        tags: Optional[str] = Query(default=None),
        rule: Optional[str] = Query(default="zscore"),
        threshold: Optional[str] = Query(default="3.0"),
    ) -> Any:
        role, caller_site = authenticate(request)
        if role == "unauthorized":
            return unauthorized_response()

        if name is None or name == "":
            return error_response("name: required")
        if start is None or start == "":
            return error_response("from: required")
        if end is None or end == "":
            return error_response("to: required")

        tags_dict: Optional[Dict[str, str]] = None
        if tags is not None and tags != "":
            tags_dict = {}
            for pair in tags.split(","):
                if ":" not in pair:
                    return error_response("tags: expected key:value pairs separated by commas")
                k, v = pair.split(":", 1)
                tags_dict[k] = v

        if rule not in ("zscore", "ewma", "binary"):
            return error_response("rule: must be zscore, ewma, or binary")

        try:
            threshold_val = float(threshold) if threshold is not None else 0.0
        except (TypeError, ValueError):
            return error_response("threshold: must be a number greater than 0")
        if math.isnan(threshold_val) or math.isinf(threshold_val) or threshold_val <= 0:
            return error_response("threshold: must be a number greater than 0")

        if role == "project" and caller_site is not None:
            if tags_dict is None:
                tags_dict = {"site": caller_site}
            elif "site" in tags_dict and tags_dict["site"] != caller_site:
                return forbidden_response("forbidden: this key can only read its own site")
            else:
                tags_dict["site"] = caller_site

        try:
            points = store.query(name=name, start=start, end=end, tags=tags_dict)
        except ValidationError as e:
            return error_response(str(e))

        if not points:
            return {"rule": rule, "threshold": float(threshold_val), "flags": []}

        from detection.detector import binary_flags, ewma_flags, to_frame, zscore_flags

        df = to_frame(points)
        if rule == "zscore":
            raw_flags = zscore_flags(df, threshold=threshold_val)
        elif rule == "ewma":
            raw_flags = ewma_flags(df, threshold=threshold_val)
        else:
            raw_flags = binary_flags(df)

        cleaned_flags: List[Dict[str, Any]] = [
            {
                "name": str(f["name"]),
                "tags": {str(k): str(v) for k, v in dict(f["tags"]).items()},
                "ts": str(f["ts"]),
                "value": float(f["value"]),
                "score": float(f["score"]),
                "rule": str(f["rule"]),
            }
            for f in raw_flags
        ]
        cleaned_flags.sort(key=lambda item: item["ts"])

        return {
            "rule": rule,
            "threshold": float(threshold_val),
            "flags": cleaned_flags,
        }

    @app.get("/catalog")
    async def catalog(request: Request) -> Any:
        role, caller_site = authenticate(request)
        if role == "unauthorized":
            return unauthorized_response()

        raw_series = store.catalog()
        by_site: Dict[str, Dict[str, Dict[str, Any]]] = {}

        for item in raw_series:
            tags = item.get("tags") or {}
            site_val = tags.get("site")
            if role == "project":
                if not site_val or site_val != caller_site:
                    continue
                site_group = str(site_val)
            else:
                site_group = str(site_val) if site_val else "(untagged)"

            metric_name = str(item["name"])
            last_ts = str(item["last_ts"])

            site_metrics = by_site.setdefault(site_group, {})
            if metric_name not in site_metrics:
                site_metrics[metric_name] = {
                    "envs": set(),
                    "services": set(),
                    "last_ts": last_ts,
                }
            else:
                if last_ts > site_metrics[metric_name]["last_ts"]:
                    site_metrics[metric_name]["last_ts"] = last_ts

            env_val = tags.get("env")
            if env_val:
                site_metrics[metric_name]["envs"].add(str(env_val))

            service_val = tags.get("service")
            if service_val:
                site_metrics[metric_name]["services"].add(str(service_val))

        sites_list: List[Dict[str, Any]] = []
        for site_name in sorted(by_site.keys()):
            metrics_map = by_site[site_name]
            metrics_list: List[Dict[str, Any]] = []
            for m_name in sorted(metrics_map.keys()):
                m_info = metrics_map[m_name]
                metrics_list.append({
                    "name": m_name,
                    "envs": sorted(m_info["envs"]),
                    "services": sorted(m_info["services"]),
                    "last_ts": m_info["last_ts"],
                })
            sites_list.append({
                "site": site_name,
                "metrics": metrics_list,
            })

        return {"sites": sites_list}

    @app.get("/health")
    async def health() -> Any:
        return {"status": "ok", "points": len(store)}

    @app.get("/dashboard")
    async def dashboard() -> HTMLResponse:
        html_path = Path(__file__).parent / "static" / "dashboard.html"
        return HTMLResponse(content=html_path.read_text(encoding="utf-8"))

    return app
