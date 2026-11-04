/**
 * Vigil SDK end-to-end demo script (Node.js 18+).
 *
 * 1. Build the SDK first:
 *      cd sdk && npm ci && npm run build
 * 2. Set VIGIL_API_KEY in your environment if the store requires a key:
 *      export VIGIL_STORE_URL="http://127.0.0.1:8000"
 *      export VIGIL_API_KEY="your-key-here"
 * 3. Run from the repo root:
 *      node examples/sdk_demo.mjs
 */

import { Vigil } from "../sdk/dist/index.js";

const storeUrl = (process.env.VIGIL_STORE_URL || "http://127.0.0.1:8000").replace(/\/+$/, "");
const apiKey = process.env.VIGIL_API_KEY && process.env.VIGIL_API_KEY.length > 0
  ? process.env.VIGIL_API_KEY
  : undefined;

function toUtcIsoSecond(date) {
  return date.toISOString().slice(0, 19) + "Z";
}

async function main() {
  // 1. Create a Vigil client
  console.log(`1. Creating Vigil client for store at ${storeUrl} (API key configured: ${Boolean(apiKey)})`);
  const vigil = new Vigil({
    url: storeUrl,
    ...(apiKey ? { apiKey } : {}),
    defaultTags: { env: "demo" },
    flushIntervalMs: 0,
    onError: (message) => {
      console.log(`SDK error: ${message}`);
    },
  });

  // 2. Record 5 page_load_ms points (1 second apart ending at now) and 1 boolean uptime_up point
  const nowMs = Date.now();
  const sampleValues = [810, 825, 790, 845, 830];
  const pageLoadResults = [];
  for (let i = 0; i < 5; i += 1) {
    const ptDate = new Date(nowMs - (4 - i) * 1000);
    const ok = vigil.record("page_load_ms", sampleValues[i], { client: "acme" }, ptDate);
    pageLoadResults.push(ok);
  }
  const uptimeOk = vigil.record("uptime_up", true, { client: "acme" }, new Date(nowMs));
  console.log(
    `2. Recorded 5 page_load_ms points (returned: ${JSON.stringify(pageLoadResults)}) ` +
      `and 1 boolean uptime_up point (returned: ${uptimeOk})`
  );

  // 3. Print stats() before and after flushing
  console.log("3. Stats before flush:", JSON.stringify(vigil.stats()));
  await vigil.flush();
  console.log("   Stats after flush: ", JSON.stringify(vigil.stats()));

  // Prepare query range: 5 minutes ago to 1 minute from now
  const fromTs = toUtcIsoSecond(new Date(nowMs - 5 * 60 * 1000));
  const toTs = toUtcIsoSecond(new Date(nowMs + 60 * 1000));
  const headers = apiKey ? { "X-API-Key": apiKey } : {};

  // 4. Query page_load_ms
  const pageLoadParams = new URLSearchParams({
    name: "page_load_ms",
    from: fromTs,
    to: toTs,
    tags: "client:acme",
  });
  const pageLoadRes = await fetch(`${storeUrl}/query?${pageLoadParams.toString()}`, { headers });
  const pageLoadBody = pageLoadRes.ok ? await pageLoadRes.json() : {};
  const pageLoadPoints = Array.isArray(pageLoadBody.points) ? pageLoadBody.points : [];
  console.log(
    `4. GET /query (page_load_ms): status=${pageLoadRes.status}, ` +
      `points=${pageLoadPoints.length}, values=${JSON.stringify(pageLoadPoints.map((p) => p.value))}`
  );

  // 5. Query uptime_up
  const uptimeParams = new URLSearchParams({
    name: "uptime_up",
    from: fromTs,
    to: toTs,
    tags: "client:acme",
  });
  const uptimeRes = await fetch(`${storeUrl}/query?${uptimeParams.toString()}`, { headers });
  const uptimeBody = uptimeRes.ok ? await uptimeRes.json() : {};
  const uptimePoints = Array.isArray(uptimeBody.points) ? uptimeBody.points : [];
  const uptimeValues = uptimePoints.map((p) => p.value);
  console.log(
    `5. GET /query (uptime_up): status=${uptimeRes.status}, ` +
      `points=${uptimePoints.length}, value=${uptimeValues.length > 0 ? uptimeValues[0] : "none"}`
  );

  // 6. Failure demo 1: wrong API key
  console.log("6. Failure demo 1 (wrong API key):");
  const wrongKeyClient = new Vigil({
    url: storeUrl,
    apiKey: "wrong-key",
    defaultTags: { env: "demo" },
    flushIntervalMs: 0,
    onError: (message) => {
      console.log(`   SDK error: ${message}`);
    },
  });
  wrongKeyClient.record("api_response_ms", 150, { client: "acme" }, new Date(nowMs));
  await wrongKeyClient.flush();
  const wrongKeyStats = wrongKeyClient.stats();
  console.log(`   Stats with wrong key: ${JSON.stringify(wrongKeyStats)} (script did not crash)`);
  if (wrongKeyStats.sent === 1) {
    console.log("   Note: store has no API key configured, so the wrong key was accepted.");
  }

  // 7. Failure demo 2: unreachable store port (http://127.0.0.1:9)
  console.log("7. Failure demo 2 (unreachable store at http://127.0.0.1:9):");
  const offlineClient = new Vigil({
    url: "http://127.0.0.1:9",
    timeoutMs: 1000,
    defaultTags: { env: "demo" },
    flushIntervalMs: 0,
    onError: (message) => {
      console.log(`   SDK error: ${message}`);
    },
  });
  offlineClient.record("page_load_ms", 500, { client: "acme" }, new Date(nowMs));
  await offlineClient.flush();
  const offlineStats = offlineClient.stats();
  console.log(
    `   stats().queued=${offlineStats.queued} (point stayed queued for retry; script did not crash)`
  );

  // 8. Verify results from steps 4 and 5
  const step4Ok = pageLoadRes.status === 200 && pageLoadPoints.length === 5;
  const step5Ok =
    uptimeRes.status === 200 &&
    uptimePoints.length === 1 &&
    uptimePoints[0].value === 1;

  if (step4Ok && step5Ok) {
    console.log("8. Success: verified 5 page_load_ms points and 1 uptime_up point (value 1).");
    process.exit(0);
  } else {
    console.error(
      `8. Verification failed: step 4 (status=${pageLoadRes.status}, count=${pageLoadPoints.length}, expected 5), ` +
        `step 5 (status=${uptimeRes.status}, count=${uptimePoints.length}, value=${uptimeValues[0]}, expected 1 point with value 1).`
    );
    process.exit(1);
  }
}

main().catch((err) => {
  console.error("Unexpected error in demo:", err instanceof Error ? err.message : String(err));
  process.exit(1);
});

