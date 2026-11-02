import assert from "node:assert/strict";
import http, { type IncomingHttpHeaders } from "node:http";
import type { AddressInfo } from "node:net";
import { afterEach, describe, it } from "node:test";
import { Vigil, type Point } from "../src/index.js";

interface RecordedRequest {
  method: string | undefined;
  url: string | undefined;
  headers: IncomingHttpHeaders;
  body: { points?: Point[] } & Record<string, unknown>;
}

interface FakeServer {
  readonly url: string;
  readonly requests: RecordedRequest[];
  setResponse(status: number, body: unknown, delayMs?: number): void;
  stop(): Promise<void>;
  start(): Promise<void>;
}

async function createFakeServer(): Promise<FakeServer> {
  let server: http.Server | null = null;
  let port = 0;
  let statusCode = 200;
  let responseBody: unknown = { accepted: 1 };
  let responseDelayMs = 0;
  const requests: RecordedRequest[] = [];

  async function start(): Promise<void> {
    if (server !== null) {
      return;
    }
    const srv = http.createServer((req, res) => {
      const chunks: Buffer[] = [];
      req.on("data", (chunk: Buffer) => {
        chunks.push(chunk);
      });
      req.on("end", () => {
        const raw = Buffer.concat(chunks).toString("utf-8");
        const parsed =
          raw.length > 0
            ? (JSON.parse(raw) as RecordedRequest["body"])
            : ({} as RecordedRequest["body"]);

        requests.push({
          method: req.method,
          url: req.url,
          headers: req.headers,
          body: parsed,
        });

        const send = (): void => {
          res.writeHead(statusCode, { "Content-Type": "application/json" });
          res.end(JSON.stringify(responseBody));
        };

        if (responseDelayMs > 0) {
          setTimeout(send, responseDelayMs);
        } else {
          send();
        }
      });
    });

    await new Promise<void>((resolve, reject) => {
      srv.once("error", reject);
      srv.listen(port, "127.0.0.1", () => {
        srv.removeListener("error", reject);
        const addr = srv.address() as AddressInfo;
        port = addr.port;
        server = srv;
        resolve();
      });
    });
  }

  async function stop(): Promise<void> {
    if (server === null) {
      return;
    }
    const srv = server;
    server = null;
    srv.closeAllConnections();
    await new Promise<void>((resolve, reject) => {
      srv.close((err) => {
        if (err) {
          reject(err);
        } else {
          resolve();
        }
      });
    });
  }

  await start();

  return {
    get url(): string {
      return `http://127.0.0.1:${port}`;
    },
    requests,
    setResponse(status: number, body: unknown, delayMs = 0): void {
      statusCode = status;
      responseBody = body;
      responseDelayMs = delayMs;
    },
    stop,
    start,
  };
}

async function waitFor(
  predicate: () => boolean,
  timeoutMs = 2000,
  intervalMs = 10,
): Promise<void> {
  const deadline = Date.now() + timeoutMs;
  while (!predicate()) {
    if (Date.now() >= deadline) {
      throw new Error(`Condition not met within ${timeoutMs}ms`);
    }
    await new Promise<void>((resolve) => setTimeout(resolve, intervalMs));
  }
}

