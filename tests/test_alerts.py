"""Unit tests for alerting monitor, rate limiter, and webhook formatting in store/alerts.py."""

from datetime import datetime, timedelta, timezone
import json
from typing import Any, Dict, List, Optional
import urllib.error
import urllib.request
from zoneinfo import ZoneInfo

import pytest

import store.alerts
from store.alerts import (
    AlertMonitor,
    Message,
    RateLimitedSender,
    _NoRedirectHandler,
    _fmt_local,
    _safe_label,
    parse_alert_env,
    parse_digest_hour,
    parse_max_per_hour,
    parse_timezone,
    post_webhook,
    run_cli_test,
    validate_webhook,
)
from store.engine import TimeSeriesStore


class FakeClock:
    """Controllable UTC clock for deterministic alert and digest tests."""

    def __init__(self, initial: datetime) -> None:
        self.current = initial

    def __call__(self) -> datetime:
        return self.current

    def advance(self, seconds: int) -> None:
        self.current = self.current + timedelta(seconds=seconds)


def test_validate_webhook_accepts_valid_and_rejects_invalid() -> None:
    valid_discord = "https://discord.com/api/webhooks/100000001/ FakeToken_Abc-123".replace(" ", "")
    valid_discordapp = "https://discordapp.com/api/webhooks/100000002/FakeToken_Def-456"
    valid_slack = "https://hooks.slack.com/services/T00000000/B00000000/FakeSlackToken000"

    assert validate_webhook(valid_discord) == valid_discord
    assert validate_webhook(valid_discordapp) == valid_discordapp
    assert validate_webhook(valid_slack) == valid_slack

    invalid_inputs = [
        "",
        "   ",
        "http://discord.com/api/webhooks/100/FakeToken",
        "https://example.com/api/webhooks/100/FakeToken",
        "https://discord.com.evil.example/api/webhooks/100/FakeToken",
        "https://user:pass@discord.com/api/webhooks/100/FakeToken",
        "https://discord.com/api/webhooks/100/FakeToken?wait=true",
        "https://discord.com/api/webhooks/100/FakeToken#frag",
        "https://discord.com:443/api/webhooks/100/FakeToken",
        "https://discord.com/api/webhooks/notanumber/FakeToken",
        "https://hooks.slack.com/services/",
        "https://hooks.slack.com/other/T00/B00/X00",
    ]
    for bad in invalid_inputs:
        with pytest.raises(ValueError) as exc_info:
            validate_webhook(bad)
        err_text = str(exc_info.value)
        assert err_text.startswith("VIGIL_ALERT_WEBHOOK:")
        if bad.strip():
            assert bad.strip() not in err_text
            assert "FakeToken" not in err_text


def test_parse_functions_defaults_and_error_prefixes() -> None:
    assert parse_digest_hour(None) == 8
    assert parse_digest_hour("") == 8
    assert parse_digest_hour("0") == 0
    assert parse_digest_hour("23") == 23
    for bad_hour in ("-1", "24", "noon"):
        with pytest.raises(ValueError) as exc_info:
            parse_digest_hour(bad_hour)
        assert str(exc_info.value).startswith("VIGIL_DIGEST_HOUR:")

    default_tz = parse_timezone(None)
    assert default_tz == ZoneInfo("America/New_York")
    assert parse_timezone("UTC") == ZoneInfo("UTC")
    with pytest.raises(ValueError) as exc_info:
        parse_timezone("Not/A_Real_Timezone")
    assert str(exc_info.value).startswith("VIGIL_DIGEST_TZ:")

    assert parse_max_per_hour(None) == 20
    assert parse_max_per_hour("") == 20
    assert parse_max_per_hour("1") == 1
    assert parse_max_per_hour("200") == 200
    for bad_max in ("0", "-5", "201", "many"):
        with pytest.raises(ValueError) as exc_info:
            parse_max_per_hour(bad_max)
        assert str(exc_info.value).startswith("VIGIL_ALERT_MAX_PER_HOUR:")

    assert parse_alert_env(None) == "prod"
    assert parse_alert_env("") == "prod"
    assert parse_alert_env("staging-1") == "staging-1"
    for bad_env in ("PROD", "1prod", "prod space"):
        with pytest.raises(ValueError) as exc_info:
            parse_alert_env(bad_env)
        assert str(exc_info.value).startswith("VIGIL_ALERT_ENV:")


