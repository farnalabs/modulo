#!/usr/bin/env node
/**
 * Modulo API Example: Run Lifecycle — fetch
 *
 * Demonstrates triggering a run, polling status, getting IO, and cancelling.
 *
 * Usage:
 *   export MODULO_URL=http://localhost:8000
 *   export MODULO_EMAIL=admin@example.com
 *   export MODULO_PASSWORD=changeme
 *   node runs/js.js
 */

const BASE_URL = (process.env.MODULO_URL || "http://localhost:8000").replace(/\/+$/, "");
const EMAIL = process.env.MODULO_EMAIL;
const PASSWORD = process.env.MODULO_PASSWORD;

if (!EMAIL || !PASSWORD) {
  console.error("MODULO_EMAIL and MODULO_PASSWORD must be set");
  process.exit(1);
}

async function api(path, options = {}) {
  const url = `${BASE_URL}${path}`;
  const res = await fetch(url, {
    ...options,
    headers: { "Content-Type": "application/json", ...options.headers },
  });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`${res.status} ${res.statusText}: ${text}`);
  }
  return res.json();
}

function auth(token) {
  return { Authorization: `Bearer ${token}` };
}

async function main() {
  // Login
  const loginResp = await api("/api/v1/auth/login", {
    method: "POST",
    body: JSON.stringify({ email: EMAIL, password: PASSWORD }),
  });
  const token = loginResp.access_token;
  const h = auth(token);

  // Find a pipeline
  const list = await api("/api/v1/pipelines?page=1&page_size=5", { headers: h });
  if (!list.items.length) {
    console.error("No pipelines found. Create one first (see pipelines/ example).");
    process.exit(1);
  }
  const pipelineId = list.items[0].id;
  console.log(`Using pipeline: ${list.items[0].name} (${pipelineId})`);

  // Step 1: Trigger a run
  console.log("\nTriggering run ...");
  const run = await api("/api/v1/runs", {
    method: "POST",
    headers: h,
    body: JSON.stringify({
      pipeline_id: pipelineId,
      input_payload: { pr_url: "https://github.com/example/org/pull/42" },
    }),
  });
  const runId = run.run_id;
  console.log(`  Run ID: ${runId}, Status: ${run.status}`);

  // Step 2: Poll run status
  console.log("\nPolling run status ...");
  const terminalStates = new Set(["completed", "failed", "cancelled"]);
  let finalStatus;
  for (let i = 0; i < 15; i++) {
    await new Promise((r) => setTimeout(r, 2000));
    const detail = await api(`/api/v1/runs/${runId}`, { headers: h });
    finalStatus = detail.status;
    console.log(`  [${i + 1}] status = ${finalStatus}`);
    if (terminalStates.has(finalStatus)) break;
  }

  if (!terminalStates.has(finalStatus)) {
    console.log("\n  Run did not finish — cancelling ...");
    await api(`/api/v1/runs/${runId}/cancel`, { method: "POST", headers: h });
    console.log("  Cancel accepted");
  }

  // Step 3: Get run IO
  console.log(`\nFetching run IO ...`);
  try {
    const io = await api(`/api/v1/runs/${runId}/io`, { headers: h });
    console.log(`  Status:  ${io.status}`);
    console.log(`  Outputs: ${JSON.stringify(io.outputs_json)}`);
  } catch {
    console.log("  IO not available");
  }

  // Step 4: Get WS token
  console.log("\nRequesting WebSocket token ...");
  try {
    const ws = await api("/api/v1/auth/ws-token", {
      method: "POST",
      headers: h,
      body: "{}",
    });
    console.log(`  WS token: ${ws.ws_token.slice(0, 20)}...`);
    console.log(`  Connect:  ws://${BASE_URL.replace(/^https?:\/\//, "")}/api/v1/runs/${runId}/ws?token=${ws.ws_token}`);
  } catch {
    console.log("  WS token not available");
  }

  console.log("\nDone.");
}

main().catch((err) => {
  console.error("Fatal:", err.message);
  process.exit(1);
});
