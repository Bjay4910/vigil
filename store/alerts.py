"""Alerting monitor, rate-limited sender, and Discord/Slack webhook delivery."""

import asyncio
from dataclasses import dataclass
from datetime import date, datetime, timezone
import inspect
import json
import os
import re
import sys
from typing import Any, Callable, Dict, List, Optional, Set, Tuple
import urllib.error
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

from store.engine import format_ts, parse_ts

_DISCORD_PATH_RE = re.compile(r"^/api/webhooks/[0-9]+/[A-Za-z0-9_-]+$")
_SLACK_PATH_RE = re.compile(r"^/services/[A-Za-z0-9/]+$")
_ENV_RE = re.compile(r"^[a-z][a-z0-9_-]*$")
_SAFE_LABEL_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")
_USER_AGENT = "Vigil-alerts/1.0"


def _fmt_local(epoch: int, tz: ZoneInfo) -> str:
    """Format a UTC epoch timestamp in tz as '3:07 PM EST'."""
    dt = datetime.fromtimestamp(epoch, tz=timezone.utc).astimezone(tz)
    h = dt.hour
    hour_12 = h % 12 or 12
    ampm = "AM" if h < 12 else "PM"
    tz_abbr = dt.strftime("%Z")
    return f"{hour_12}:{dt.minute:02d} {ampm} {tz_abbr}"


def _safe_label(value: Optional[str], default: str) -> str:
    """Return value if it matches ^[a-z0-9][a-z0-9_-]{0,39}$, else default."""
    if isinstance(value, str) and _SAFE_LABEL_RE.match(value):
        return value
    return default


def validate_webhook(url: str) -> str:
    """Validate a Discord or Slack webhook URL without ever leaking the URL in errors."""
    if not isinstance(url, str) or not url.strip() or url != url.strip():
        raise ValueError("VIGIL_ALERT_WEBHOOK: invalid webhook URL")
    if any(ch.isspace() for ch in url):
        raise ValueError("VIGIL_ALERT_WEBHOOK: invalid webhook URL")

    try:
        parsed = urllib.parse.urlsplit(url)
    except ValueError as exc:
        raise ValueError("VIGIL_ALERT_WEBHOOK: invalid webhook URL") from exc

    if parsed.scheme.lower() != "https":
        raise ValueError("VIGIL_ALERT_WEBHOOK: webhook URL must use https")

    if (
        parsed.username is not None
        or parsed.password is not None
        or "@" in (parsed.netloc or "")
    ):
        raise ValueError("VIGIL_ALERT_WEBHOOK: webhook URL must not contain credentials")

    if parsed.query or parsed.fragment or "?" in url or "#" in url:
        raise ValueError("VIGIL_ALERT_WEBHOOK: webhook URL must not have query or fragment")

    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("VIGIL_ALERT_WEBHOOK: webhook URL must not specify a port") from exc
    if port is not None or ":" in (parsed.netloc or ""):
        raise ValueError("VIGIL_ALERT_WEBHOOK: webhook URL must not specify a port")

    host = (parsed.hostname or "").lower()
    path = parsed.path or ""

    if host in ("discord.com", "discordapp.com"):
        if not _DISCORD_PATH_RE.match(path):
            raise ValueError("VIGIL_ALERT_WEBHOOK: invalid Discord webhook path")
        return url

    if host == "hooks.slack.com":
        if not _SLACK_PATH_RE.match(path) or path == "/services/":
            raise ValueError("VIGIL_ALERT_WEBHOOK: invalid Slack webhook path")
        return url

    raise ValueError("VIGIL_ALERT_WEBHOOK: host must be discord.com, discordapp.com, or hooks.slack.com")


def parse_digest_hour(raw: Optional[str]) -> int:
    """Parse VIGIL_DIGEST_HOUR (default 8, 0..23)."""
    if raw is None or not raw.strip():
        return 8
    stripped = raw.strip()
    if not re.match(r"^-?\d+$", stripped):
        raise ValueError("VIGIL_DIGEST_HOUR: must be an integer between 0 and 23")
    val = int(stripped)
    if val < 0 or val > 23:
        raise ValueError("VIGIL_DIGEST_HOUR: must be between 0 and 23")
    return val


def parse_timezone(raw: Optional[str]) -> ZoneInfo:
    """Parse VIGIL_DIGEST_TZ (default America/New_York)."""
    if raw is None or not raw.strip():
        return ZoneInfo("America/New_York")
    tz_name = raw.strip()
    try:
        return ZoneInfo(tz_name)
    except Exception as exc:
        raise ValueError("VIGIL_DIGEST_TZ: unknown or invalid timezone") from exc