def test_seed_with_down_site_sends_nothing_and_recovers_on_up() -> None:
    ts_store = TimeSeriesStore()
    clock = FakeClock(datetime(2026, 10, 6, 12, 0, 0, tzinfo=timezone.utc))
    ts_store.write([
        {
            "name": "uptime_up",
            "value": 0,
            "ts": "2026-10-06T12:00:00Z",
            "tags": {"site": "site-a", "env": "prod", "service": "uptime"},
        }
    ])

    monitor = AlertMonitor(
        ts_store,
        env="prod",
        tz=ZoneInfo("America/New_York"),
        digest_hour=8,
        now_fn=clock,
    )
    monitor.seed()
    assert monitor.poll() == []

    # 5 minutes later site-a recovers
    clock.advance(300)
    ts_store.write([
        {
            "name": "uptime_up",
            "value": 1,
            "ts": "2026-10-06T12:05:00Z",
            "tags": {"site": "site-a", "env": "prod", "service": "uptime"},
        }
    ])
    msgs = monitor.poll()
    assert len(msgs) == 1
    assert msgs[0].kind == "recovered"
    assert msgs[0].ping is False
    assert msgs[0].text == "Recovered: site-a is back up after 5 minutes (down since 8:00 AM EDT)"


def test_seed_with_up_site_then_down_and_repeated_zeros_and_recovery() -> None:
    ts_store = TimeSeriesStore()
    clock = FakeClock(datetime(2026, 10, 6, 12, 0, 0, tzinfo=timezone.utc))
    ts_store.write([
        {
            "name": "uptime_up",
            "value": 1,
            "ts": "2026-10-06T12:00:00Z",
            "tags": {"site": "site-a", "env": "prod", "service": "uptime"},
        }
    ])

    monitor = AlertMonitor(
        ts_store,
        env="prod",
        tz=ZoneInfo("America/New_York"),
        digest_hour=8,
        now_fn=clock,
    )
    monitor.seed()
    assert monitor.poll() == []

    # First 0 triggers one down alert with ping=True
    clock.advance(60)
    ts_store.write([
        {
            "name": "uptime_up",
            "value": 0,
            "ts": "2026-10-06T12:01:00Z",
            "tags": {"site": "site-a", "env": "prod", "service": "uptime"},
        }
    ])
    msgs1 = monitor.poll()
    assert len(msgs1) == 1
    assert msgs1[0] == Message(
        kind="down",
        text="DOWN: site-a failed its uptime check at 8:01 AM EDT",
        ping=True,
    )

    # Repeated 0 while already down sends nothing
    clock.advance(60)
    ts_store.write([
        {
            "name": "uptime_up",
            "value": 0,
            "ts": "2026-10-06T12:02:00Z",
            "tags": {"site": "site-a", "env": "prod", "service": "uptime"},
        }
    ])
    assert monitor.poll() == []

    # Recovery 3 minutes after 12:01 UTC
    clock.advance(120)
    ts_store.write([
        {
            "name": "uptime_up",
            "value": 1,
            "ts": "2026-10-06T12:04:00Z",
            "tags": {"site": "site-a", "env": "prod", "service": "uptime"},
        }
    ])
    msgs2 = monitor.poll()
    assert len(msgs2) == 1
    assert msgs2[0] == Message(
        kind="recovered",
        text="Recovered: site-a is back up after 3 minutes (down since 8:01 AM EDT)",
        ping=False,
    )


def test_form_failed_dedupe_and_filtering() -> None:
    ts_store = TimeSeriesStore()
    clock = FakeClock(datetime(2026, 10, 6, 14, 0, 0, tzinfo=timezone.utc))
    monitor = AlertMonitor(
        ts_store,
        env="prod",
        tz=ZoneInfo("America/New_York"),
        digest_hour=8,
        now_fn=clock,
    )
    monitor.seed()

    # Value 1 sends nothing; env=test sends nothing; missing site tag sends nothing
    ts_store.write([
        {
            "name": "contact_form_success",
            "value": 1,
            "ts": "2026-10-06T14:00:00Z",
            "tags": {"site": "site-a", "env": "prod", "service": "contact"},
        },
        {
            "name": "uptime_up",
            "value": 0,
            "ts": "2026-10-06T14:00:00Z",
            "tags": {"site": "site-a", "env": "test", "service": "uptime"},
        },
        {
            "name": "uptime_up",
            "value": 0,
            "ts": "2026-10-06T14:00:00Z",
            "tags": {"env": "prod", "service": "uptime"},
        },
    ])
    assert monitor.poll() == []

    # Value 0 on contact_form_success triggers form_failed
    clock.advance(60)
    ts_store.write([
        {
            "name": "contact_form_success",
            "value": 0,
            "ts": "2026-10-06T14:01:00Z",
            "tags": {"site": "site-a", "env": "prod", "service": "contact"},
        }
    ])
    msgs = monitor.poll()
    assert len(msgs) == 1
    assert msgs[0] == Message(
        kind="form_failed",
        text="FORM FAILED: site-a could not save a submission (contact) at 10:01 AM EDT",
        ping=True,
    )

    # Another failure for site-a 5 minutes later is suppressed (10-minute dedupe)
    clock.advance(300)
    ts_store.write([
        {
            "name": "form_submission_success",
            "value": 0,
            "ts": "2026-10-06T14:06:00Z",
            "tags": {"site": "site-a", "env": "prod"},
        }
    ])
    assert monitor.poll() == []

    # 10 minutes after first alert (14:11 UTC), a new failure alerts again (default service 'form')
    clock.advance(300)
    ts_store.write([
        {
            "name": "form_submission_success",
            "value": 0,
            "ts": "2026-10-06T14:11:00Z",
            "tags": {"site": "site-a", "env": "prod"},
        }
    ])
    msgs_after = monitor.poll()
    assert len(msgs_after) == 1
    assert msgs_after[0] == Message(
        kind="form_failed",
        text="FORM FAILED: site-a could not save a submission (form) at 10:11 AM EDT",
        ping=True,
    )