describe("Vigil SDK", () => {
  const servers: FakeServer[] = [];

  async function setupServer(): Promise<FakeServer> {
    const srv = await createFakeServer();
    servers.push(srv);
    return srv;
  }

  afterEach(async () => {
    while (servers.length > 0) {
      const srv = servers.pop();
      if (srv) {
        await srv.stop();
      }
    }
  });

  it("1. record then flush sends one POST to /ingest with points and Content-Type header", async () => {
    const srv = await setupServer();
    const vigil = new Vigil({ url: srv.url, flushIntervalMs: 0, apiKey: "" });

    const ok = vigil.record(
      "page_load_ms",
      842,
      { client: "acme", env: "prod" },
      "2026-01-02T03:04:05Z",
    );
    assert.equal(ok, true);
    assert.equal(vigil.stats().queued, 1);

    await vigil.flush();

    assert.equal(srv.requests.length, 1);
    const req = srv.requests[0];
    assert.equal(req.method, "POST");
    assert.equal(req.url, "/ingest");
    assert.equal(req.headers["content-type"], "application/json");
    assert.deepEqual(req.body, {
      points: [
        {
          name: "page_load_ms",
          value: 842,
          ts: "2026-01-02T03:04:05Z",
          tags: { client: "acme", env: "prod" },
        },
      ],
    });
    assert.deepEqual(vigil.stats(), {
      queued: 0,
      sent: 1,
      dropped: 0,
      failed: 0,
    });
  });

  it("2. booleans become 1 and 0", async () => {
    const srv = await setupServer();
    const vigil = new Vigil({ url: srv.url, flushIntervalMs: 0, apiKey: "" });

    assert.equal(
      vigil.record("uptime_up", true, { client: "acme" }, "2026-01-02T03:04:05Z"),
      true,
    );
    assert.equal(
      vigil.record("uptime_up", false, { client: "globex" }, "2026-01-02T03:04:06Z"),
      true,
    );

    await vigil.flush();

    assert.equal(srv.requests.length, 1);
    const points = srv.requests[0].body.points ?? [];
    assert.equal(points.length, 2);
    assert.strictEqual(points[0].value, 1);
    assert.strictEqual(points[1].value, 0);
  });

  it("3. defaultTags merge with per-call tags, and per-call tag wins on a clash", async () => {
    const srv = await setupServer();
    const vigil = new Vigil({
      url: srv.url,
      flushIntervalMs: 0,
      apiKey: "",
      defaultTags: { client: "acme", env: "staging", service: "web" },
    });

    vigil.record(
      "api_response_ms",
      125,
      { env: "prod", site: "acme.example" },
      "2026-01-02T03:04:05Z",
    );
    await vigil.flush();

    assert.equal(srv.requests.length, 1);
    const points = srv.requests[0].body.points ?? [];
    assert.deepEqual(points[0].tags, {
      client: "acme",
      env: "prod",
      service: "web",
      site: "acme.example",
    });
  });

  it("4. invalid name, NaN value, unknown tag key, and uppercase tag value return false, queue nothing, and call onError", async () => {
    const srv = await setupServer();
    const errors: string[] = [];
    const vigil = new Vigil({
      url: srv.url,
      flushIntervalMs: 0,
      apiKey: "",
      onError: (msg) => errors.push(msg),
    });

    assert.equal(vigil.record("InvalidName", 100, { client: "acme" }), false);
    assert.equal(vigil.record("page_load_ms", Number.NaN, { client: "acme" }), false);
    assert.equal(vigil.record("page_load_ms", 100, { region: "us" }), false);
    assert.equal(vigil.record("page_load_ms", 100, { client: "Acme" }), false);

    assert.equal(errors.length, 4);
    assert.match(errors[0], /^name:/);
    assert.match(errors[1], /^value:/);
    assert.match(errors[2], /^tags:/);
    assert.match(errors[3], /^tags:/);

    assert.equal(vigil.stats().queued, 0);
    await vigil.flush();
    assert.equal(srv.requests.length, 0);
  });

  it("5. 250 points with batchSize 100 produce 3 requests of 100, 100, 50", async () => {
    const srv = await setupServer();
    const vigil = new Vigil({
      url: srv.url,
      batchSize: 100,
      flushIntervalMs: 0,
      apiKey: "",
    });

    for (let i = 0; i < 250; i += 1) {
      assert.equal(
        vigil.record("page_load_ms", i, { client: "acme" }, "2026-01-02T03:04:05Z"),
        true,
      );
    }

    await vigil.flush();

    assert.equal(srv.requests.length, 3);
    assert.deepEqual(
      srv.requests.map((r) => r.body.points?.length),
      [100, 100, 50],
    );
    assert.equal(vigil.stats().sent, 250);
    assert.equal(vigil.stats().queued, 0);
  });

  it("6. reaching batchSize triggers an automatic flush without calling flush()", async () => {
    const srv = await setupServer();
    const vigil = new Vigil({
      url: srv.url,
      batchSize: 5,
      flushIntervalMs: 0,
      apiKey: "",
    });

    for (let i = 0; i < 4; i += 1) {
      vigil.record("page_load_ms", i, { client: "acme" }, "2026-01-02T03:04:05Z");
    }
    assert.equal(srv.requests.length, 0);

    vigil.record("page_load_ms", 4, { client: "acme" }, "2026-01-02T03:04:05Z");

    await waitFor(() => srv.requests.length === 1);
    assert.equal(srv.requests.length, 1);
    assert.equal(srv.requests[0].body.points?.length, 5);
  });

  it("7. network failure keeps points queued and calls onError; recovery sends them", async () => {
    const srv = await setupServer();
    const errors: string[] = [];
    const vigil = new Vigil({
      url: srv.url,
      flushIntervalMs: 0,
      apiKey: "",
      onError: (msg) => errors.push(msg),
    });

    await srv.stop();

    vigil.record("page_load_ms", 111, { client: "acme" }, "2026-01-02T03:04:05Z");
    vigil.record("page_load_ms", 222, { client: "globex" }, "2026-01-02T03:04:06Z");

    await assert.doesNotReject(() => vigil.flush());
    assert.equal(errors.length, 1);
    assert.equal(vigil.stats().queued, 2);
    assert.equal(vigil.stats().sent, 0);

    await srv.start();
    await vigil.flush();

    assert.equal(srv.requests.length, 1);
    assert.deepEqual(
      srv.requests[0].body.points?.map((p) => p.value),
      [111, 222],
    );
    assert.equal(vigil.stats().queued, 0);
    assert.equal(vigil.stats().sent, 2);
  });

  it('8. a 400 response with {"error":"name: bad"} drops the batch, calls onError, and increments failed', async () => {
    const srv = await setupServer();
    srv.setResponse(400, { error: "name: bad" });

    const errors: string[] = [];
    const vigil = new Vigil({
      url: srv.url,
      flushIntervalMs: 0,
      apiKey: "",
      onError: (msg) => errors.push(msg),
    });

    vigil.record("page_load_ms", 10, { client: "acme" }, "2026-01-02T03:04:05Z");
    vigil.record("page_load_ms", 20, { client: "acme" }, "2026-01-02T03:04:06Z");

    await vigil.flush();

    assert.deepEqual(errors, ["name: bad"]);
    assert.deepEqual(vigil.stats(), {
      queued: 0,
      sent: 0,
      dropped: 0,
      failed: 2,
    });
  });

  it("9. a 500 response puts the points back in the queue", async () => {
    const srv = await setupServer();
    srv.setResponse(500, { error: "internal error" });

    const errors: string[] = [];
    const vigil = new Vigil({
      url: srv.url,
      flushIntervalMs: 0,
      apiKey: "",
      onError: (msg) => errors.push(msg),
    });

    vigil.record("page_load_ms", 10, { client: "acme" }, "2026-01-02T03:04:05Z");
    vigil.record("page_load_ms", 20, { client: "acme" }, "2026-01-02T03:04:06Z");

    await vigil.flush();

    assert.equal(errors.length, 1);
    assert.deepEqual(vigil.stats(), {
      queued: 2,
      sent: 0,
      dropped: 0,
      failed: 0,
    });

    srv.setResponse(200, { accepted: 2 });
    await vigil.flush();

    assert.equal(srv.requests.length, 2);
    assert.deepEqual(
      srv.requests[1].body.points?.map((p) => p.value),
      [10, 20],
    );
    assert.equal(vigil.stats().queued, 0);
    assert.equal(vigil.stats().sent, 2);
  });

  it("10. maxQueue drops the oldest points when exceeded and updates stats().dropped", async () => {
    const srv = await setupServer();
    const errors: string[] = [];
    const vigil = new Vigil({
      url: srv.url,
      maxQueue: 3,
      batchSize: 100,
      flushIntervalMs: 0,
      apiKey: "",
      onError: (msg) => errors.push(msg),
    });

    for (const val of [1, 2, 3, 4, 5]) {
      vigil.record("page_load_ms", val, { client: "acme" }, "2026-01-02T03:04:05Z");
    }

    assert.equal(vigil.stats().queued, 3);
    assert.equal(vigil.stats().dropped, 2);
    assert.deepEqual(errors, [
      "queue full: dropped oldest point",
      "queue full: dropped oldest point",
    ]);

    await vigil.flush();
    assert.deepEqual(
      srv.requests[0].body.points?.map((p) => p.value),
      [3, 4, 5],
    );
  });

  it("11. apiKey sends X-API-Key header, and omits header when no key is set", async () => {
    const srv = await setupServer();
    const savedKey = process.env.VIGIL_API_KEY;
    delete process.env.VIGIL_API_KEY;

    try {
      const withKey = new Vigil({
        url: srv.url,
        apiKey: "test-secret-acme",
        flushIntervalMs: 0,
      });
      withKey.record("page_load_ms", 50, { client: "acme" }, "2026-01-02T03:04:05Z");
      await withKey.flush();

      const withoutKey = new Vigil({
        url: srv.url,
        flushIntervalMs: 0,
      });
      withoutKey.record("page_load_ms", 60, { client: "globex" }, "2026-01-02T03:04:06Z");
      await withoutKey.flush();

      assert.equal(srv.requests.length, 2);
      assert.equal(srv.requests[0].headers["x-api-key"], "test-secret-acme");
      assert.equal("x-api-key" in srv.requests[1].headers, false);
    } finally {
      if (savedKey !== undefined) {
        process.env.VIGIL_API_KEY = savedKey;
      }
    }
  });

  it("12. close() stops the timer, sends remaining points, and allows clean exit", async () => {
    const srv = await setupServer();
    const vigil = new Vigil({
      url: srv.url,
      flushIntervalMs: 60_000,
      batchSize: 100,
      apiKey: "",
    });

    vigil.record("page_load_ms", 300, { client: "acme" }, "2026-01-02T03:04:05Z");
    vigil.record("page_load_ms", 400, { client: "globex" }, "2026-01-02T03:04:06Z");

    await vigil.close();

    assert.equal(srv.requests.length, 1);
    assert.equal(srv.requests[0].body.points?.length, 2);
    assert.equal(vigil.stats().queued, 0);
    assert.equal(vigil.stats().sent, 2);
  });

  it("13. timestamps have YYYY-MM-DDTHH:MM:SSZ format without milliseconds, and Date objects format properly", async () => {
    const srv = await setupServer();
    const errors: string[] = [];
    const vigil = new Vigil({
      url: srv.url,
      flushIntervalMs: 0,
      apiKey: "",
      onError: (msg) => errors.push(msg),
    });

    vigil.record("page_load_ms", 100, { client: "acme" });
    vigil.record(
      "page_load_ms",
      200,
      { client: "acme" },
      new Date("2026-01-02T03:04:05.789Z"),
    );
    assert.equal(
      vigil.record(
        "page_load_ms",
        300,
        { client: "acme" },
        "2026-01-02T03:04:05.789Z",
      ),
      false,
    );
    assert.equal(errors.length, 1);
    assert.match(errors[0], /^ts:/);

    await vigil.flush();

    assert.equal(srv.requests.length, 1);
    const points = srv.requests[0].body.points ?? [];
    assert.equal(points.length, 2);
    assert.match(points[0].ts, /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/);
    assert.equal(points[0].ts.includes("."), false);
    assert.equal(points[1].ts, "2026-01-02T03:04:05Z");
  });

  it("14. flush on an empty queue makes no request", async () => {
    const srv = await setupServer();
    const vigil = new Vigil({ url: srv.url, flushIntervalMs: 0, apiKey: "" });

    await vigil.flush();
    assert.equal(srv.requests.length, 0);
  });

  it("15. two flush() calls at the same time do not send any point twice", async () => {
    const srv = await setupServer();
    srv.setResponse(200, { accepted: 3 }, 30);

    const vigil = new Vigil({ url: srv.url, flushIntervalMs: 0, apiKey: "" });
    vigil.record("page_load_ms", 1, { client: "acme" }, "2026-01-02T03:04:05Z");
    vigil.record("page_load_ms", 2, { client: "acme" }, "2026-01-02T03:04:06Z");
    vigil.record("page_load_ms", 3, { client: "acme" }, "2026-01-02T03:04:07Z");

    const p1 = vigil.flush();
    const p2 = vigil.flush();
    assert.strictEqual(p1, p2);

    await Promise.all([p1, p2]);

    assert.equal(srv.requests.length, 1);
    assert.deepEqual(
      srv.requests[0].body.points?.map((p) => p.value),
      [1, 2, 3],
    );
    assert.equal(vigil.stats().sent, 3);
    assert.equal(vigil.stats().queued, 0);
  });
});

