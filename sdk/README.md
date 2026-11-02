# vigil-sdk

Zero-dependency TypeScript client library for sending metrics to the Vigil time-series store (`POST /ingest`). Uses the global `fetch` built into Node.js 18+.

> **Server-side only:** Use `vigil-sdk` in server-side Node.js code or serverless functions only — **never in browser code**, because `VIGIL_API_KEY` must not be exposed publicly.

## Install from the repo folder

Build the SDK in `sdk/`, then install it from the local folder in your service:

```bash
cd sdk
npm install
npm run build

# In your application directory:
npm install /path/to/vigil/sdk
```

## Environment variables

| Variable | Description | Default |
| --- | --- | --- |
| `VIGIL_STORE_URL` | Base URL of the Vigil store | `http://localhost:8000` |
| `VIGIL_API_KEY` | API key sent in the `X-API-Key` header when set | *(unset)* |

## Basic usage

```ts
import { Vigil } from "vigil-sdk";

const vigil = new Vigil({
  defaultTags: { client: "acme", env: "prod", service: "api" },
  onError: (msg) => console.error("[vigil]", msg),
});

vigil.record("api_response_ms", 142);
vigil.record("uptime_up", true, { site: "acme.example" });

// On shutdown, stop the background timer and flush remaining points:
await vigil.close();
```

## Netlify Function example (serverless)

**Important:** Serverless functions freeze or terminate immediately once they return a response, so queued points in memory will be lost unless flushed first. Always call `await vigil.flush()` **before** returning the response, and never run this SDK in browser code since the API key must remain secret on the server.

```ts
import { Vigil } from "vigil-sdk";

const vigil = new Vigil({
  defaultTags: { client: "acme", site: "acme.example", env: "prod" },
  flushIntervalMs: 0,
  onError: (msg) => console.error("[vigil]", msg),
});

export default async (request: Request): Promise<Response> => {
  try {
    const formData = await request.json();
    // Handle contact form submission (e.g. send email or store message)...
    void formData;

    vigil.record("contact_form_success", true);
    await vigil.flush();

    return new Response(JSON.stringify({ ok: true }), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  } catch {
    vigil.record("contact_form_success", false);
    await vigil.flush();

    return new Response(JSON.stringify({ ok: false }), {
      status: 500,
      headers: { "Content-Type": "application/json" },
    });
  }
};
```

