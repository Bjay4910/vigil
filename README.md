# vigil

Lightweight observability platform: a metrics SDK, a from-scratch time-series store, and anomaly detection. Built to monitor real client sites and automations.

**Status:** v1 in development (target: Oct 15, 2026)

## Components

| Folder | What it does |
| --- | --- |
| `sdk/` | Small library that services import to send metrics |
| `store/` | Time-series store with an ingestion API and a query API |
| `detection/` | Anomaly detection (z-score and EWMA) and backtesting |
| `dashboard/` | Live dashboard for metrics and alerts |
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
- One anomaly rule (z-score or EWMA)
- One live dashboard
- Everything runs with `docker-compose up`
- Tests, CI, a store benchmark, and an anomaly backtest

## Quick start

Coming soon.

## Contributing

See `CONTRIBUTING.md`. Never commit secrets. Copy `.env.example` to `.env` and fill in your own values.

## License

MIT
