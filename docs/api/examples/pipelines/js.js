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

async function login() {
  const data = await api("/api/v1/auth/login", {
    method: "POST",
    body: JSON.stringify({ email: EMAIL, password: PASSWORD }),
  });
  return data.access_token;
}

function auth(token) {
  return { Authorization: `Bearer ${token}` };
}

async function main() {
  const token = await login();
  const h = auth(token);

  // Step 1: List pipelines
  console.log("Listing pipelines ...");
  const list = await api("/api/v1/pipelines?page=1&page_size=20", { headers: h });
  console.log(`  Found ${list.total} pipeline(s)`);
  for (const p of list.items) {
    console.log(`    - ${p.id}: ${p.name}`);
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
  console.log(`  Created: ${created.name} (id=${pipelineId})`);

  // Step 3: Get pipeline detail
  console.log(`\nFetching pipeline ${pipelineId} ...`);
  const detail = await api(`/api/v1/pipelines/${pipelineId}`, { headers: h });
  console.log(`  Name:        ${detail.name}`);
  console.log(`  Description: ${detail.description || "N/A"}`);
  console.log(`  Visibility:  ${detail.visibility}`);

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
  console.log(`  New description: ${updated.description}`);

  // Step 5: Delete pipeline
  console.log(`\nDeleting pipeline ${pipelineId} ...`);
  const delRes = await fetch(`${BASE_URL}/api/v1/pipelines/${pipelineId}`, {
    method: "DELETE",
    headers: { ...h, "Content-Type": "application/json" },
  });
  if (!delRes.ok) throw new Error(`delete failed: ${delRes.status}`);
  console.log("  Deleted successfully (204 No Content)");

  console.log("\nDone.");
}

main().catch((err) => {
  console.error("Fatal:", err.message);
  process.exit(1);
});
