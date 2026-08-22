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

const { api, logSafe, EMAIL, PASSWORD, runMain } = require("../_shared/client.js");

async function main() {
  // Step 1: Login
  console.log(`Logging in as ${EMAIL} ...`);
  const login = await api("/api/v1/auth/login", {
    method: "POST",
    body: JSON.stringify({ email: EMAIL, password: PASSWORD }),
  });
  const { access_token, refresh_token, token_type } = login;
  console.log(`  access_token:  ${logSafe(access_token.slice(0, 20))}...`);
  console.log(`  refresh_token: ${logSafe(refresh_token.slice(0, 20))}...`);
  console.log(`  token_type:    ${logSafe(token_type)}`);

  const auth = (token) => ({ Authorization: `Bearer ${token}` });

  // Step 2: Get current user
  console.log("\nFetching current user ...");
  const me = await api("/api/v1/auth/me", { headers: auth(access_token) });
  console.log(`  ${logSafe(me.display_name)} <${logSafe(me.email)}>`);
  console.log(`  role:    ${logSafe(me.org_role)}`);
  console.log(`  user_id: ${logSafe(me.id)}`);

  // Step 3: Refresh tokens
  console.log("\nRefreshing token ...");
  const refreshResp = await api("/api/v1/auth/refresh", {
    method: "POST",
    body: JSON.stringify({ refresh_token }),
  });
  const newAccess = refreshResp.access_token;
  const newRefresh = refreshResp.refresh_token;
  console.log(`  new access_token:  ${logSafe(newAccess.slice(0, 20))}...`);
  console.log(`  new refresh_token: ${logSafe(newRefresh.slice(0, 20))}...`);

  // Step 4: Logout
  console.log("\nLogging out ...");
  const logout = await api("/api/v1/auth/logout", {
    method: "POST",
    headers: auth(newAccess),
    body: JSON.stringify({ refresh_token: newRefresh }),
  });
  console.log(`  ${logSafe(logout.detail)}`);

  console.log("\nDone.");
}

runMain(main);
