"""FastAPI application for time-series store."""

import hmac
import math
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from store.engine import TimeSeriesStore, ValidationError


def error_response(message: str) -> JSONResponse:
    """Return a standard 400 JSON error response."""
    return JSONResponse(status_code=400, content={"error": message})


def unauthorized_response() -> JSONResponse:
    """Return a standard 401 JSON unauthorized response."""
    return JSONResponse(status_code=401, content={"error": "unauthorized"})


def create_app(
    store: Optional[TimeSeriesStore] = None,
    api_key: Optional[str] = None,
) -> FastAPI:
    """Create and configure the FastAPI application."""
    if store is None:
        data_path = os.environ.get("VIGIL_DATA_PATH", "data/points.jsonl")
        store = TimeSeriesStore(data_path)

    if api_key is None:
        api_key = os.environ.get("VIGIL_API_KEY")
    effective_key: Optional[str] = api_key if api_key else None

    def is_authorized(request: Request) -> bool:
        if effective_key is None:
            return True
        provided = request.headers.get("X-API-Key")
        if provided is None:
            return False
        return hmac.compare_digest(provided, effective_key)

    app = FastAPI()

    @app.post("/ingest")
    async def ingest(request: Request) -> Any:
        if not is_authorized(request):
            return unauthorized_response()

        try:
            body = await request.json()
        except Exception:
            return error_response("body: must be valid JSON")

        if not isinstance(body, dict) or "points" not in body or not isinstance(body["points"], list):
            return error_response("points: must be a list")

        points = body["points"]
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
        if not is_authorized(request):
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
        if not is_authorized(request):
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

        if rule not in ("zscore", "ewma"):
            return error_response("rule: must be zscore or ewma")

        try:
            threshold_val = float(threshold) if threshold is not None else 0.0
        except (TypeError, ValueError):
            return error_response("threshold: must be a number greater than 0")
        if math.isnan(threshold_val) or math.isinf(threshold_val) or threshold_val <= 0:
            return error_response("threshold: must be a number greater than 0")

        try:
            points = store.query(name=name, start=start, end=end, tags=tags_dict)
        except ValidationError as e:
            return error_response(str(e))

        if not points:
            return {"rule": rule, "threshold": float(threshold_val), "flags": []}

        from detection.detector import ewma_flags, to_frame, zscore_flags

        df = to_frame(points)
        if rule == "zscore":
            raw_flags = zscore_flags(df, threshold=threshold_val)
        else:
            raw_flags = ewma_flags(df, threshold=threshold_val)

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
