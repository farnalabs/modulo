#!/usr/bin/env node
/**
 * Modulo API Example: Authentication (Login) - fetch
 *
 * Demonstrates login, token refresh, and logout using fetch.
 *
 * Authentication model (FAR-1197):
 *   - The access token is in the login JSON body as `access_token`.
 *   - The refresh token is set ONLY as the httpOnly `modulo_refresh` cookie;
 *     it never appears in a response body.
 *   - `/auth/refresh` and `/auth/logout` take no request body. They read the
 *     cookie and require a double-submit CSRF header: the JS-readable
 *     `XSRF-TOKEN` cookie echoed in `X-CSRF-Token`.
 *
 * Node's fetch does not store cookies, so this example passes a CookieJar
 * (from ../_shared/client.js) to `api()`. In a browser the cookie jar is the
 * browser itself: call `fetch(..., { credentials: "include" })` and read
 * `document.cookie` for XSRF-TOKEN.
 *
 * Usage:
 *   export MODULO_URL=http://localhost:8000
 *   export MODULO_EMAIL=admin@example.com
 *   export MODULO_PASSWORD=changeme
 *   node auth-login/js.js
 */

const { api, logSafe, EMAIL, PASSWORD, runMain, CookieJar } = require("../_shared/client.js");

function csrfHeaders(jar) {
  const token = jar.get("XSRF-TOKEN");
  if (!token) throw new Error("XSRF-TOKEN cookie missing: login did not return a CSRF token");
  return { "X-CSRF-Token": token };
}

async function main() {
  const jar = new CookieJar();
  const auth = (token) => ({ Authorization: `Bearer ${token}` });

  // Step 1: Login. The access token is in the body; auth and CSRF cookies are
  // set on the response and stored in the jar.
  console.log(`Logging in as ${EMAIL} ...`);
  const login = await api("/api/v1/auth/login", {
    method: "POST",
    jar,
    body: JSON.stringify({ email: EMAIL, password: PASSWORD }),
  });
  const { access_token, token_type } = login;
  console.log(`  access_token:  ${logSafe(access_token.slice(0, 20))}...`);
  console.log(`  token_type:    ${logSafe(token_type)}`);
  console.log(`  modulo_refresh cookie set: ${jar.get("modulo_refresh") !== undefined}`);
  console.log(`  must_change_password:      ${login.must_change_password === true}`);

  // Step 2: Get current user
  console.log("\nFetching current user ...");
  const me = await api("/api/v1/auth/me", { headers: auth(access_token) });
  console.log(`  ${logSafe(me.display_name)} <${logSafe(me.email)}>`);
  console.log(`  role:    ${logSafe(me.org_role)}`);
  console.log(`  user_id: ${logSafe(me.id)}`);

  // Step 3: Refresh tokens. No body: the server reads the modulo_refresh
  // cookie, and the double-submit header must match the XSRF-TOKEN cookie.
  console.log("\nRefreshing token ...");
  const refreshResp = await api("/api/v1/auth/refresh", {
    method: "POST",
    jar,
    headers: csrfHeaders(jar),
  });
  const newAccess = refreshResp.access_token;
  console.log(`  new access_token: ${logSafe(newAccess.slice(0, 20))}...`);
  console.log(`  rotated modulo_refresh cookie: ${jar.get("modulo_refresh") !== undefined}`);

  // Step 4: Logout. Uses the rotated XSRF-TOKEN cookie, not the pre-refresh one.
  console.log("\nLogging out ...");
  const logout = await api("/api/v1/auth/logout", {
    method: "POST",
    jar,
    headers: { ...auth(newAccess), ...csrfHeaders(jar) },
  });
  console.log(`  ${logSafe(logout.detail)}`);

  console.log("\nDone.");
}

runMain(main);
