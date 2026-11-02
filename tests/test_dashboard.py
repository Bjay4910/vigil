"""Tests for the GET /dashboard page."""

import re

from fastapi.testclient import TestClient
from store.api import create_app
from store.engine import TimeSeriesStore


def test_dashboard_returns_200_html_with_vigil_title() -> None:
    client = TestClient(create_app(TimeSeriesStore()))
    resp = client.get("/dashboard")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    assert "Vigil" in resp.text


def test_dashboard_is_open_when_api_key_is_set_and_never_leaks_key() -> None:
    secret = "test-key-123"
    client = TestClient(create_app(TimeSeriesStore(), api_key=secret))
    resp = client.get("/dashboard")
    assert resp.status_code == 200
    assert secret not in resp.text


def test_dashboard_contains_no_external_resource_references() -> None:
    client = TestClient(create_app(TimeSeriesStore()))
    html = client.get("/dashboard").text

    assert "<script src=" not in html.lower()
    attr_matches = re.findall(r"""(?:src|href)\s*=\s*['"]([^'"]*)['"]""", html, flags=re.IGNORECASE)
    for val in attr_matches:
        assert "http://" not in val.lower()
        assert "https://" not in val.lower()


def test_dashboard_does_not_use_browser_storage_or_cookies() -> None:
    client = TestClient(create_app(TimeSeriesStore()))
    html = client.get("/dashboard").text

    assert "localStorage" not in html
    assert "sessionStorage" not in html
    assert "document.cookie" not in html


def test_dashboard_does_not_use_inner_html() -> None:
    client = TestClient(create_app(TimeSeriesStore()))
    html = client.get("/dashboard").text

    assert "innerHTML" not in html

