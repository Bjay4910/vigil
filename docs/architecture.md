# Vigil Architecture

## 1. Overview

Vigil is a lightweight observability platform built from scratch for collecting time-series metrics, storing and querying them, and flagging anomalous values. Services instrument their server-side code with the zero-dependency TypeScript SDK (`sdk/`), which validates, batches, and sends metric points over HTTP to the FastAPI store API (`store/api.py`). The store engine (`store/engine.py`) keeps sorted time series in memory backed by an append-only JSON Lines file on disk, and exposes query and anomaly-flagging endpoints. An anomaly detection module (`detection/detector.py`) scores each series using z-score, EWMA, and a binary up/down rule, and a self-contained browser dashboard (`store/static/dashboard.html`) visualizes the series and flagged points.

*Source: `README.md`, `store/api.py`, `store/engine.py`, `detection/detector.py`, `store/static/dashboard.html`, `sdk/src/client.ts`.*

## 2. Data Flow

```text
+--------------------+
|  Instrumented App  |
| (server-side code) |
+---------+----------+
          |
          |  vigil.record(...) / send(...)
          v
+--------------------+
|     Vigil SDK /    |
|  Generator Script  |
+---------+----------+
          |
          |  POST /ingest  (JSON {"points": [...]}, optional X-API-Key)
          v
+---------------------------------------------------------------+
|                         Store API                             |
|                        (store/api.py)                         |
|                                                               |
|   +-------------------------------------------------------+   |
|   |                   TimeSeriesStore                     |   |
|   |                  (store/engine.py)                    |   |
|   |  - In-memory _Series (bisect-sorted timestamps/vals)  |   |
|   |  - Append-only disk log (data/points.jsonl)           |   |
|   +-------------------------------------------------------+   |
+---------------+-------------------------------+---------------+
                |                               |
   GET /query   |                               |   GET /flags
                v                               v
+-------------------------------+   +-------------------------------+
|  Detector library / Dashboard |   |      In-process Detector      |
|  (detection/detector.py,      |   |  (zscore_flags / ewma_flags / |
|   store/static/dashboard.html)|   |   binary_flags -> Dashboard)  |
+-------------------------------+   +-------------------------------+
```

*Source: `store/api.py`, `store/engine.py`, `detection/detector.py`, `detection/generate.py`, `sdk/src/client.ts`, `store/static/dashboard.html`.*

## 3. The Store

`TimeSeriesStore` (`store/engine.py`) keeps points in memory grouped into one `_Series` object per unique `(metric_name, sorted_tags_tuple)` key. Each `_Series` stores two parallel Python lists—`timestamps: List[int]` (UTC epoch seconds) and `values: List[float]`—maintained in sorted order using `bisect.bisect_right` on insertion, so out-of-order writes are placed in chronological position and duplicate timestamps are preserved in arrival order. An index dictionary `_by_metric` maps each metric name to all `_Series` objects for that metric.

Range queries (`store.query(name, start, end, tags)`) parse `start` and `end` into epoch seconds, filter `_by_metric[name]` to series whose tags are a superset of the requested `tags` filter (subset matching), and slice each matching series in `O(log N)` time using `bisect.bisect_left(series.timestamps, start_epoch)` and `bisect.bisect_left(series.timestamps, end_epoch)`. Because both bounds use `bisect_left`, queries are start-inclusive and end-exclusive (`[start, end)`). Points across matching series are collected and sorted by timestamp (oldest first).

When initialized with a file path (defaulting to `VIGIL_DATA_PATH` or `data/points.jsonl` in `create_app`), the store replays existing lines on startup via `_replay()`. Blank lines are ignored, and any truncated, non-JSON, or invalid lines increment `store.skipped_lines` and are skipped without aborting startup. During `store.write(points)`, every point in the incoming batch is validated first via `validate_point`:
- `name` must be a string matching `^[a-z][a-z0-9_]*$`.
- `value` must be an `int` or `float` (booleans, `NaN`, and `inf`/`-inf` are rejected).
- `ts` must be a valid UTC calendar timestamp matching `^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$`.
- `tags` (optional, defaults to `{}`) must be a `dict` whose keys are in `ALLOWED_TAG_KEYS` (`{"client", "site", "env", "service"}`) and whose values are non-empty lowercase strings.

Batch writes are all-or-nothing: `store.write` validates every point in the list before mutating memory or appending to disk. If even one point in a batch fails validation, `ValidationError` is raised and zero points from that batch are stored in memory or written to the JSONL file.

