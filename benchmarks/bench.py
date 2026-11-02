"""Command-line benchmark for a running Vigil time-series store."""

import argparse
import datetime
import http.client
import json
import math
import os
import platform
import random
import sys
import time
import urllib.parse
from typing import Any, Dict, List, Optional, Tuple

BASE_EPOCH = 1767225600  # 2026-01-01T00:00:00Z

SERIES_SPECS: List[Tuple[str, Dict[str, str], float, float]] = [
    ("page_load_ms", {"client": "client01", "env": "prod"}, 850.0, 60.0),
    ("page_load_ms", {"client": "client02", "env": "prod"}, 850.0, 60.0),
    ("page_load_ms", {"client": "client03", "env": "prod"}, 850.0, 60.0),
    ("page_load_ms", {"client": "client04", "env": "prod"}, 850.0, 60.0),
    ("page_load_ms", {"client": "client05", "env": "prod"}, 850.0, 60.0),
    ("api_response_ms", {"client": "client01", "env": "prod"}, 180.0, 15.0),
    ("api_response_ms", {"client": "client02", "env": "prod"}, 180.0, 15.0),
    ("api_response_ms", {"client": "client03", "env": "prod"}, 180.0, 15.0),
    ("api_response_ms", {"client": "client04", "env": "prod"}, 180.0, 15.0),
    ("api_response_ms", {"client": "client05", "env": "prod"}, 180.0, 15.0),
]


