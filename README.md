# vigil
[![CI](https://github.com/Bjay4910/vigil/actions/workflows/ci.yml/badge.svg)](https://github.com/Bjay4910/vigil/actions/workflows/ci.yml)

Lightweight observability platform: a metrics SDK, a from-scratch time-series store, and anomaly detection. Built to monitor real client sites and automations.

**Status:** In production. Vigil runs on Fly.io as Orductive's observability backend. Each project reports with its own API key; the first production client site is live and reports form-submission outcomes (verified in production on 2026-11-06). Uptime checks for sites without server-side code, and alerting, are not built yet.

## What it does

Vigil provides a zero-dependency TypeScript SDK (`sdk/`) that validates, batches, and sends metric data points to a FastAPI time-series store (`store/`). The store keeps points sorted in memory per metric and tag set using `bisect`, persists them to an append-only JSON Lines file on disk, and protects `POST /ingest`, `GET /query`, and `GET /flags` with optional `X-API-Key` authentication. An anomaly detector (`detection/`) scores time series using z-score, EWMA, and a binary up/down rule, and a built-in browser dashboard (`GET /dashboard`) plots series and highlights flagged anomalies.

## Results

Measured on synthetic data. The store ran in a Docker container on an Apple M4 Pro MacBook, and the benchmark client ran on the same machine (macOS, Python 3.9.6):

- **Ingest throughput (`docs/benchmark.md`):** `60,347.5` points/sec (`100,000` synthetic points in `1.657` s across `200` batches of `500`; batch latency median `6.56` ms, p95 `8.21` ms, max `20.78` ms) (a single run).
- **Query latency for one 10,000-point series (`docs/benchmark.md`):** median `51.48` ms, p95 `72.30` ms, max `72.54` ms.
- **Backtest precision and recall at threshold `4.0` (`docs/backtest.md`):** `zscore` achieved `92%` precision and `100%` recall (`45` caught, `4` false alarms); `ewma` achieved `94%` precision and `100%` recall (`45` caught, `3` false alarms) across `5 seeds x 3 series x 1440` synthetic points.

See [docs/architecture.md](docs/architecture.md) for the full architecture and design documentation.

## Components

| Folder | What it does |
| --- | --- |
| `sdk/` | Small library that services import to send metrics |
| `store/` | Time-series store with an ingestion API and a query API |
| `detection/` | Anomaly detection (z-score, EWMA, and a binary up/down rule) and backtesting |
| `store/static/` | Dashboard page, served at `/dashboard` |
| `docs/` | Metric format, architecture, benchmark and backtest results |
| `tests/` | Test suite |

## How it fits together

Services use the SDK to send points to the store (`POST /ingest`). The dashboard and the detector read from the store (`GET /query`). The detector flags unusual values, and the dashboard shows them.

## Metric format

```json
{"name": "page_load_ms", "value": 842, "ts": "2026-10-06T14:03:11Z", "tags": {"client": "acme", "site": "acme.example", "env": "prod"}}
```

The full spec lives in `docs/metric-format.md`.

## v1 scope

- Metrics SDK
- Time-series store built from scratch
- Three detection rules: z-score, EWMA, and a binary rule for up/down metrics
- One live dashboard
- Everything runs with `docker compose up --build`
- Tests, CI, a store benchmark, and an anomaly backtest

## Quick start

1. **Run with Docker:** Copy `.env.example` to `.env`, set `VIGIL_API_KEY` to a long random string, run `docker compose up --build`, then open http://127.0.0.1:8000/dashboard (enter the key in the key box).
2. **Run without Docker:**
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements-dev.txt
   python -m uvicorn store.api:create_app --factory --port 8000
   ```
3. **Run the tests:**
   ```bash
   python -m pytest -q
   ```
4. **Try it with fake data:**
   ```bash
   python -m detection.generate --url http://127.0.0.1:8000
   ```
5. **Send metrics from your own code:** Use the TypeScript SDK in `sdk/` (see `sdk/README.md`).

## Deployment

- Runs in production on Fly.io configured by `fly.toml` and built from the repository `Dockerfile`.
- Deployed on a single 1 GB shared-CPU machine (`min_machines_running = 1`, `auto_stop_machines = false`) with a persistent volume mounted at `/data` and HTTPS enforced (`force_https = true`).
- Secrets (`VIGIL_API_KEY` for the unrestricted admin key and `VIGIL_PROJECT_KEYS` as comma-separated `site:key` pairs) are set with `fly secrets set` and are never stored in the repository.
- Run as a single machine by design because the store keeps its data in memory and on a single volume.
- A new project gets its own API key and a `site` tag added as a `site:key` entry in `VIGIL_PROJECT_KEYS`.
- The SDK is used from server-side code only, and in a serverless function callers must `await flush()` before returning.

## Contributing

See `CONTRIBUTING.md`. Never commit secrets. Copy `.env.example` to `.env` and fill in your own values.

## License

MIT