*Source: `store/engine.py`, `docs/metric-format.md`, `tests/test_engine.py`, `tests/test_api.py`.*

## 4. The API

`create_app(store=None, api_key=None, project_keys=None)` in `store/api.py` builds the FastAPI application. If `api_key` is `None`, it reads `os.environ.get("VIGIL_API_KEY")` (treating `""` as unset); if `project_keys` is `None`, it parses `os.environ.get("VIGIL_PROJECT_KEYS")` via `parse_project_keys(raw, admin_key=...)` as a comma-separated list of `site:key` pairs (`site` matching `^[a-z0-9][a-z0-9_-]*$`, `key` at least 16 characters, with no duplicate sites, duplicate keys, or overlap with the admin key). When either the admin key or at least one project key is configured, `authenticate(request)` compares the incoming `X-API-Key` header using `hmac.compare_digest` against the admin key (if set) and every project key without stopping early, returning `("admin", None)`, `("project", site_name)`, or `("unauthorized", None)`. On protected endpoints, this check runs before reading the request body or validating query parameters, returning `401` with `{"error": "unauthorized"}` when unauthorized; if neither admin nor project keys are configured, `authenticate` returns `("open", None)` and all endpoints are open. When authenticated with a project key for `caller_site`, `POST /ingest` checks that every dict point in `points` has `tags.site == caller_site` before calling `store.write` (returning `403` with `{"error": "forbidden: this key can only write points tagged with its own site"}` and writing nothing if any point does not match), and `GET /query` and `GET /flags` return `403` with `{"error": "forbidden: this key can only read its own site"}` if `tags` specifies a different `site`, or set `tags["site"] = caller_site` automatically when `site` is omitted.

| Endpoint | Auth | Parameters / Body | Success Response | Error Behavior |
| --- | --- | --- | --- | --- |
| `POST /ingest` | `X-API-Key` (if keys set) | JSON body `{"points": [...]}` | `200 {"accepted": N}` | `401 {"error": "unauthorized"}` before reading body; `400 {"error": "body: must be valid JSON"}` on malformed JSON; `400 {"error": "points: must be a list"}` if `points` is missing/not a list; `403 {"error": "forbidden: this key can only write points tagged with its own site"}` if a project key sends a point without matching `tags.site`; `400 {"error": "<field>: ..."}` if any point fails `validate_point`. |
| `GET /query` | `X-API-Key` (if keys set) | `name`, `from`, `to`, optional `tags` (`k:v,k:v`) | `200 {"points": [...]}` | `401` if unauthorized; `400` if `name`, `from`, or `to` is missing (`"<param>: required"`), if `tags` lacks `:` (`"tags: expected key:value pairs separated by commas"`), or if timestamps fail `parse_ts`; `403 {"error": "forbidden: this key can only read its own site"}` if a project key requests another `site`. |
| `GET /flags` | `X-API-Key` (if keys set) | `name`, `from`, `to`, optional `tags`, `rule` (`"zscore"`, `"ewma"`, or `"binary"`, default `"zscore"`), `threshold` (default `"3.0"`) | `200 {"rule": ..., "threshold": ..., "flags": [...]}` | `401` if unauthorized; `400` on missing `name`/`from`/`to`, bad `tags`, `rule` not in `("zscore", "ewma", "binary")` (`"rule: must be zscore, ewma, or binary"`), or `threshold` not a finite float `> 0` (`"threshold: must be a number greater than 0"`); `403 {"error": "forbidden: this key can only read its own site"}` if a project key requests another `site`. Returns `{"rule": ..., "threshold": ..., "flags": []}` without invoking the detector when `store.query` returns no points. |
| `GET /catalog` | `X-API-Key` (if keys set) | None | `200 {"sites": [{"site": ..., "metrics": [{"name": ..., "envs": [...], "services": [...], "last_ts": ...}]}]}` | `401 {"error": "unauthorized"}` if unauthorized; exposes only `site`, `env`, and `service` tags (never `client`), grouping untagged series under `"(untagged)"` for admin/open mode and restricting a project key to its own `site` only. |
| `GET /health` | Open | None | `200 {"status": "ok", "points": N}` | Always open for container healthchecks. |
| `GET /dashboard` | Open | None | `200` (`text/html`) | Reads `store/static/dashboard.html` on each request; data requests made from the page carry `X-API-Key` independently. |

