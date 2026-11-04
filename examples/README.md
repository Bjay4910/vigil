# Vigil SDK Demo

`examples/sdk_demo.mjs` is a standalone Node.js 18+ script that exercises the compiled TypeScript SDK (`sdk/dist/index.js`) against a running Vigil store:

1. Initializes a `Vigil` client with `defaultTags: { env: "demo" }`.
2. Records 5 `page_load_ms` points (1 second apart) and 1 boolean `uptime_up` (`true` -> `1`) point for fake client `acme`.
3. Flushes the batch to `POST /ingest` and prints `stats()` before and after.
4. Queries `GET /query` for both `page_load_ms` and `uptime_up` and prints the returned points.
5. Demonstrates graceful error handling when sending with a wrong API key (`401`) and when sending to an unreachable port (`http://127.0.0.1:9`), showing that failed network points remain queued without crashing the process.

## How to run

1. Build the TypeScript SDK once:
   ```bash
   cd sdk
   npm ci
   npm run build
   cd ..
   ```
2. Point to a running store and set `VIGIL_API_KEY` if the store has authentication enabled:
   ```bash
   export VIGIL_STORE_URL="http://127.0.0.1:8000"
   export VIGIL_API_KEY="your-api-key"
   ```
3. Run the demo from the repository root:
   ```bash
   node examples/sdk_demo.mjs
   ```

## Expected output

```text
1. Creating Vigil client for store at http://127.0.0.1:8000 (API key configured: true)
2. Recorded 5 page_load_ms points (returned: [true,true,true,true,true]) and 1 boolean uptime_up point (returned: true)
3. Stats before flush: {"queued":6,"sent":0,"dropped":0,"failed":0}
   Stats after flush:  {"queued":0,"sent":6,"dropped":0,"failed":0}
4. GET /query (page_load_ms): status=200, points=5, values=[810,825,790,845,830]
5. GET /query (uptime_up): status=200, points=1, value=1
6. Failure demo 1 (wrong API key):
   SDK error: ingest failed with status 401
   Stats with wrong key: {"queued":1,"sent":0,"dropped":0,"failed":0} (script did not crash)
7. Failure demo 2 (unreachable store at http://127.0.0.1:9):
   SDK error: fetch failed
   stats().queued=1 (point stayed queued for retry; script did not crash)
8. Success: verified 5 page_load_ms points and 1 uptime_up point (value 1).
```

> **Note:** Running this script writes 6 demo points (or 7 if the store has no API key configured) into the target store. Point it at a throwaway store instance (or a fresh container) if you want steps 4 and 5 to see only the 6 points from a single run.

