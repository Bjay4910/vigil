"""Tests for API key headers in detection scripts."""

import json
import urllib.request
from typing import Any, Dict, List, Optional

import pytest

from detection.detector import fetch
from detection.generate import send

SAMPLE_POINT: Dict[str, Any] = {
    "name": "page_load_ms",
    "value": 850.0,
    "ts": "2026-10-06T10:00:00Z",
    "tags": {"client": "acme", "env": "prod"},
}


class _FakeResponse:
    """Minimal context-manager response matching urllib.request.urlopen."""

    def __init__(self, payload: Dict[str, Any]) -> None:
        self._raw = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self._raw

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        return False


def _header_map(req: urllib.request.Request) -> Dict[str, str]:
    """Return all headers from a Request with lowercase keys."""
    return {k.lower(): v for k, v in req.header_items()}


def test_generate_send_adds_api_key_when_set_and_omits_when_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: List[urllib.request.Request] = []

    def fake_urlopen(req: urllib.request.Request, *args: Any, **kwargs: Any) -> _FakeResponse:
        assert isinstance(req, urllib.request.Request)
        captured.append(req)
        return _FakeResponse({"accepted": 1})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    monkeypatch.setenv("VIGIL_API_KEY", "secret-key-acme")
    accepted = send([SAMPLE_POINT], "http://localhost:8000")
    assert accepted == 1
    assert len(captured) == 1
    assert _header_map(captured[0]).get("x-api-key") == "secret-key-acme"

    monkeypatch.delenv("VIGIL_API_KEY", raising=False)
    send([SAMPLE_POINT], "http://localhost:8000")
    assert len(captured) == 2
    assert "x-api-key" not in _header_map(captured[1])


def test_detector_fetch_adds_api_key_when_set_and_omits_when_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: List[urllib.request.Request] = []

    def fake_urlopen(req: urllib.request.Request, *args: Any, **kwargs: Any) -> _FakeResponse:
        assert isinstance(req, urllib.request.Request)
        captured.append(req)
        return _FakeResponse({"points": [SAMPLE_POINT]})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    monkeypatch.setenv("VIGIL_API_KEY", "secret-key-acme")
    df = fetch(
        "http://localhost:8000",
        "page_load_ms",
        "2026-10-06T00:00:00Z",
        "2026-10-07T00:00:00Z",
    )
    assert len(df) == 1
    assert len(captured) == 1
    assert _header_map(captured[0]).get("x-api-key") == "secret-key-acme"

    monkeypatch.delenv("VIGIL_API_KEY", raising=False)
    fetch(
        "http://localhost:8000",
        "page_load_ms",
        "2026-10-06T00:00:00Z",
        "2026-10-07T00:00:00Z",
    )
    assert len(captured) == 2
    assert "x-api-key" not in _header_map(captured[1])


def test_empty_api_key_adds_no_header_for_both(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: List[urllib.request.Request] = []

    def fake_urlopen(req: urllib.request.Request, *args: Any, **kwargs: Any) -> _FakeResponse:
        assert isinstance(req, urllib.request.Request)
        captured.append(req)
        return _FakeResponse({"accepted": 1, "points": [SAMPLE_POINT]})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setenv("VIGIL_API_KEY", "")

    send([SAMPLE_POINT], "http://localhost:8000")
    fetch(
        "http://localhost:8000",
        "page_load_ms",
        "2026-10-06T00:00:00Z",
        "2026-10-07T00:00:00Z",
    )

    assert len(captured) == 2
    assert "x-api-key" not in _header_map(captured[0])
    assert "x-api-key" not in _header_map(captured[1])