*Source: `store/api.py`, `tests/test_api.py`, `tests/test_api_auth.py`, `tests/test_api_project_keys.py`, `tests/test_api_flags.py`, `tests/test_dashboard.py`.*

## 5. The SDK

The TypeScript SDK (`sdk/src/client.ts`, `sdk/src/validate.ts`) has zero runtime dependencies and uses Node 18+'s built-in global `fetch`.

- **`record(name, value, tags?, ts?): boolean`**: Validates inputs client-side with the same rules as the store (except boolean values `true`/`false` are converted to `1`/`0` by `normalizeValue`, and `ts` defaults to the current UTC second or formats a `Date` to `YYYY-MM-DDTHH:MM:SSZ`). `defaultTags` from `VigilOptions` are merged with per-call `tags`, with per-call tags winning on key collisions. `record` never throws: if validation fails, it calls `onError(message)`, queues nothing, and returns `false`. On the first valid point, if `flushIntervalMs > 0` (default `5000` ms), it starts an interval timer and calls `.unref()` on it so the timer never prevents a Node.js process from exiting. If `queue.length >= batchSize` (default `100`), it triggers an asynchronous `flush()`.
- **Queue limit (`maxQueue`, default `1000`)**: If pushing a point causes `queue.length > maxQueue`, the SDK drops the oldest points from the front of the queue (`queue.shift()`), increments `stats().dropped`, and calls `onError("queue full: dropped oldest point")`.
- **`flush(): Promise<void>`**: Never rejects. If a flush is already in flight, it returns the existing in-flight promise so concurrent `flush()` calls never overlap or send duplicate points. If the queue is empty, it makes no HTTP request. Otherwise it sends batches of at most `batchSize` points to `POST {url}/ingest` with `Content-Type: application/json`, optional `X-API-Key`, and an `AbortController` set to `timeoutMs` (default `5000` ms):
  - **2xx**: Increments `stats().sent` by the batch length and continues to the next queued batch.
  - **400**: Treats the batch as permanently invalid, drops it, increments `stats().failed`, calls `onError` with the `"error"` string from the JSON response body (if present), and continues to the next batch.
  - **500 / 5xx, network error, timeout, or any other status**: Re-inserts the batch at the front of the queue (`queue.unshift(...batch)`), trimming oldest points if `maxQueue` is exceeded, calls `onError`, and stops flushing so remaining points stay queued for the next flush.
- **`close(): Promise<void>`**: Stops the periodic timer and awaits `flush()`.
- **Serverless usage**: Serverless functions (such as Netlify Functions) freeze or terminate their execution environment as soon as they return a response, before background timers can fire. Callers in serverless handlers must `await vigil.flush()` before returning a response, and must only run the SDK on the server side so `VIGIL_API_KEY` is never exposed to browsers.

*Source: `sdk/src/client.ts`, `sdk/src/validate.ts`, `sdk/README.md`, `sdk/test/client.test.ts`.*

## 6. Detection

`detection/detector.py` converts queried points into a pandas `DataFrame` via `to_frame(points)`, parsing `ts` as UTC timestamps and serializing each point's `tags` with sorted keys (`json.dumps(t, sort_keys=True)`) into a `series` column so distinct tag combinations are scored independently.

Inside `_flag_series(df, make_window, threshold, rule)`, each `(name, series)` group is sorted by `ts`, a window object `past = make_window(values)` is constructed, and both the mean and standard deviation are shifted forward by one step (`mean = past.mean().shift(1)`, `std = past.std().shift(1)`). This ensures every point `values[i]` is compared strictly against points *before* `i` (`0 .. i-1`) and never against itself. A point is flagged when `abs(z) > threshold` and `std > 0`, where `z = (values - mean) / std`:
- **`zscore_flags(df, window=60, threshold=3.0)`**: Uses a simple rolling window `v.rolling(window, min_periods=window)` with default `window = 60` points and `threshold = 3.0`. The first 60 points of each series act as warmup and are never flagged.
- **`ewma_flags(df, alpha=0.05, warmup=60, threshold=3.0)`**: Uses an exponentially weighted window `v.ewm(alpha=alpha, min_periods=warmup)` with default smoothing factor `alpha = 0.05`, `warmup = 60` points, and `threshold = 3.0`.

Each returned flag is a dictionary `{"name": name, "tags": row["tags"], "ts": "YYYY-MM-DDTHH:MM:SSZ", "value": row["value"], "score": round(float(z[idx]), 2), "rule": rule}`.