def test_digest_schedule_and_contents() -> None:
    tz = ZoneInfo("America/New_York")
    # 2026-10-06 is EDT (UTC-4): 07:59 EDT is 11:59 UTC, 08:00 EDT is 12:00 UTC, 14:01 EDT is 18:01 UTC
    clock = FakeClock(datetime(2026, 10, 6, 11, 59, 0, tzinfo=timezone.utc))
    ts_store = TimeSeriesStore()
    monitor = AlertMonitor(ts_store, env="prod", tz=tz, digest_hour=8, now_fn=clock)

    # Before 8 AM local -> None
    assert monitor.digest() is None

    # More than 6 hours late -> None
    clock.current = datetime(2026, 10, 6, 18, 1, 0, tzinfo=timezone.utc)
    assert monitor.digest() is None

    # At 8 AM local with no data -> "No uptime data in the last 24 hours."
    clock.current = datetime(2026, 10, 6, 12, 0, 0, tzinfo=timezone.utc)
    empty_msg = monitor.digest()
    assert empty_msg is not None
    assert empty_msg.kind == "digest"
    assert empty_msg.ping is False
    assert empty_msg.text == (
        "Vigil daily summary, Tue Oct 6\nNo uptime data in the last 24 hours."
    )
    # Calling again the same local day returns None
    assert monitor.digest() is None

    # Next day (Wed Oct 7, 12:00 UTC): all healthy, full 24h coverage (oldest point 23h30m ago)
    clock.current = datetime(2026, 10, 7, 12, 0, 0, tzinfo=timezone.utc)
    ts_store.write([
        {
            "name": "uptime_up",
            "value": 1,
            "ts": "2026-10-06T12:30:00Z",
            "tags": {"site": "site-a", "env": "prod", "service": "uptime"},
        },
        {
            "name": "response_ms",
            "value": 110,
            "ts": "2026-10-06T12:30:00Z",
            "tags": {"site": "site-a", "env": "prod", "service": "uptime"},
        },
        {
            "name": "uptime_up",
            "value": 1,
            "ts": "2026-10-07T11:55:00Z",
            "tags": {"site": "site-a", "env": "prod", "service": "uptime"},
        },
        {
            "name": "response_ms",
            "value": 130,
            "ts": "2026-10-07T11:55:00Z",
            "tags": {"site": "site-a", "env": "prod", "service": "uptime"},
        },
    ])
    healthy_msg = monitor.digest()
    assert healthy_msg is not None
    assert healthy_msg.text.endswith("All sites healthy.")
    assert "site-a: 100.0% uptime, 0 failed checks, avg response 120 ms" in healthy_msg.text
    assert "Data since" not in healthy_msg.text

    # Next day (Thu Oct 8, 12:00 UTC): failed check on site-a, stale site-b, and <24h data
    fresh_store = TimeSeriesStore()
    clock.current = datetime(2026, 10, 8, 12, 0, 0, tzinfo=timezone.utc)
    monitor2 = AlertMonitor(fresh_store, env="prod", tz=tz, digest_hour=8, now_fn=clock)
    fresh_store.write([
        {
            "name": "uptime_up",
            "value": 0,
            "ts": "2026-10-08T11:50:00Z",
            "tags": {"site": "site-a", "env": "prod", "service": "uptime"},
        },
        {
            "name": "uptime_up",
            "value": 1,
            "ts": "2026-10-08T10:00:00Z",
            "tags": {"site": "site-b", "env": "prod", "service": "uptime"},
        },
    ])
    attn_msg = monitor2.digest()
    assert attn_msg is not None
    assert "site-b: 100.0% uptime, 0 failed checks, avg response n/a (no recent checks)" in attn_msg.text
    assert "Data since 6:00 AM EDT (store restarted or newly set up)" in attn_msg.text
    assert attn_msg.text.endswith("Needs attention: site-a, site-b")


