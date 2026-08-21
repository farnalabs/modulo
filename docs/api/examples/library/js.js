#!/usr/bin/env node
/**
 * Modulo API Example: Library Primitive Management — fetch
 *
 * Demonstrates browsing, searching, previewing, copying, and rating primitives.
 *
 * Usage:
 *   export MODULO_URL=http://localhost:8000
 *   export MODULO_EMAIL=admin@example.com
 *   export MODULO_PASSWORD=changeme
 *   node library/js.js
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
  const loginResp = await api("/api/v1/auth/login", {
    method: "POST",
    body: JSON.stringify({ email: EMAIL, password: PASSWORD }),
  });
  const token = loginResp.access_token;
  const h = auth(token);

  // Step 1: Browse library
  console.log("Browsing library ...");
  const browse = await api("/api/v1/libraries?page=1&page_size=20", { headers: h });
  console.log(`  Found ${browse.total} primitive(s)`);
  for (const p of browse.items) {
    console.log(`    - ${p.id}: ${p.name} (${p.primitive_type || "N/A"})`);
  }

  const items = browse.items;

  if (items.length > 0) {
    const prim = items[0];

    // Step 2: Preview
    console.log(`\nPreviewing ${prim.name} ...`);
    const detail = await api(`/api/v1/libraries/${prim.id}`, { headers: h });
    console.log(`  Description: ${detail.description || "N/A"}`);

    // Step 3: Copy-to-adapt
    console.log(`\nCopying to adapt ...`);
    const cloned = await api(`/api/v1/libraries/${prim.id}/adapt`, {
      method: "POST",
      headers: h,
      body: "{}",
    });
    console.log(`  Cloned ID: ${cloned.id}`);

    // Step 4: Rate
    console.log(`\nSubmitting rating ...`);
    const rating = await api(`/api/v1/libraries/${prim.id}/ratings`, {
      method: "POST",
      headers: h,
      body: JSON.stringify({ thumbs_up: true, comment: "Great primitive!" }),
    });
    console.log(`  Rating submitted: ${rating.id || "OK"}`);
  } else {
    // Create one
    console.log("\nNo primitives found. Creating one ...");
    const created = await api("/api/v1/libraries", {
      method: "POST",
      headers: h,
      body: JSON.stringify({
        name: "Code Review Agent",
        primitive_type: "agent",
        slug: "code-review-agent",
        description: "Reviews PR code changes",
        content_json: {
          prompt_template: "Review the following PR diff: {{diff}}",
        },
      }),
    });
    console.log(`  Created: ${created.id}`);
  }

  console.log("\nDone.");
}

main().catch((err) => {
  console.error("Fatal:", err.message);
  process.exit(1);
});
