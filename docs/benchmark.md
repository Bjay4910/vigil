# Vigil Store Benchmark Report

- **Date (UTC):** 2026-11-02
- **Command:** `python -m benchmarks.bench --url http://127.0.0.1:8001 --points 100000 --batch-size 500 --runs 20 --warmup 3 --seed 42 --write-md docs/benchmark.md`
- **Platform:** `macOS-26.5.1-arm64-arm-64bit` (`arm64`)
- **Python:** `3.9.6`

## Ingest Results

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

## Query & Flags Results

| Case | Count | Median (ms) | p95 (ms) | Max (ms) |
| --- | --- | --- | --- | --- |
| One series, last 15m | 900 | 4.76 | 6.41 | 10.89 |
| One series, last 1h | 3600 | 15.50 | 29.77 | 35.22 |
| One series, full range | 10000 | 51.48 | 72.30 | 72.54 |
| All series (page_load_ms), full range | 50000 | 302.87 | 324.93 | 329.72 |
| GET /flags (zscore, one series, full range) | 29 | 39.67 | 52.72 | 54.41 |

## How to read this

These numbers are from synthetic data, one client, over localhost, on one machine; they are not a production promise.
