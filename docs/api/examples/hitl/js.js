#!/usr/bin/env node
/**
 * Modulo API Example: Human-in-the-Loop (HITL) — fetch
 *
 * Demonstrates listing, claiming, and approving HITL gates.
 *
 * Usage:
 *   export MODULO_URL=http://localhost:8000
 *   export MODULO_EMAIL=admin@example.com
 *   export MODULO_PASSWORD=changeme
 *   node hitl/js.js
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

  // Step 1: List all pending gates org-wide
  console.log("Fetching pending HITL gates ...");
  const pending = await api("/api/v1/hitl/pending", { headers: h });
  console.log(`  ${pending.gates.length} pending gate(s)`);

  if (!pending.gates.length) {
    console.log("\nNo pending gates. Trigger a pipeline with a human_review node first.");
    return;
  }

  for (const g of pending.gates) {
    const claimed = g.claimed_by || "available";
    console.log(`  Gate ${g.gate_id} | Run ${g.run_id} | Claimed: ${claimed}`);
  }

  // Step 2: Pick first unclaimed gate
  const available = pending.gates.filter((g) => !g.claimed_by);
  if (!available.length) {
    console.log("\nAll gates already claimed.");
    return;
  }

  const gate = available[0];
  const { run_id, gate_id } = gate;
  console.log(`\nUsing gate ${gate_id} on run ${run_id}`);

  // Step 3: Claim the gate
  console.log(`\nClaiming gate ${gate_id} ...`);
  const claim = await api(`/api/v1/runs/${run_id}/hitl/${gate_id}/claim`, {
    method: "POST",
    headers: h,
    body: JSON.stringify({ expiry_minutes: 10 }),
  });
  console.log(`  Claimed! Token: ${claim.claim_token.slice(0, 20)}...`);

  // Step 4: Approve the gate
  console.log(`\nApproving gate ${gate_id} ...`);
  const approve = await api(`/api/v1/runs/${run_id}/hitl/${gate_id}/approve`, {
    method: "POST",
    headers: h,
    body: JSON.stringify({
      claim_token: claim.claim_token,
      notes: "Approved via JS example",
    }),
  });
  console.log(`  Status: ${approve.status}`);

  console.log("\nDone.");
}

main().catch((err) => {
  console.error("Fatal:", err.message);
  process.exit(1);
});
