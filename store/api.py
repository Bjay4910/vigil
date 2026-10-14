"""FastAPI application for time-series store."""

import os
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse
from store.engine import TimeSeriesStore, ValidationError


def error_response(message: str) -> JSONResponse:
    """Return a standard 400 JSON error response."""
    return JSONResponse(status_code=400, content={"error": message})


def create_app(store: Optional[TimeSeriesStore] = None) -> FastAPI:
    """Create and configure the FastAPI application."""
    if store is None:
        data_path = os.environ.get("VIGIL_DATA_PATH", "data/points.jsonl")
        store = TimeSeriesStore(data_path)

    app = FastAPI()

    @app.post("/ingest")
    async def ingest(request: Request) -> Any:
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
        name: Optional[str] = Query(default=None),
        start: Optional[str] = Query(default=None, alias="from"),
        end: Optional[str] = Query(default=None, alias="to"),
        tags: Optional[str] = Query(default=None),
    ) -> Any:
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

    @app.get("/health")
    async def health() -> Any:
        return {"status": "ok", "points": len(store)}

    return app
