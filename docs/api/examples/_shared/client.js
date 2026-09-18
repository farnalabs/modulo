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
  // Pin the resolved URL to the configured base origin so a (future) caller
  // cannot use `path` to traverse off-host (SSRF / forging). `path` is always a
  // hard-coded literal in the doc examples, but we enforce it defensively.
  const base = new URL(BASE_URL);
  const url = new URL(path, base); // NOSONAR [jssecurity:S7044,jssecurity:S8476] -- origin pinned below
  if (url.origin !== base.origin) {
    throw new Error(`API path escapes base origin: ${logSafe(path)}`);
  }
  const res = await fetch(url, { // NOSONAR [jssecurity:S7044,jssecurity:S8476] -- url origin pinned above
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
    console.error("Fatal:", logSafe(err.message)); // NOSONAR [jssecurity:S5145] -- err.message is our own, path-sanitised error text
    process.exit(1);
  }
}

module.exports = { BASE_URL, EMAIL, PASSWORD, api, logSafe, auth, runMain };
