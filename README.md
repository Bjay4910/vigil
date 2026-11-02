# vigil
[![CI](https://github.com/Bjay4910/vigil/actions/workflows/ci.yml/badge.svg)](https://github.com/Bjay4910/vigil/actions/workflows/ci.yml)

Lightweight observability platform: a metrics SDK, a from-scratch time-series store, and anomaly detection. Built to monitor real client sites and automations.

**Status:** v1 is working end to end and is not yet deployed against real Orductive systems.

## What it does

Vigil provides a zero-dependency TypeScript SDK (`sdk/`) that validates, batches, and sends metric data points to a FastAPI time-series store (`store/`). The store keeps points sorted in memory per metric and tag set using `bisect`, persists them to an append-only JSON Lines file on disk, and protects `POST /ingest`, `GET /query`, and `GET /flags` with optional `X-API-Key` authentication. An anomaly detector (`detection/`) scores time series using rolling z-score and EWMA rules, and a built-in browser dashboard (`GET /dashboard`) plots series and highlights flagged anomalies.

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
| `detection/` | Anomaly detection (z-score and EWMA) and backtesting |
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
- Two anomaly rules: z-score and EWMA
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

## Contributing

See `CONTRIBUTING.md`. Never commit secrets. Copy `.env.example` to `.env` and fill in your own values.

## License

MIT
