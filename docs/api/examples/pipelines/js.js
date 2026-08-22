#!/usr/bin/env node
/**
 * Modulo API Example: Pipeline CRUD — fetch
 *
 * Demonstrates pipeline list, create, get, update, and delete.
 *
 * Usage:
 *   export MODULO_URL=http://localhost:8000
 *   export MODULO_EMAIL=admin@example.com
 *   export MODULO_PASSWORD=changeme
 *   node pipelines/js.js
 */

const { api, logSafe, auth, BASE_URL, EMAIL, PASSWORD, runMain } = require("../_shared/client.js");

function assertSafeId(id) {
  if (!/^[0-9a-fA-F-]{1,64}$/.test(id)) {
    throw new Error(`Invalid identifier: ${logSafe(id)}`);
  }
}

async function login() {
  const data = await api("/api/v1/auth/login", {
    method: "POST",
    body: JSON.stringify({ email: EMAIL, password: PASSWORD }),
  });
  return data.access_token;
}

async function main() {
  const token = await login();
  const h = auth(token);

  // Step 1: List pipelines
  console.log("Listing pipelines ...");
  const list = await api("/api/v1/pipelines?page=1&page_size=20", { headers: h });
  console.log(`  Found ${logSafe(list.total)} pipeline(s)`);
  for (const p of list.items) {
    console.log(`    - ${logSafe(p.id)}: ${logSafe(p.name)}`);
  }

  // Step 2: Create a pipeline
  console.log("\nCreating pipeline ...");
  const created = await api("/api/v1/pipelines", {
    method: "POST",
    headers: h,
    body: JSON.stringify({
      name: "PR Review Pipeline",
      description: "Automated PR review for code quality",
      visibility: "org",
      max_concurrent_runs: 3,
    }),
  });
  const pipelineId = created.id;
  console.log(`  Created: ${logSafe(created.name)} (id=${logSafe(pipelineId)})`);

  // Step 3: Get pipeline detail
  console.log(`\nFetching pipeline ${logSafe(pipelineId)} ...`);
  const detail = await api(`/api/v1/pipelines/${pipelineId}`, { headers: h });
  console.log(`  Name:        ${logSafe(detail.name)}`);
  console.log(`  Description: ${logSafe(detail.description || "N/A")}`);
  console.log(`  Visibility:  ${logSafe(detail.visibility)}`);

  // Step 4: Update pipeline
  console.log("\nUpdating pipeline ...");
  const updated = await api(`/api/v1/pipelines/${pipelineId}`, {
    method: "PATCH",
    headers: h,
    body: JSON.stringify({
      description: "Updated: now handles code review + security scanning",
      max_concurrent_runs: 5,
    }),
  });
  console.log(`  New description: ${logSafe(updated.description)}`);

  // Step 5: Delete pipeline
  console.log(`\nDeleting pipeline ${logSafe(pipelineId)} ...`);
  assertSafeId(pipelineId);
  const base = new URL(BASE_URL);
  const deleteUrl = new URL(`/api/v1/pipelines/${pipelineId}`, base); // NOSONAR [jssecurity:S7044,jssecurity:S8476] -- id validated above, origin pinned below
  if (deleteUrl.origin !== base.origin) {
    throw new Error(`Invalid delete path: ${logSafe(pipelineId)}`);
  }
  const delRes = await fetch(deleteUrl, { // NOSONAR [jssecurity:S7044,jssecurity:S8476] -- url origin pinned above
    method: "DELETE",
    headers: { ...h, "Content-Type": "application/json" },
  });
  if (!delRes.ok) throw new Error(`delete failed: ${delRes.status}`);
  console.log("  Deleted successfully (204 No Content)");

  console.log("\nDone.");
}

runMain(main);
