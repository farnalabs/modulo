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

const { api, logSafe, auth, EMAIL, PASSWORD, runMain } = require("../_shared/client.js");

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
  console.log(`  ${logSafe(pending.gates.length)} pending gate(s)`);

  if (!pending.gates.length) {
    console.log("\nNo pending gates. Trigger a pipeline with a human_review node first.");
    return;
  }

  for (const g of pending.gates) {
    const claimed = g.claimed_by || "available";
    console.log(`  Gate ${g.review_id} | Run ${g.run_id} | Claimed: ${claimed}`);
  }

  // Step 2: Pick first unclaimed gate
  const available = pending.gates.filter((g) => !g.claimed_by);
  if (!available.length) {
    console.log("\nAll gates already claimed.");
    return;
  }

  const gate = available[0];
  const { run_id, review_id } = gate;
  console.log(`\nUsing gate ${review_id} on run ${run_id}`);

  // Step 3: Claim the gate
  console.log(`\nClaiming gate ${review_id} ...`);
  const claim = await api(`/api/v1/runs/${run_id}/hitl/${review_id}/claim`, {
    method: "POST",
    headers: h,
    body: JSON.stringify({ expiry_minutes: 10 }),
  });
  console.log(`  Claimed! Token: ${logSafe(claim.claim_token.slice(0, 20))}...`);

  // Step 4: Approve the gate
  console.log(`\nApproving gate ${review_id} ...`);
  const approve = await api(`/api/v1/runs/${run_id}/hitl/${review_id}/approve`, {
    method: "POST",
    headers: h,
    body: JSON.stringify({
      claim_token: claim.claim_token,
      notes: "Approved via JS example",
    }),
  });
  console.log(`  Status: ${logSafe(approve.status)}`);

  console.log("\nDone.");
}

runMain(main);
