"""
Synthetic metric generator: normal traffic plus a few injected spikes

Run it against a local store:
    python3 -m detection.generate --url http://127.0.0.1:8000

The spikes it injects are saved to data/injected_spikes.json so the
detector can be scored against them later (the backtest).
"""

import argparse
import datetime
import json
import math
import os
import random
import urllib.request
from typing import Any, Dict, Iterator, List, Tuple

from store.engine import format_ts, parse_ts

# Each series we fake: (metric name, tags, normal level, noise size).
# fake client names only.

DEFAULT_SERIES: List[Tuple[str, Dict[str, str], float, float]] = [
    ("page_load_ms", {"client": "acme", "env": "prod"}, 850.0, 60.0),
    ("page_load_ms", {"client": "globex", "env": "prod"}, 1200.0, 90.0),
    ("api_response_ms", {"client": "acme", "env": "prod", "service": "api"}, 180.0, 15.0),
]

def generate_series(
        name: str,
        tags: Dict[str, str],
        start: str,
        n_points: int,
        interval_s: int = 60,
        baseline: float = 850.0,
        noise: float = 60.0,
        daily_amplitude: float  = 0.15,
        n_spikes: int = 3,
        spike_factor: float = 4.0,
        warmup: int = 60,
        seed: int = 42,
) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Build one series of points and return (points, spike timestamps)"""
    if n_points <= warmup + n_spikes:
        raise ValueError("n_points must be larger than warmup + n_spikes")

    rng = random.Random(seed)
    start_epoch = parse_ts(start, field="start")

    # choose where the spike go, never inside the warm-up window
    spike_idx = set(rng.sample(range(warmup, n_points), n_spikes))

    points: List[Dict[str, Any]] =[]
    spike_ts: List[str] = []
    for i in range(n_points):
        epoch = start_epoch + i * interval_s

        # Daily rhythm: one full sine wave every 24 hours.
        seconds_into_day = epoch % 86400
        daily = math.sin(2 * math.pi * seconds_into_day / 86400)
        expected = baseline * (1 + daily_amplitude * daily)

        value = rng.gauss(expected, noise)
        ts = format_ts(epoch)

        if i in spike_idx:
            value = expected * spike_factor
            spike_ts.append(ts)

        points.append({
            "name": name,
            "value": round(max(value, 0.0), 1),
            "ts": ts,
            "tags": dict(tags),
        })

    return points, spike_ts

def batches(points: List[Dict[str, Any]], size: int = 500) -> Iterator[List[Dict[str, Any]]]:
    """Yield the points in chunks of at most 'size'"""
    for i in range(0, len(points), size):
        yield points[i:i + size]


def send(points: List[Dict[str, Any]], url: str, batch_size: int = 500) -> int:
    """ Post points to {url}/ingest in batches. Return how many were accepted """
    accepted = 0
    for chunk in batches(points, batch_size):
        body = json.dumps({"points": chunk}).encode("utf-8")
        req = urllib.request.Request(
            url.rstrip("/") + "/ingest",
            data = body,
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        with urllib.request.urlopen(req) as resp:
            accepted += json.loads(resp.read())["accepted"]
    return accepted

def main() -> None:
    parser = argparse.ArgumentParser(description="Send synthetic metrics to vigil store.")
    parser.add_argument("--url", default=os.environ.get("VIGIL_STORE_URL", "http://127.0.0.1:8000"))
    parser.add_argument("--hours", type=int, default=24)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--labels", default="data/injected_spikes.json")
    parser.add_argument("--dry-run", action="store_true", help="Generate but do not send")
    args = parser.parse_args()

    now = datetime.datetime.now(datetime.timezone.utc).replace(second=0, microsecond=0)
    start = format_ts(int(now.timestamp()) - args.hours * 3600)
    n_points = args.hours * 60   # one point per minute

    all_points: List[Dict[str, Any]] = []
    labels: List[Dict[str, Any]] = []
    for offset, (name, tags, baseline, noise) in enumerate(DEFAULT_SERIES):
        points, spikes = generate_series(
            name, tags, start, n_points,
            baseline=baseline, noise=noise, seed=args.seed + offset,
        )
        all_points.extend(points)
        labels.extend({"name": name, "tags": tags, "ts": ts} for ts in spikes)

    os.makedirs(os.path.dirname(args.labels) or ".", exist_ok=True)
    with open(args.labels, "w", encoding="utf-8") as f:
        json.dump(labels, f, indent=2)

    if args.dry_run:
        print(f"Generated {len(all_points)} points, {len(labels)} spikes (not sent)")
        return

    accepted = send(all_points, args.url)
    print(f"Sent {accepted} points, injected {len(labels)} spikes -> {args.labels}")

if __name__ == "__main__":
    main()