For up/down metrics, `binary_flags(df)` groups by `["name", "series"]`, sorts each series by `ts`, and checks that every value in the series is `0.0` or `1.0` (skipping any series that contains `NaN` or any other value). For each binary series, it finds every run of consecutive `0` values and emits one flag at the first `0` of the run with `"value": 0.0`, `"score": float(run_length)`, and `"rule": "binary"`, returning the flags sorted by `ts` (oldest first); a single `0` counts as an outage (`score` `1.0`), recoveries to `1` are not flagged, and `threshold` is ignored when `GET /flags` is called with `rule=binary`. The score counts only zeros inside the queried window, and a gap with no points is not treated as an outage.

*Source: `detection/detector.py`, `tests/test_detector.py`, `tests/test_detector_binary.py`.*

## 7. Results

### Store Benchmark (`docs/benchmark.md`)

Measured with `python -m benchmarks.bench --url http://127.0.0.1:8001 --points 100000 --batch-size 500 --runs 20 --warmup 3 --seed 42 --write-md docs/benchmark.md`. The store ran in a Docker container built from the repo's `Dockerfile` (`python:3.9-slim`) on the same MacBook, with the benchmark client running on macOS; the platform (`macOS-26.5.1-arm64-arm-64bit`, `arm64`) and Python version (`3.9.6`) in `docs/benchmark.md` describe the client, not the container; the hardware is an Apple M4 Pro MacBook Pro (stated by the author, not recorded in the repo); the ingest figure comes from a single run. These numbers are from synthetic data, one client, over localhost, on one machine; they are not a production promise.

#### Ingest Results

| Metric | Value |
| --- | --- |
| Total points | 100000 |
| Batch size | 500 |
| Batches | 200 |
| Total time (s) | 1.657 |
| Throughput (points/s) | 60347.5 |
| Batch median (ms) | 6.56 |
| Batch p95 (ms) | 8.21 |
| Batch max (ms) | 20.78 |

#### Query & Flags Results

| Case | Count | Median (ms) | p95 (ms) | Max (ms) |
| --- | --- | --- | --- | --- |
| One series, last 15m | 900 | 4.76 | 6.41 | 10.89 |
| One series, last 1h | 3600 | 15.50 | 29.77 | 35.22 |
| One series, full range | 10000 | 51.48 | 72.30 | 72.54 |
| All series (page_load_ms), full range | 50000 | 302.87 | 324.93 | 329.72 |
| GET /flags (zscore, one series, full range) | 29 | 39.67 | 52.72 | 54.41 |

### Detector Backtest (`docs/backtest.md`)

Measured on `5 seeds x 3 series x 1440 points, 3 injected spikes per series. Generated with python3 -m detection.backtest.`

| Rule | Threshold | Caught | False alarms | Precision | Recall |
|---|---|---|---|---|---|
| zscore | 3.0 | 45 | 85 | 35% | 100% |
| zscore | 3.5 | 45 | 17 | 73% | 100% |
| zscore | 4.0 | 45 | 4 | 92% | 100% |
| zscore | 5.0 | 45 | 2 | 96% | 100% |
| ewma | 3.0 | 45 | 81 | 36% | 100% |
| ewma | 3.5 | 45 | 13 | 78% | 100% |
| ewma | 4.0 | 45 | 3 | 94% | 100% |
| ewma | 5.0 | 45 | 2 | 96% | 100% |

The backtest uses synthetic data (sine-wave daily baseline plus Gaussian noise and `4.0x` multiplicative spikes from `detection/generate.py`), so the precision and recall numbers describe that synthetic data and not real traffic. Furthermore, the candidate thresholds (`3.0`, `3.5`, `4.0`, `5.0`) were evaluated and chosen on the same synthetic data generator they were scored on.

*Source: `docs/benchmark.md`, `docs/backtest.md`, `benchmarks/bench.py`, `detection/backtest.py`, `detection/generate.py`.*

## 8. Deployment and CI

### Docker & Docker Compose

