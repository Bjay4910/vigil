# Metric format

Version 1. Draft for review: Ordi can change anything here in a pull request.

## One data point

```json
{"name": "page_load_ms", "value": 842, "ts": "2026-10-06T14:03:11Z", "tags": {"client": "acme", "site": "acme.example", "env": "prod"}}
```

## Rules

| Field | Rule |
| --- | --- |
| `name` | Lowercase `snake_case` with the unit at the end, like `page_load_ms` |
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

## Detector output

One record per flagged point:

```json
{"name": "page_load_ms", "tags": {"client": "acme", "env": "prod"}, "ts": "2026-10-06T14:03:11Z", "value": 4200, "score": 5.2, "rule": "zscore"}
```

## Open questions

- Is second precision enough for timestamps?
- Do we need more tag keys?
- How much history should the detector read per query?
