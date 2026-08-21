// Shared client helpers for the Modulo API documentation examples.
//
// The examples under docs/api/examples/* are intentionally runnable,
// self-contained snippets. They previously duplicated this boilerplate
// (HTTP client, safe-logging helper, auth header builder, env setup), which
// tripped SonarCloud's copy-paste duplication gate. It now lives here once.

const BASE_URL = (process.env.MODULO_URL || "http://localhost:8000").replace(/\/+$/, "");
const EMAIL = process.env.MODULO_EMAIL;
const PASSWORD = process.env.MODULO_PASSWORD;

if (!EMAIL || !PASSWORD) {
  console.error("MODULO_EMAIL and MODULO_PASSWORD must be set");
  process.exit(1);
}

async function api(path, options = {}) {
  if (typeof path !== "string" || !path.startsWith("/") || /^[a-z][a-z0-9+.-]*:\/\//i.test(path)) {
    throw new Error(`Invalid API path: ${logSafe(path)}`);
  }
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

function logSafe(value, max = 200) {
  return String(value ?? "").replace(/[\r\n]+/g, " ").slice(0, max);
}

function auth(token) {
  return { Authorization: `Bearer ${token}` };
}

async function runMain(mainFn) {
  try {
    await mainFn();
  } catch (err) {
    console.error("Fatal:", err.message);
    process.exit(1);
  }
}

module.exports = { BASE_URL, EMAIL, PASSWORD, api, logSafe, auth, runMain };