- **`Dockerfile`**: Builds from `python:3.9-slim`, installs `requirements.txt` (`fastapi`, `uvicorn`, `pandas`), copies only `store/` and `detection/`, creates a non-root user `vigil` and `/data` directory owned by `vigil` prior to declaring `VOLUME /data`, sets `ENV PYTHONUNBUFFERED=1` and `ENV VIGIL_DATA_PATH=/data/points.jsonl`, exposes port `8000`, configures a `HEALTHCHECK` every `30s` (`timeout=5s`, `start-period=10s`, `retries=3`) against `http://127.0.0.1:8000/health`, and runs `python -m uvicorn store.api:create_app --factory --host 0.0.0.0 --port 8000`.
- **`docker-compose.yml`**: Runs the `store` service bound to localhost only (`127.0.0.1:8000:8000`), passes `VIGIL_API_KEY: ${VIGIL_API_KEY:-}` and `VIGIL_PROJECT_KEYS: ${VIGIL_PROJECT_KEYS:-}` from `.env` or the shell environment (defaulting to empty/open if unset), mounts the named volume `vigil-data` at `/data`, and sets `restart: unless-stopped`.

### Fly.io (`fly.toml`)

- **Region and VM:** Deployed in `primary_region = 'iad'` on one shared-CPU machine (`cpu_kind = 'shared'`, `cpus = 1`, `memory = '1gb'`), built from the repository `Dockerfile`.
- **Single-machine topology by design:** Runs a single machine (`min_machines_running = 1`, `auto_stop_machines = false`, `auto_start_machines = true`) with a single persistent volume (`source = 'vigil_data'`) mounted at `destination = '/data'`. Running one machine is by design because `TimeSeriesStore` holds series data in memory backed by the JSONL log on that machine's volume; volume snapshots are scheduled with a retention of 5 (as Fly reported at volume creation).
- **Networking, health checks, and secrets:** Routes external traffic over HTTPS (`force_https = true`) to `internal_port = 8000` and checks `GET /health` over HTTP every `30s` (`timeout = '5s'`, `grace_period = '10s'`). Secrets (`VIGIL_API_KEY`, `VIGIL_PROJECT_KEYS`) are configured via `fly secrets set` and are never placed in `fly.toml`.

### Continuous Integration (`.github/workflows/ci.yml`)

Triggered on `pull_request` and `push` to `main` with `permissions: contents: read` and per-ref concurrency cancellation:
- **`python-tests`**: Runs on `ubuntu-24.04` with Python `3.9`, installs `requirements-dev.txt`, and executes `python -m pytest -q`.
- **`sdk-tests`**: Runs on `ubuntu-24.04` with Node.js `22` in `sdk/`, and executes `npm ci`, `npm test`, and `npm run build`.

The runners are pinned to `ubuntu-24.04` because `ubuntu-latest` moved to Ubuntu 26.04, which does not provide Python 3.9.

### Uptime checker (`store/checks.py`)

- **Lifespan background task:** Started by the FastAPI `lifespan` context manager in `create_app` only when `VIGIL_CHECKS` is non-empty (comma-separated `site=url` pairs, `https` public hostnames only, at most 20 entries), running every `VIGIL_CHECK_INTERVAL` seconds (default `300`, minimum `30`) with `env` set from `VIGIL_CHECK_ENV` (default `prod`).
- **Thread isolation, 15-second confirmation re-check, and hard timeouts:** Network `GET` requests run concurrently in worker threads via `asyncio.to_thread` wrapped in `asyncio.wait_for(..., timeout + 5.0)`, while all `store.write` calls happen back on the event loop thread. In production (`retry_delay=15.0`), any site that fails its initial check in a round waits 15 seconds and is re-checked once before its `uptime_up` and `response_ms` points are recorded, filtering out single-request blips.
- **Redirect and body bounds:** Redirects are followed (up to 5 hops) only to the same origin (`scheme`, `host`, `port`) or to an `https` public hostname (`_redirect_allowed` and `_host_is_public`); at most 65,536 bytes of the response body are read and discarded, so page content is never stored—only `uptime_up` (`1` or `0`) and `response_ms` (rounded integer milliseconds) tagged with `site`, `env`, and `service: "uptime"`.

### Alerting (`store/alerts.py`)