def format_epoch_utc(epoch: int) -> str:
    """Format epoch seconds as YYYY-MM-DDTHH:MM:SSZ in UTC."""
    dt = datetime.datetime.fromtimestamp(int(epoch), tz=datetime.timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def percentile(values: List[float], p: float) -> float:
    """Compute the p-th percentile (0..100) using linear interpolation."""
    if not values:
        raise ValueError("values must not be empty")
    s = sorted(float(v) for v in values)
    if p <= 0:
        return s[0]
    if p >= 100:
        return s[-1]
    pos = (len(s) - 1) * (p / 100.0)
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return s[lo]
    frac = pos - lo
    return s[lo] + frac * (s[hi] - s[lo])


def summarize(latencies_ms: List[float]) -> Dict[str, float]:
    """Return median, p95, and max for a list of latency measurements in ms."""
    if not latencies_ms:
        raise ValueError("latencies_ms must not be empty")
    return {
        "median": percentile(latencies_ms, 50),
        "p95": percentile(latencies_ms, 95),
        "max": float(max(latencies_ms)),
    }


def make_points(n: int, seed: int = 42) -> List[Dict[str, Any]]:
    """Generate n deterministic synthetic points spread evenly across 10 series."""
    rng = random.Random(seed)
    num_series = len(SERIES_SPECS)
    points: List[Dict[str, Any]] = []

    for i in range(n):
        series_idx = i % num_series
        step = i // num_series
        name, tags, baseline, noise = SERIES_SPECS[series_idx]
        raw_val = rng.gauss(baseline, noise)
        val = round(max(1.0, raw_val), 2)
        ts = format_epoch_utc(BASE_EPOCH + step)
        points.append({
            "name": name,
            "value": val,
            "ts": ts,
            "tags": dict(tags),
        })

    return points


def batches(points: List[Dict[str, Any]], size: int = 500) -> List[List[Dict[str, Any]]]:
    """Split points into chunks of at most size."""
    if size <= 0:
        raise ValueError("size must be positive")
    return [points[i:i + size] for i in range(0, len(points), size)]


def _open_connection(url: str) -> Tuple[http.client.HTTPConnection, str]:
    parsed = urllib.parse.urlparse(url)
    scheme = parsed.scheme or "http"
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or (443 if scheme == "https" else 80)
    base_path = parsed.path.rstrip("/")
    if scheme == "https":
        conn: http.client.HTTPConnection = http.client.HTTPSConnection(host, port, timeout=30)
    else:
        conn = http.client.HTTPConnection(host, port, timeout=30)
    return conn, base_path


def _request_json(
    conn: http.client.HTTPConnection,
    method: str,
    path: str,
    headers: Dict[str, str],
    body: Optional[bytes] = None,
) -> Tuple[int, Any]:
    conn.request(method, path, body=body, headers=headers)
    resp = conn.getresponse()
    raw = resp.read()
    status = resp.status
    try:
        data = json.loads(raw.decode("utf-8")) if raw else {}
    except Exception:
        data = {"raw": raw.decode("utf-8", errors="replace")}
    return status, data


def _format_ingest_table(ingest_result: Dict[str, Any]) -> str:
    lines = [
        "| Metric | Value |",
        "| --- | --- |",
        f"| Total points | {ingest_result['points']} |",
        f"| Batch size | {ingest_result['batch_size']} |",
        f"| Batches | {ingest_result['batches']} |",
        f"| Total time (s) | {ingest_result['total_s']:.3f} |",
        f"| Throughput (points/s) | {ingest_result['points_per_s']:.1f} |",
        f"| Batch median (ms) | {ingest_result['median_ms']:.2f} |",
        f"| Batch p95 (ms) | {ingest_result['p95_ms']:.2f} |",
        f"| Batch max (ms) | {ingest_result['max_ms']:.2f} |",
    ]
    return "\n".join(lines)


def _format_query_table(rows: List[Dict[str, Any]]) -> str:
    lines = [
        "| Case | Count | Median (ms) | p95 (ms) | Max (ms) |",
        "| --- | --- | --- | --- | --- |",
    ]
    for r in rows:
        if r.get("error"):
            lines.append(f"| {r['label']} | ERROR ({r['error']}) | - | - | - |")
        else:
            lines.append(
                f"| {r['label']} | {r['count']} | {r['median_ms']:.2f} | {r['p95_ms']:.2f} | {r['max_ms']:.2f} |"
            )
    return "\n".join(lines)


def run_benchmark(args: argparse.Namespace) -> int:
    api_key = os.environ.get("VIGIL_API_KEY")
    base_headers: Dict[str, str] = {"Connection": "keep-alive"}
    if api_key:
        base_headers["X-API-Key"] = api_key

    conn, base_path = _open_connection(args.url)
    try:
        try:
            status, health = _request_json(conn, "GET", f"{base_path}/health", base_headers)
        except Exception as exc:
            print(f"Error: cannot reach store at {args.url}: {exc}", file=sys.stderr)
            return 1

        if status != 200 or not isinstance(health, dict) or "points" not in health:
            print(f"Error: unexpected /health response (status {status})", file=sys.stderr)
            return 1

        existing_points = int(health["points"])
        if existing_points > 0 and not args.force:
            print(
                f"Refusing to run: the store already has {existing_points} points. "
                "Use a fresh container, or pass --force.",
                file=sys.stderr,
            )
            return 1

        points = make_points(args.points, seed=args.seed)
        point_batches = batches(points, size=args.batch_size)

        ingest_headers = dict(base_headers)
        ingest_headers["Content-Type"] = "application/json"

        batch_latencies_ms: List[float] = []
        t_start_all = time.perf_counter()
        for batch in point_batches:
            payload = json.dumps({"points": batch}).encode("utf-8")
            t0 = time.perf_counter()
            status, resp_data = _request_json(
                conn, "POST", f"{base_path}/ingest", ingest_headers, body=payload
            )
            elapsed_ms = (time.perf_counter() - t0) * 1000.0
            if status != 200:
                print(f"Error: POST /ingest failed with status {status}: {resp_data}", file=sys.stderr)
                return 1
            batch_latencies_ms.append(elapsed_ms)
        total_s = time.perf_counter() - t_start_all

        status, post_health = _request_json(conn, "GET", f"{base_path}/health", base_headers)
        if status != 200 or post_health.get("points") != len(points):
            actual = post_health.get("points") if isinstance(post_health, dict) else "unknown"
            print(
                f"Error: /health point count mismatch: expected {len(points)}, got {actual}",
                file=sys.stderr,
            )
            return 1

        ingest_stats = summarize(batch_latencies_ms) if batch_latencies_ms else {
            "median": 0.0,
            "p95": 0.0,
            "max": 0.0,
        }
        ingest_result: Dict[str, Any] = {
            "points": len(points),
            "batch_size": args.batch_size,
            "batches": len(point_batches),
            "total_s": total_s,
            "points_per_s": (len(points) / total_s) if total_s > 0 else 0.0,
            "median_ms": ingest_stats["median"],
            "p95_ms": ingest_stats["p95"],
            "max_ms": ingest_stats["max"],
        }

        series_points_count = max(1, (len(points) + 9) // 10)
        series_start_epoch = BASE_EPOCH
        series_end_epoch = BASE_EPOCH + series_points_count - 1
        full_from = format_epoch_utc(series_start_epoch)
        full_to = format_epoch_utc(series_end_epoch + 1)
        last_15m_from = format_epoch_utc(max(series_start_epoch, series_end_epoch + 1 - 15 * 60))
        last_1h_from = format_epoch_utc(max(series_start_epoch, series_end_epoch + 1 - 60 * 60))
        one_series_tags = "client:client01,env:prod"

        query_cases: List[Tuple[str, Dict[str, str]]] = [
            (
                "One series, last 15m",
                {"name": "page_load_ms", "from": last_15m_from, "to": full_to, "tags": one_series_tags},
            ),
            (
                "One series, last 1h",
                {"name": "page_load_ms", "from": last_1h_from, "to": full_to, "tags": one_series_tags},
            ),
            (
                "One series, full range",
                {"name": "page_load_ms", "from": full_from, "to": full_to, "tags": one_series_tags},
            ),
            (
                "All series (page_load_ms), full range",
                {"name": "page_load_ms", "from": full_from, "to": full_to},
            ),
        ]

        query_rows: List[Dict[str, Any]] = []
        for label, params in query_cases:
            qs = urllib.parse.urlencode(params)
            path = f"{base_path}/query?{qs}"
            expected_count: Optional[int] = None
            latencies: List[float] = []

            for run_idx in range(args.warmup + args.runs):
                t0 = time.perf_counter()
                status, body = _request_json(conn, "GET", path, base_headers)
                elapsed_ms = (time.perf_counter() - t0) * 1000.0
                if status != 200:
                    print(f"Error: GET /query ({label}) failed with status {status}: {body}", file=sys.stderr)
                    return 1
                pts = body.get("points", [])
                count = len(pts)
                if expected_count is None:
                    expected_count = count
                elif count != expected_count:
                    print(
                        f"Error: GET /query ({label}) returned inconsistent point counts: "
                        f"{expected_count} vs {count}",
                        file=sys.stderr,
                    )
                    return 1
                if run_idx >= args.warmup:
                    latencies.append(elapsed_ms)

            stats = summarize(latencies)
            query_rows.append({
                "label": label,
                "count": expected_count or 0,
                "median_ms": stats["median"],
                "p95_ms": stats["p95"],
                "max_ms": stats["max"],
            })

        flags_params = {
            "name": "page_load_ms",
            "from": full_from,
            "to": full_to,
            "tags": one_series_tags,
            "rule": "zscore",
        }
        flags_path = f"{base_path}/flags?{urllib.parse.urlencode(flags_params)}"
        flags_latencies: List[float] = []
        flags_count: Optional[int] = None
        flags_error: Optional[str] = None

        for run_idx in range(args.warmup + args.runs):
            t0 = time.perf_counter()
            status, body = _request_json(conn, "GET", flags_path, base_headers)
            elapsed_ms = (time.perf_counter() - t0) * 1000.0
            if status != 200:
                flags_error = f"status {status}"
                print(f"Warning: GET /flags returned status {status}: {body}", file=sys.stderr)
                break
            fl = body.get("flags", [])
            flags_count = len(fl)
            if run_idx >= args.warmup:
                flags_latencies.append(elapsed_ms)

        if flags_error is not None or not flags_latencies:
            query_rows.append({
                "label": "GET /flags (zscore, one series, full range)",
                "error": flags_error or "no runs",
            })
        else:
            fstats = summarize(flags_latencies)
            query_rows.append({
                "label": "GET /flags (zscore, one series, full range)",
                "count": flags_count or 0,
                "median_ms": fstats["median"],
                "p95_ms": fstats["p95"],
                "max_ms": fstats["max"],
            })

        ingest_table = _format_ingest_table(ingest_result)
        query_table = _format_query_table(query_rows)

        print("\n=== Ingest Benchmark ===")
        print(ingest_table)
        print("\n=== Query & Flags Benchmark ===")
        print(query_table)

        if args.write_md:
            today = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
            cmd_str = (
                f"python -m benchmarks.bench --url {args.url} --points {args.points} "
                f"--batch-size {args.batch_size} --runs {args.runs} --warmup {args.warmup} "
                f"--seed {args.seed}"
            )
            if args.force:
                cmd_str += " --force"
            if args.write_md:
                cmd_str += f" --write-md {args.write_md}"

            md_lines = [
                "# Vigil Store Benchmark Report",
                "",
                f"- **Date (UTC):** {today}",
                f"- **Command:** `{cmd_str}`",
                f"- **Platform:** `{platform.platform()}` (`{platform.machine()}`)",
                f"- **Python:** `{platform.python_version()}`",
                "",
                "## Ingest Results",
                "",
                ingest_table,
                "",
                "## Query & Flags Results",
                "",
                query_table,
                "",
                "## How to read this",
                "",
                "These numbers are from synthetic data, one client, over localhost, on one machine; "
                "they are not a production promise.",
                "",
            ]
            parent_dir = os.path.dirname(args.write_md)
            if parent_dir:
                os.makedirs(parent_dir, exist_ok=True)
            with open(args.write_md, "w", encoding="utf-8") as f:
                f.write("\n".join(md_lines))

        return 0
    finally:
        conn.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Benchmark a running Vigil store.")
    parser.add_argument("--url", default="http://127.0.0.1:8001")
    parser.add_argument("--points", type=int, default=100000)
    parser.add_argument("--batch-size", type=int, default=500)
    parser.add_argument("--runs", type=int, default=20)
    parser.add_argument("--warmup", type=int, default=3)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--write-md", default=None)
    parser.add_argument("--force", action="store_true")
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    sys.exit(run_benchmark(args))


if __name__ == "__main__":
    main()

