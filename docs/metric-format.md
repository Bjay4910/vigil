# Metric format

Version 1.

## One data point

```json
{"name": "page_load_ms", "value": 842, "ts": "2026-10-06T14:03:11Z", "tags": {"client": "acme", "site": "acme.example", "env": "prod"}}
```

## Rules

| Field | Rule |
| --- | --- |
| `name` | Lowercase `snake_case`. Put the unit at the end when there is one, like `page_load_ms`; names such as `uptime_up` are fine too. |
| `value` | A number. Booleans are sent as 1 or 0 |
| `ts` | ISO 8601 in UTC with a trailing `Z`, to the second |
| `tags` | Lowercase string keys and values. Allowed keys: `client`, `site`, `env`, `service` |

Use fake client names (like `acme`) in tests, docs, and sample data.

## Starting metrics

| Metric | What it measures | Priority |
| --- | --- | --- |
| `page_load_ms` | Client site load time | First |
| `uptime_up` | Site reachable (1 or 0) | First |
| `api_response_ms` | Backend response time | First |
| `automation_success` | Automation run succeeded (1 or 0) | First |
| `automation_duration_ms` | Automation run time | Later |
| `api_error` | Backend error (1 or 0) | Later |
| `deploy_success` | Deploy succeeded (1 or 0) | Later |
| `deploy_duration_s` | Deploy time | Later |
| `lighthouse_score` | Site quality score, 0 to 100 | Later |

## API

**Send points:** `POST /ingest`

Request body:

```json
{"points": [{"name": "page_load_ms", "value": 842, "ts": "2026-10-06T14:03:11Z", "tags": {"client": "acme", "env": "prod"}}]}
```

Response: `{"accepted": 1}`. An invalid point returns status 400 with a message saying which field is wrong.

**Read points:** `GET /query?name=page_load_ms&from=2026-10-06T00:00:00Z&to=2026-10-07T00:00:00Z&tags=client:acme,env:prod`

Response: `{"points": [...]}` with points sorted by `ts`, oldest first. `tags` is optional.

## Other endpoints

- **`GET /flags`**: Parameters `name`, `from`, `to`, optional `tags`, `rule` (`zscore`, `ewma`, or `binary`, default `zscore`), and `threshold` (default `3.0`). Runs the detector on matching points and returns `{"rule": ..., "threshold": ..., "flags": [...]}` where each element of `flags` is a detector output record described below, sorted by `ts` (oldest first). The `binary` rule applies only to series whose values are all `0` or `1`, flags the first `0` of each outage with the number of consecutive zeros as its `score`, and ignores `threshold`. A single `0` counts as an outage (`score` `1.0`), and non-binary series produce no flags under `rule=binary`. The score counts only the zeros inside the queried time window, so the same outage can score differently depending on where the window starts, and an outage that is already running at the start of the window is flagged at the window's first point. The rule only sees recorded points: if nothing records a value while a site is down (for example because the monitor itself is down), there is nothing to flag.
- **`GET /health`**: Open endpoint. Returns `{"status": "ok", "points": <count>}`.
- **`GET /dashboard`**: Open endpoint. Returns the self-contained HTML dashboard page.

When `VIGIL_API_KEY` or `VIGIL_PROJECT_KEYS` is set on the store, `/ingest`, `/query`, and `/flags` require a valid `X-API-Key` header, or they return status `401` with `{"error": "unauthorized"}`. `VIGIL_PROJECT_KEYS` takes a comma-separated list of `site:key` pairs (where each site matches `^[a-z0-9][a-z0-9_-]*$` and each key is at least 16 characters), scoping each project key to a single `site` tag value. A project key may only write points whose `tags.site` matches its site (otherwise `/ingest` returns `403` with `{"error": "forbidden: this key can only write points tagged with its own site"}`) and may only query or flag its own site (automatically filtering to its site when `site` is omitted from `tags`, or returning `403` with `{"error": "forbidden: this key can only read its own site"}` if another site is requested). The admin key (`VIGIL_API_KEY`) remains unrestricted and can read and write all sites.

## Detector output

One record per flagged point:

```json
{"name": "page_load_ms", "tags": {"client": "acme", "env": "prod"}, "ts": "2026-10-06T14:03:11Z", "value": 4200, "score": 5.2, "rule": "zscore"}
```