def test_digest_on_dst_transition_days() -> None:
    tz = ZoneInfo("America/New_York")
    ts_store = TimeSeriesStore()

    # Spring-forward: 2026-03-08 (08:00 EDT is 12:00 UTC)
    clock_spring = FakeClock(datetime(2026, 3, 8, 12, 0, 0, tzinfo=timezone.utc))
    mon_spring = AlertMonitor(ts_store, env="prod", tz=tz, digest_hour=8, now_fn=clock_spring)
    assert mon_spring.digest() is not None
    clock_spring.advance(3600)
    assert mon_spring.digest() is None

    # Fall-back: 2026-11-01 (08:00 EST is 13:00 UTC)
    clock_fall = FakeClock(datetime(2026, 11, 1, 13, 0, 0, tzinfo=timezone.utc))
    mon_fall = AlertMonitor(ts_store, env="prod", tz=tz, digest_hour=8, now_fn=clock_fall)
    assert mon_fall.digest() is not None
    clock_fall.advance(3600)
    assert mon_fall.digest() is None


def test_rate_limited_sender() -> None:
    clock = FakeClock(datetime(2026, 10, 6, 12, 0, 0, tzinfo=timezone.utc))
    sent: List[Message] = []
    limiter = RateLimitedSender(
        send_fn=lambda m: (sent.append(m) is None),
        max_per_hour=2,
        now_fn=clock,
    )

    m1 = Message("down", "down 1", True)
    m2 = Message("recovered", "rec 1", False)
    m3 = Message("down", "down 2", True)
    digest_msg = Message("digest", "summary", False)

    assert limiter.send(m1) is True
    assert limiter.send(m2) is True
    assert limiter.send(m3) is False
    assert limiter.suppressed == 1

    # Digest is exempt from rate limit
    assert limiter.send(digest_msg) is True
    assert limiter.suppressed == 1
    assert len(sent) == 3

    # Advance past 1 hour -> window resets
    clock.advance(3601)
    assert limiter.send(m3) is True
    assert len(sent) == 4


