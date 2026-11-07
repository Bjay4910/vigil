"""API integration tests for optional alerting in store/api.py."""

import time
from typing import List, Tuple

from fastapi.testclient import TestClient
import pytest

from store.alerts import Message
from store.api import create_app
import store.checks
from store.engine import TimeSeriesStore


def test_alert_lifespan_emits_single_down_alert_on_outage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def stub_down(url: str, timeout: float = 10.0) -> Tuple[bool, float]:
        return (False, 25.0)

    monkeypatch.setattr(store.checks, "check_url", stub_down)

    received: List[Message] = []

    def stub_send(msg: Message) -> bool:
        received.append(msg)
        return True

    app = create_app(
        TimeSeriesStore(),
        checks={"site-a": "https://example.com"},
        check_interval=0.05,
        alert_webhook="https://discord.com/api/webhooks/100000001/FakeTokenAbc123",
        alert_send=stub_send,
        alert_interval=0.05,
    )

    with TestClient(app):
        time.sleep(0.5)
        down_msgs = [m for m in received if m.kind == "down"]
        assert len(down_msgs) == 1
        assert "site-a" in down_msgs[0].text
        assert down_msgs[0].ping is True

        time.sleep(0.3)
        down_msgs_after = [m for m in received if m.kind == "down"]
        assert len(down_msgs_after) == 1


def test_invalid_alert_webhook_raises_on_create_app() -> None:
    with pytest.raises(ValueError) as exc_info:
        create_app(
            TimeSeriesStore(),
            alert_webhook="http://discord.com/api/webhooks/100/FakeToken",
        )
    assert str(exc_info.value).startswith("VIGIL_ALERT_WEBHOOK:")


def test_no_alert_webhook_runs_no_alert_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("VIGIL_ALERT_WEBHOOK", raising=False)
    received: List[Message] = []

    app = create_app(
        TimeSeriesStore(),
        alert_send=lambda m: (received.append(m) is None),
        alert_interval=0.05,
    )
    with TestClient(app) as client:
        time.sleep(0.15)
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"
    assert received == []

