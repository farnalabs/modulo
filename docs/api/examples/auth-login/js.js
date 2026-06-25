#!/usr/bin/env node
/**
 * Modulo API Example: Authentication (Login) — fetch
 *
 * Demonstrates login, token refresh, and logout using fetch.
 *
 * Usage:
 *   export MODULO_URL=http://localhost:8000
 *   export MODULO_EMAIL=admin@example.com
 *   export MODULO_PASSWORD=changeme
 *   node auth-login/js.js
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
    headers: {
      "Content-Type": "application/json",
      ...options.headers,
    },
  });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`${res.status} ${res.statusText}: ${text}`);
  }
  return res.json();
}

async function main() {
  // Step 1: Login
  console.log(`Logging in as ${EMAIL} ...`);
  const login = await api("/api/v1/auth/login", {
    method: "POST",
    body: JSON.stringify({ email: EMAIL, password: PASSWORD }),
  });
  const { access_token, refresh_token, token_type } = login;
  console.log(`  access_token:  ${access_token.slice(0, 20)}...`);
  console.log(`  refresh_token: ${refresh_token.slice(0, 20)}...`);
  console.log(`  token_type:    ${token_type}`);

  const auth = (token) => ({ Authorization: `Bearer ${token}` });

  // Step 2: Get current user
  console.log("\nFetching current user ...");
  const me = await api("/api/v1/auth/me", { headers: auth(access_token) });
  console.log(`  ${me.display_name} <${me.email}>`);
  console.log(`  role:    ${me.org_role}`);
  console.log(`  user_id: ${me.id}`);

  // Step 3: Refresh tokens
  console.log("\nRefreshing token ...");
  const refreshResp = await api("/api/v1/auth/refresh", {
    method: "POST",
    body: JSON.stringify({ refresh_token }),
  });
  const newAccess = refreshResp.access_token;
  const newRefresh = refreshResp.refresh_token;
  console.log(`  new access_token:  ${newAccess.slice(0, 20)}...`);
  console.log(`  new refresh_token: ${newRefresh.slice(0, 20)}...`);

  // Step 4: Logout
  console.log("\nLogging out ...");
  const logout = await api("/api/v1/auth/logout", {
    method: "POST",
    headers: auth(newAccess),
    body: JSON.stringify({ refresh_token: newRefresh }),
  });
  console.log(`  ${logout.detail}`);

  console.log("\nDone.");
}

main().catch((err) => {
  console.error("Fatal:", err.message);
  process.exit(1);
});