- **Watched series and startup seed:** Enabled when `VIGIL_ALERT_WEBHOOK` is set. `AlertMonitor` watches `uptime_up`, `form_submission_success`, and `contact_form_success` series whose `env` tag matches `VIGIL_ALERT_ENV` (default `prod`). On startup, `monitor.seed()` runs synchronously before the checker task starts, recording each existing series' newest timestamp in `last_seen` and initializing each site's `uptime_up` state (`"down"` or `"up"`) silently so a store restart never re-alerts on historical points or already-known outages.
- **Confirmation retry and state transitions:** Because the uptime checker re-checks failed sites after 15 seconds before writing `uptime_up = 0`, a `DOWN` alert fires only after two failed checks 15 seconds apart. `poll()` emits one `down` message (`ping=True`) when a site transitions to `0`, ignores repeated zeros while down, emits one silent `recovered` message (`ping=False`) with the outage duration in minutes when `uptime_up` returns to `1`, and emits at most one `form_failed` message (`ping=True`) per site per 10 minutes when a form metric records `0`.
- **Daily digest and rate limit:** `digest()` posts one silent (`ping=False`) 24-hour summary per local day at or after `VIGIL_DIGEST_HOUR` (default `8`) in `VIGIL_DIGEST_TZ` (default `America/New_York`). `RateLimitedSender` caps non-digest alerts at `VIGIL_ALERT_MAX_PER_HOUR` (default `20`) per rolling hour while never dropping `digest` messages.
- **Discord and Slack webhooks and message privacy:** `validate_webhook` accepts only `https` URLs on `discord.com`, `discordapp.com`, or `hooks.slack.com` with no credentials, query, fragment, or port, and never includes the URL in error messages. `post_webhook` sends pinging messages with `@everyone` (Discord) or `<!channel>` (Slack) and silent messages with Discord `flags: 4096` (`SUPPRESS_NOTIFICATIONS`), never follows redirects, and includes only the `site` tag, the kind of event (`service` tag for form failures), and the UTC timestamp.

*Source: `Dockerfile`, `.dockerignore`, `docker-compose.yml`, `fly.toml`, `.github/workflows/ci.yml`, `store/checks.py`, `store/alerts.py`, `store/api.py`, `tests/test_docker_files.py`, `tests/test_fly_toml.py`, `tests/test_fly_toml_checks.py`, `tests/test_fly_toml_alerts.py`, `tests/test_ci_file.py`, `tests/test_checks.py`, `tests/test_checks_retry.py`, `tests/test_api_checks.py`, `tests/test_alerts.py`, `tests/test_api_alerts.py`.*

## 9. Known Limitations

