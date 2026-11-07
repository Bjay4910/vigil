"""Shared pytest fixtures."""

import pytest


@pytest.fixture(autouse=True)
def clear_vigil_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure VIGIL_API_KEY from the shell environment never leaks into tests."""
    monkeypatch.delenv("VIGIL_API_KEY", raising=False)
    monkeypatch.delenv("VIGIL_PROJECT_KEYS", raising=False)
    monkeypatch.delenv("VIGIL_RATE_LIMIT", raising=False)
    monkeypatch.delenv("VIGIL_AUTH_FAIL_LIMIT", raising=False)
    monkeypatch.delenv("VIGIL_MAX_BODY_BYTES", raising=False)
    monkeypatch.delenv("VIGIL_MAX_POINTS", raising=False)

