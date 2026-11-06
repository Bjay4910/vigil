"""FastAPI application for time-series store."""

import hmac
import math
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from store.engine import TimeSeriesStore, ValidationError

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

    def authenticate(request: Request) -> Tuple[str, Optional[str]]:
        if effective_key is None and not effective_project_keys:
            return ("open", None)
        provided = request.headers.get("X-API-Key")
        if provided is None:
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

        return (matched_role, matched_site)

    app = FastAPI()

    @app.post("/ingest")
    async def ingest(request: Request) -> Any:
        role, caller_site = authenticate(request)
        if role == "unauthorized":
            return unauthorized_response()

        try:
            body = await request.json()
        except Exception:
            return error_response("body: must be valid JSON")

        if not isinstance(body, dict) or "points" not in body or not isinstance(body["points"], list):
            return error_response("points: must be a list")

        points = body["points"]
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

    @app.get("/health")
    async def health() -> Any:
        return {"status": "ok", "points": len(store)}

    @app.get("/dashboard")
    async def dashboard() -> HTMLResponse:
        html_path = Path(__file__).parent / "static" / "dashboard.html"
        return HTMLResponse(content=html_path.read_text(encoding="utf-8"))

    return app