def parse_max_per_hour(raw: Optional[str]) -> int:
    """Parse VIGIL_ALERT_MAX_PER_HOUR (default 20, 1..200)."""
    if raw is None or not raw.strip():
        return 20
    stripped = raw.strip()
    if not re.match(r"^-?\d+$", stripped):
        raise ValueError("VIGIL_ALERT_MAX_PER_HOUR: must be an integer between 1 and 200")
    val = int(stripped)
    if val < 1 or val > 200:
        raise ValueError("VIGIL_ALERT_MAX_PER_HOUR: must be between 1 and 200")
    return val


def parse_alert_env(raw: Optional[str]) -> str:
    """Parse VIGIL_ALERT_ENV (default prod)."""
    if raw is None or not raw.strip():
        return "prod"
    stripped = raw.strip()
    if not _ENV_RE.match(stripped):
        raise ValueError("VIGIL_ALERT_ENV: must match ^[a-z][a-z0-9_-]*$")
    return stripped


@dataclass
class Message:
    """Alert or digest notification message."""

    kind: str
    text: str
    ping: bool


class AlertMonitor:
    """Stateful alert evaluator over a TimeSeriesStore."""

    def __init__(
        self,
        store: Any,
        env: str,
        tz: ZoneInfo,
        digest_hour: int,
        now_fn: Optional[Callable[[], datetime]] = None,
    ) -> None:
        self.store = store
        self.env = env
        self.tz = tz
        self.digest_hour = digest_hour
        self.now_fn: Callable[[], datetime] = (
            now_fn if now_fn is not None else (lambda: datetime.now(timezone.utc))
        )
        self.seeded: bool = False
        self.last_seen: Dict[Tuple[str, Tuple[Tuple[str, str], ...]], int] = {}
        self.site_state: Dict[str, str] = {}
        self.down_since: Dict[str, int] = {}
        self.last_form_alert: Dict[str, int] = {}
        self.last_digest_date: Optional[date] = None

    def seed(self) -> None:
        """One-time silent startup initialization from existing store data."""
        for item in self.store.catalog():
            tags = item.get("tags") or {}
            if tags.get("env") != self.env:
                continue
            name = str(item["name"])
            last_ts_str = str(item["last_ts"])
            last_epoch = parse_ts(last_ts_str)
            series_key = (name, tuple(sorted(tags.items())))
            self.last_seen[series_key] = last_epoch

            if name == "uptime_up":
                raw_site = tags.get("site")
                if raw_site:
                    site = _safe_label(str(raw_site), "unknown-site")
                    pts = self.store.query(
                        "uptime_up",
                        last_ts_str,
                        format_ts(last_epoch + 1),
                        tags=tags,
                    )
                    if pts:
                        newest = pts[-1]
                        if float(newest["value"]) == 0.0:
                            self.site_state[site] = "down"
                            self.down_since[site] = last_epoch
                        else:
                            self.site_state[site] = "up"
                            self.down_since.pop(site, None)

        self.seeded = True

    def poll(self) -> List[Message]:
        """Inspect points from the last 15 minutes and emit new transition messages."""
        now_dt = self.now_fn()
        now_epoch = int(now_dt.timestamp())
        start_str = format_ts(now_epoch - 15 * 60)
        end_str = format_ts(now_epoch + 1)

        messages: List[Message] = []
        watched = ("uptime_up", "form_submission_success", "contact_form_success")

        for metric_name in watched:
            pts = self.store.query(metric_name, start_str, end_str)
            matching: List[Tuple[int, Dict[str, Any]]] = []
            for pt in pts:
                tags = pt.get("tags") or {}
                if tags.get("env") != self.env:
                    continue
                pt_epoch = parse_ts(pt["ts"])
                matching.append((pt_epoch, pt))

            matching.sort(key=lambda item: item[0])

            for pt_epoch, pt in matching:
                tags = pt.get("tags") or {}
                series_key = (metric_name, tuple(sorted(tags.items())))
                prev_seen = self.last_seen.get(series_key)
                if prev_seen is not None and pt_epoch <= prev_seen:
                    continue
                self.last_seen[series_key] = pt_epoch

                raw_site = tags.get("site")
                if not raw_site:
                    continue
                site = _safe_label(str(raw_site), "unknown-site")

                val = float(pt["value"])
                local_str = _fmt_local(pt_epoch, self.tz)

                if metric_name == "uptime_up":
                    state = self.site_state.get(site, "up")
                    if val == 0.0:
                        if state != "down":
                            self.site_state[site] = "down"
                            self.down_since[site] = pt_epoch
                            messages.append(
                                Message(
                                    kind="down",
                                    text=f"DOWN: {site} failed its uptime check at {local_str}",
                                    ping=True,
                                )
                            )
                    elif val == 1.0:
                        if state == "down":
                            started = self.down_since.get(site, pt_epoch)
                            minutes = max(1, (pt_epoch - started) // 60)
                            since_str = _fmt_local(started, self.tz)
                            self.site_state[site] = "up"
                            self.down_since.pop(site, None)
                            messages.append(
                                Message(
                                    kind="recovered",
                                    text=(
                                        f"Recovered: {site} is back up after {minutes} minutes "
                                        f"(down since {since_str})"
                                    ),
                                    ping=False,
                                )
                            )
                        else:
                            self.site_state[site] = "up"
                else:
                    if val == 0.0:
                        last_alert = self.last_form_alert.get(site)
                        if last_alert is None or (pt_epoch - last_alert) >= 600:
                            self.last_form_alert[site] = pt_epoch
                            service_label = _safe_label(tags.get("service"), "form")
                            messages.append(
                                Message(
                                    kind="form_failed",
                                    text=(
                                        f"FORM FAILED: {site} could not save a submission "
                                        f"({service_label}) at {local_str}"
                                    ),
                                    ping=True,
                                )
                            )

        return messages

    def digest(self) -> Optional[Message]:
        """Build the daily summary message if due in self.tz."""
        now_utc = self.now_fn()
        now_local = now_utc.astimezone(self.tz)
        today_local = now_local.date()

        if self.last_digest_date == today_local:
            return None

        seconds_since_midnight = (
            now_local.hour * 3600 + now_local.minute * 60 + now_local.second
        )
        target_seconds = self.digest_hour * 3600
        elapsed_since_target = seconds_since_midnight - target_seconds
        if elapsed_since_target < 0 or elapsed_since_target >= 6 * 3600:
            return None

        self.last_digest_date = today_local

        now_epoch = int(now_utc.timestamp())
        start_24h = format_ts(now_epoch - 24 * 3600)
        end_24h = format_ts(now_epoch + 1)

        first_line = (
            f"Vigil daily summary, {now_local.strftime('%a')} "
            f"{now_local.strftime('%b')} {now_local.day}"
        )

        uptime_pts = [
            p
            for p in self.store.query("uptime_up", start_24h, end_24h)
            if (p.get("tags") or {}).get("env") == self.env
            and (p.get("tags") or {}).get("site")
        ]

        if not uptime_pts:
            return Message(
                kind="digest",
                text=first_line + "\nNo uptime data in the last 24 hours.",
                ping=False,
            )

        resp_pts = [
            p
            for p in self.store.query("response_ms", start_24h, end_24h)
            if (p.get("tags") or {}).get("env") == self.env
            and (p.get("tags") or {}).get("site")
        ]

        by_site_uptime: Dict[str, List[Dict[str, Any]]] = {}
        for p in uptime_pts:
            s = _safe_label(str(p["tags"]["site"]), "unknown-site")
            by_site_uptime.setdefault(s, []).append(p)

        by_site_resp: Dict[str, List[float]] = {}
        for p in resp_pts:
            s = _safe_label(str(p["tags"]["site"]), "unknown-site")
            by_site_resp.setdefault(s, []).append(float(p["value"]))

        lines: List[str] = [first_line]
        attention_sites: List[str] = []

        for site in sorted(by_site_uptime.keys()):
            s_pts = by_site_uptime[site]
            total = len(s_pts)
            up_count = sum(1 for p in s_pts if float(p["value"]) == 1.0)
            failed_count = total - up_count
            pct = (up_count / total) * 100.0

            r_vals = by_site_resp.get(site, [])
            if r_vals:
                avg_ms = int(round(sum(r_vals) / len(r_vals)))
                avg_str = f"{avg_ms} ms"
            else:
                avg_str = "n/a"

            newest_epoch = max(parse_ts(p["ts"]) for p in s_pts)
            stale = (now_epoch - newest_epoch) > 30 * 60

            line = (
                f"{site}: {pct:.1f}% uptime, {failed_count} failed checks, "
                f"avg response {avg_str}"
            )
            if stale:
                line += " (no recent checks)"
            lines.append(line)

            if failed_count > 0 or stale:
                attention_sites.append(site)

        oldest_epoch = min(parse_ts(p["ts"]) for p in uptime_pts)
        if (now_epoch - oldest_epoch) < 23 * 3600:
            oldest_str = _fmt_local(oldest_epoch, self.tz)
            lines.append(
                f"Data since {oldest_str} (store restarted or newly set up)"
            )

        form_failures = 0
        form_failed_sites: Set[str] = set()
        for form_metric in ("form_submission_success", "contact_form_success"):
            for p in self.store.query(form_metric, start_24h, end_24h):
                tags = p.get("tags") or {}
                if tags.get("env") == self.env and float(p["value"]) == 0.0:
                    form_failures += 1
                    if tags.get("site"):
                        form_failed_sites.add(
                            _safe_label(str(tags["site"]), "unknown-site")
                        )

        if form_failures > 0:
            lines.append(f"Form failures: {form_failures}")
            for s in sorted(form_failed_sites):
                if s not in attention_sites:
                    attention_sites.append(s)

        if not attention_sites and form_failures == 0:
            lines.append("All sites healthy.")
        else:
            lines.append("Needs attention: " + ", ".join(attention_sites))

        return Message(kind="digest", text="\n".join(lines), ping=False)


class RateLimitedSender:
    """Rate-limits outgoing alerts to max_per_hour per rolling hour, exempting digests."""

    def __init__(
        self,
        send_fn: Callable[[Message], bool],
        max_per_hour: int,
        now_fn: Optional[Callable[[], datetime]] = None,
    ) -> None:
        self.send_fn = send_fn
        self.max_per_hour = max_per_hour
        self.now_fn: Callable[[], datetime] = (
            now_fn if now_fn is not None else (lambda: datetime.now(timezone.utc))
        )
        self.suppressed: int = 0
        self._sent_timestamps: List[float] = []

    def send(self, message: Message) -> bool:
        if message.kind == "digest":
            return bool(self.send_fn(message))

        now_ts = self.now_fn().timestamp()
        cutoff = now_ts - 3600.0
        self._sent_timestamps = [t for t in self._sent_timestamps if t > cutoff]

        if len(self._sent_timestamps) >= self.max_per_hour:
            self.suppressed += 1
            return False

        self._sent_timestamps.append(now_ts)
        return bool(self.send_fn(message))


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Redirect handler that refuses all HTTP redirects."""

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> Optional[urllib.request.Request]:
        return None


def post_webhook(url: str, message: Message, timeout: float = 10.0) -> bool:
    """Send a Message to a validated Discord or Slack webhook URL. Never raises."""
    try:
        parsed = urllib.parse.urlsplit(url)
        host = (parsed.hostname or "").lower()

        if host in ("discord.com", "discordapp.com"):
            prefix = "@everyone " if message.ping else ""
            content = (prefix + message.text)[:1900]
            if message.ping:
                payload: Dict[str, Any] = {
                    "content": content,
                    "allowed_mentions": {"parse": ["everyone"]},
                }
            else:
                payload = {
                    "content": content,
                    "allowed_mentions": {"parse": []},
                    "flags": 4096,
                }
        elif host == "hooks.slack.com":
            prefix = "<!channel> " if message.ping else ""
            payload = {"text": prefix + message.text}
        else:
            return False

        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            headers={
                "User-Agent": _USER_AGENT,
                "Content-Type": "application/json",
            },
            method="POST",
        )
        opener = urllib.request.build_opener(_NoRedirectHandler())
        with opener.open(req, timeout=timeout) as resp:
            resp.read(65536)
            status = getattr(resp, "status", None)
            if status is None:
                status = resp.getcode()
        return status is not None and 200 <= int(status) <= 299
    except Exception:
        return False


async def run_alerts(
    monitor: AlertMonitor,
    send: Callable[[Message], Any],
    interval: float,
    stop: asyncio.Event,
) -> None:
    """Seed the monitor if needed and poll/digest on each interval until stop is set."""
    if not getattr(monitor, "seeded", False):
        monitor.seed()

    while not stop.is_set():
        try:
            outgoing: List[Message] = list(monitor.poll())
            digest_msg = monitor.digest()
            if digest_msg is not None:
                outgoing.append(digest_msg)

            for msg in outgoing:
                if inspect.iscoroutinefunction(send):
                    await send(msg)
                else:
                    await asyncio.to_thread(send, msg)
        except Exception:
            print("alert round failed", file=sys.stderr)

        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
            break
        except asyncio.TimeoutError:
            pass


def run_cli_test(
    environ: Optional[Dict[str, str]] = None,
    post_fn: Optional[Callable[[str, Message], bool]] = None,
) -> int:
    """Validate VIGIL_ALERT_WEBHOOK and send two test alerts (ping and silent)."""
    env_map = environ if environ is not None else os.environ
    raw_url = env_map.get("VIGIL_ALERT_WEBHOOK", "")
    if not raw_url or not raw_url.strip():
        print("VIGIL_ALERT_WEBHOOK: missing webhook configuration", file=sys.stderr)
        return 1

    try:
        validated_url = validate_webhook(raw_url.strip())
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    sender = post_fn if post_fn is not None else post_webhook
    test_messages = [
        Message("test", "Vigil test alert (this one should ping everyone)", True),
        Message("test", "Vigil test alert (this one should be silent)", False),
    ]
    sent_count = 0
    for msg in test_messages:
        if sender(validated_url, msg):
            sent_count += 1

    print(f"sent {sent_count} of {len(test_messages)}")
    return 0 if sent_count == len(test_messages) else 1


if __name__ == "__main__":
    if "--test" in sys.argv[1:]:
        sys.exit(run_cli_test())
    sys.exit(1)