- **All points are kept in memory:** `TimeSeriesStore` holds every timestamp and value in Python lists in RAM (`_series_map` and `_by_metric`) with no retention policy, compaction, downsampling, or eviction, and the append-only JSONL file grows without bound (`store/engine.py`).
- **Single-process architecture with no replication:** The store runs as a single process backed by a local file. There is no replication, sharding, or multi-worker coordination; running multiple worker processes against the same JSONL file would result in diverging in-memory state (`store/engine.py`, `Dockerfile`).
- **Writes are flushed to the OS buffer but not `fsync`ed:** `store.write` calls `f.flush()` after writing a batch to the JSONL file, but does not call `os.fsync(f.fileno())`. An operating system crash or power failure can lose the most recent writes that have not yet been flushed to disk by the OS kernel (`store/engine.py`).
- **Zero-variance windows are not flagged by z-score or EWMA:** `_flag_series` requires `(z.abs() > threshold) & (std > 0)`, so `zscore_flags` and `ewma_flags` still skip flat series where `std == 0`. Consequently, a drop in a binary `0`/`1` metric (such as `uptime_up`) must use the `binary` rule (`binary_flags`), and non-binary flat series are still not flagged (`detection/detector.py`, `tests/test_detector.py`).
- **The binary rule only sees recorded points:** it flags runs of recorded zeros inside the queried window, so a gap with no points at all (for example a dead monitor) is not flagged, and an outage's score depends on where the query window starts (`detection/detector.py`).
- **Contamination of rolling statistics after a spike:** Neither `zscore_flags` nor `ewma_flags` masks or winsorizes previously flagged spikes when computing the rolling mean and standard deviation for subsequent points, so a large spike inflates `std` for the next `window` points and can mask follow-up anomalies (`detection/detector.py`).
- **Linear insertion cost for out-of-order writes:** While `bisect.bisect_right` finds the insertion index in `O(log N)` time, `list.insert(idx, ...)` on Python `list` objects shifts subsequent elements in `O(N)` time when points arrive out of chronological order (`store/engine.py`).
- **Second-level timestamp granularity and no deduplication:** Timestamps are stored as whole UTC seconds (`YYYY-MM-DDTHH:MM:SSZ`). Sub-second precision is truncated by the SDK and rejected if sent as milliseconds, and writing two points with the same series and second stores both points rather than overwriting or aggregating (`store/engine.py`, `sdk/src/validate.ts`).
- **Fixed tag schema:** Only four tag keys (`client`, `site`, `env`, `service`) with lowercase string values are accepted by `validate_point` and `validateTags` (`store/engine.py`, `sdk/src/validate.ts`).
- **Static environment-variable API keys, no read-only role, no rate limiting, and no TLS termination in-app:** Authentication supports an unrestricted admin key (`VIGIL_API_KEY`) plus per-project keys (`VIGIL_PROJECT_KEYS`) scoped by `site`, but there is still no read-only role, no rate limiting, and keys live in environment variables (`store/api.py`).
- **A project key can write fake points into its own site:** While a project key cannot read or write another site's points, any holder of a project key can still ingest arbitrary or fabricated metric points tagged with its own `site` (`store/api.py`).
- **No alerting is implemented:** Flags appear only in the dashboard and in `/flags` responses (`store/api.py`, `store/static/dashboard.html`).
- **No limit on response size:** `/query` and `/flags` return every matching point (the 50,000-point full-range query in the benchmark took about 303 ms) (`store/api.py`, `store/engine.py`, `docs/benchmark.md`).
- **Coverage:** one production client site reports so far, and only form-submission outcomes (1 for a saved submission, 0 for a failed save). There is no uptime checker, so a site that is down and silent produces no data and no flag, and there is no alerting.
- **Single machine and volume:** the volume is pinned to one physical host, so a host failure means downtime until Fly recovers it; there is no replication or failover.
- **Uptime checks come from one location:** the checks run from the store's own Fly region, so they show whether a site answers there, not what visitors elsewhere see; if the store is down, no checks run and nothing alerts.
- **Check data grows without retention:** each site adds two points per interval, about 1 million points a year for five sites at five-minute intervals, which is about half a gigabyte of memory by the measured upper bound (about 490 bytes per point) on a 1 GB machine; there is no retention yet.
- **Alerts come from the same process they monitor:** if the store is down, nothing is sent; the daily summary not arriving is the signal.
- **Alerting is best effort:** messages are posted once with no queue or retry, so a Discord outage can lose an alert; an outage that begins and ends between two checks is not seen; alert state is in memory, so a site that fails during a store restart may be reported late.

*Source: `store/engine.py`, `store/api.py`, `detection/detector.py`, `tests/test_detector.py`, `sdk/src/validate.ts`, `docs/benchmark.md`.*

## 10. Design Decisions

- **Parallel Python lists with `bisect` per series (`store/engine.py`):** Storing `timestamps: List[int]` and `values: List[float]` per `_Series` avoids per-point dictionary and string overhead in memory, keeps appends `O(1)` when timestamps arrive in order, and allows `bisect_left` range slicing in `O(log N)` without external database dependencies.
- **All-or-nothing batch validation (`store/engine.py`):** `store.write` validates every point in a batch before inserting any point into memory or appending to disk. This prevents partial batch writes and keeps the in-memory state and append-only file consistent without requiring rollback logic.
- **Never-throwing SDK with bounded queue (`sdk/src/client.ts`):** Observability code should never crash the host application. `record()` catches validation errors synchronously and reports them via `onError`, `flush()` catches network and HTTP errors and resolves cleanly, `.unref()` keeps the background timer from blocking process exit, and `maxQueue` bounds memory usage by dropping the oldest points if the store is unreachable.
- **Running `/flags` in-process (`store/api.py`):** `GET /flags` queries `store.query(...)` directly in memory and calls `detection.detector` functions in the same Python process (importing `detection.detector` lazily inside the handler). This avoids a round-trip HTTP call from the detector back to `/query` when serving the dashboard, while allowing the rest of the store API to start even in minimal environments without `pandas`.
- **Single self-contained HTML dashboard (`store/static/dashboard.html`):** Shipping the dashboard as one static HTML file with inline CSS, vanilla JavaScript, inline SVG rendering, `textContent`/`createElementNS` DOM construction (which avoids `innerHTML`, so metric names and tags from the API are never parsed as markup), and an in-memory-only API key variable eliminates frontend build steps, external CDN dependencies, and persistent secret leakage in browser storage.

*Source: `store/engine.py`, `store/api.py`, `sdk/src/client.ts`, `store/static/dashboard.html`.*

