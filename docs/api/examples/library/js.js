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

const { api, logSafe, auth, EMAIL, PASSWORD, runMain } = require("../_shared/client.js");

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
  console.log(`  Found ${logSafe(browse.total)} primitive(s)`);
  for (const p of browse.items) {
    console.log(`    - ${logSafe(p.id)}: ${logSafe(p.name)} (${logSafe(p.primitive_type || "N/A")})`);
  }

  const items = browse.items;

  if (items.length > 0) {
    const prim = items[0];

    // Step 2: Preview
    console.log(`\nPreviewing ${logSafe(prim.name)} ...`);
    const detail = await api(`/api/v1/libraries/${prim.id}`, { headers: h });
    console.log(`  Description: ${logSafe(detail.description || "N/A")}`);

    // Step 3: Copy-to-adapt
    console.log(`\nCopying to adapt ...`);
    const cloned = await api(`/api/v1/libraries/${prim.id}/adapt`, {
      method: "POST",
      headers: h,
      body: JSON.stringify({}),
    });
    console.log(`  Cloned ID: ${logSafe(cloned.id)}`);

    // Step 4: Rate
    console.log(`\nSubmitting rating ...`);
    const rating = await api(`/api/v1/libraries/${prim.id}/ratings`, {
      method: "POST",
      headers: h,
      body: JSON.stringify({ thumbs_up: true, comment: "Great primitive!" }),
    });
    console.log(`  Rating submitted: ${logSafe(rating.id || "OK")}`);
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
    console.log(`  Created: ${logSafe(created.id)}`);
  }

  console.log("\nDone.");
}

runMain(main);