def test_post_webhook_discord_slack_and_failures(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    captured_requests: List[urllib.request.Request] = []
    response_status = 204
    should_raise = False

    class FakeResponse:
        def __init__(self, status: int) -> None:
            self.status = status

        def read(self, _amt: Optional[int] = None) -> bytes:
            return b""

        def getcode(self) -> int:
            return self.status

        def __enter__(self) -> "FakeResponse":
            return self

        def __exit__(self, *args: object) -> None:
            pass

    def fake_open(
        self: urllib.request.OpenerDirector,
        fullurl: Any,
        data: Any = None,
        timeout: Optional[float] = None,
    ) -> FakeResponse:
        if isinstance(fullurl, urllib.request.Request):
            captured_requests.append(fullurl)
        if should_raise:
            raise urllib.error.URLError("connection refused")
        return FakeResponse(response_status)

    monkeypatch.setattr(urllib.request.OpenerDirector, "open", fake_open)

    discord_url = "https://discord.com/api/webhooks/100/SecretTokenValue"
    slack_url = "https://hooks.slack.com/services/T00/B00/SecretSlackValue"

    # Discord ping=True
    assert post_webhook(discord_url, Message("down", "DOWN: site-a", True)) is True
    body_ping = json.loads(captured_requests[-1].data.decode("utf-8"))  # type: ignore[union-attr]
    assert body_ping["content"] == "@everyone DOWN: site-a"
    assert body_ping["allowed_mentions"] == {"parse": ["everyone"]}
    assert "flags" not in body_ping

    # Discord ping=False (silent)
    assert post_webhook(discord_url, Message("recovered", "Recovered: site-a", False)) is True
    body_silent = json.loads(captured_requests[-1].data.decode("utf-8"))  # type: ignore[union-attr]
    assert body_silent["content"] == "Recovered: site-a"
    assert body_silent["allowed_mentions"] == {"parse": []}
    assert body_silent["flags"] == 4096

    # Slack ping=True and ping=False
    assert post_webhook(slack_url, Message("down", "DOWN: site-a", True)) is True
    slack_ping = json.loads(captured_requests[-1].data.decode("utf-8"))  # type: ignore[union-attr]
    assert slack_ping == {"text": "<!channel> DOWN: site-a"}

    assert post_webhook(slack_url, Message("digest", "summary", False)) is True
    slack_silent = json.loads(captured_requests[-1].data.decode("utf-8"))  # type: ignore[union-attr]
    assert slack_silent == {"text": "summary"}

    # Redirect handler refuses redirects
    handler = _NoRedirectHandler()
    dummy_req = urllib.request.Request(discord_url)
    assert handler.redirect_request(dummy_req, None, 302, "Found", {}, "https://example.com") is None

    # Status 500 returns False without raising or printing URL
    response_status = 500
    assert post_webhook(discord_url, Message("down", "DOWN: site-a", True)) is False

    # Exception returns False without raising or printing URL
    should_raise = True
    assert post_webhook(discord_url, Message("down", "DOWN: site-a", True)) is False

    out, err = capsys.readouterr()
    assert "SecretTokenValue" not in out
    assert "SecretTokenValue" not in err


def test_cli_test_helper(capsys: pytest.CaptureFixture[str]) -> None:
    assert run_cli_test(environ={}) == 1
    assert run_cli_test(environ={"VIGIL_ALERT_WEBHOOK": "http://bad.example/hook"}) == 1

    sent_msgs: List[Message] = []
    code = run_cli_test(
        environ={"VIGIL_ALERT_WEBHOOK": "https://discord.com/api/webhooks/100/SecretTokenValue"},
        post_fn=lambda url, msg: (sent_msgs.append(msg) is None),
    )
    assert code == 0
    assert len(sent_msgs) == 2
    assert sent_msgs[0].ping is True
    assert sent_msgs[1].ping is False
    out, err = capsys.readouterr()
    assert "sent 2 of 2" in out
    assert "SecretTokenValue" not in out
    assert "SecretTokenValue" not in err


def test_fmt_local() -> None:
    tz = ZoneInfo("America/New_York")
    # Winter (EST, UTC-5): 2026-01-15 20:07:00 UTC -> 3:07 PM EST
    winter_epoch = int(datetime(2026, 1, 15, 20, 7, 0, tzinfo=timezone.utc).timestamp())
    assert _fmt_local(winter_epoch, tz) == "3:07 PM EST"

    # Summer (EDT, UTC-4): 2026-07-15 19:07:00 UTC -> 3:07 PM EDT
    summer_epoch = int(datetime(2026, 7, 15, 19, 7, 0, tzinfo=timezone.utc).timestamp())
    assert _fmt_local(summer_epoch, tz) == "3:07 PM EDT"

    # Noon (EST, UTC-5): 2026-01-15 17:00:00 UTC -> 12:00 PM EST
    noon_epoch = int(datetime(2026, 1, 15, 17, 0, 0, tzinfo=timezone.utc).timestamp())
    assert _fmt_local(noon_epoch, tz) == "12:00 PM EST"

    # Midnight (EST, UTC-5): 2026-01-15 05:00:00 UTC -> 12:00 AM EST
    midnight_epoch = int(datetime(2026, 1, 15, 5, 0, 0, tzinfo=timezone.utc).timestamp())
    assert _fmt_local(midnight_epoch, tz) == "12:00 AM EST"


def test_safe_label() -> None:
    for valid_name in ("csservicestx", "thrive-aspirehi", "a_b"):
        assert _safe_label(valid_name, "unknown-site") == valid_name

    for rejected_name in (
        "@everyone",
        "Bad Name",
        "http://x.example",
        "",
        "a" * 41,
    ):
        assert _safe_label(rejected_name, "unknown-site") == "unknown-site"
        assert _safe_label(rejected_name, "form") == "form"


def test_down_message_sanitizes_unsafe_site_tag() -> None:
    ts_store = TimeSeriesStore()
    clock = FakeClock(datetime(2026, 10, 6, 12, 1, 0, tzinfo=timezone.utc))
    monitor = AlertMonitor(
        ts_store,
        env="prod",
        tz=ZoneInfo("America/New_York"),
        digest_hour=8,
        now_fn=clock,
    )
    monitor.seed()

    ts_store.write([
        {
            "name": "uptime_up",
            "value": 0,
            "ts": "2026-10-06T12:01:00Z",
            "tags": {"site": "@everyone", "env": "prod", "service": "uptime"},
        }
    ])
    msgs = monitor.poll()
    assert len(msgs) == 1
    assert msgs[0].kind == "down"
    assert "unknown-site" in msgs[0].text
    assert "@everyone" not in msgs[0].text


