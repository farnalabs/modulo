# ADR 009 — Frontend Monitor Backend Abstraction

**Date**: 2026-07-03
**Status**: Accepted

---

## Context

The app needs client-side error monitoring that self-hosters can opt into with
whichever service they already use (Sentry, Datadog, Grafana Cloud, etc.).

**What already exists:**

- **Backend** — a full forwarder system in `core/error_tracking/forwarders/`
  with `BaseForwarder` abstract class and implementations for Sentry, Datadog,
  Rollbar, PagerDuty, OpsGenie, and Loki. Forwarders are dispatched after
  the `ErrorIngestionService` ingests an error into the DB. Per-org config
  lives in `ErrorForwarderConfig` model with a UI in `SettingsErrorForwardersView.vue`.
  Separate from this, OTel tracing (`otel_bridge/export.py`) handles span
  export with console + OTLP exporters.

- **Frontend** — a custom `ErrorTracker` class (`src/lib/error-tracking/`)
  with Vue plugin (`errorHandler`/`warnHandler`), window-level handlers
  (`onerror`/`unhandledrejection`), breadcrumb capture (clicks, API calls,
  route changes), and a batched HMAC-signed transport to
  `POST /api/v1/errors/ingest`. No third-party SDK is shipped.

**Two existing bugs in the frontend error pipeline (must be fixed before or
as part of this ADR):**

1. **`source` field is wrong.** The frontend sends `source: config.appName`
   (`'modulo'`) but the backend validates it must be one of `'frontend'`,
   `'backend'`, or `'celery'`. Every ingest request returns 422. **No
   frontend error has ever reached the DB.**

2. **`session_key` field name mismatch.** The frontend `SessionKeyResponse`
   interface expects `session_key` but the backend returns `{"key": ...}`.
   Fix: update the frontend interface to expect `key` (matching the backend)
   and update test mocks accordingly.

**Gap:** The frontend ErrorTracker sends everything to the backend, which may
then forward to external services. This works for error events but means:

1. **No client-side SDK features** — session replays, RUM performance metrics,
   JS error source maps, real user monitoring are unavailable unless a browser
   SDK runs on the client.
2. **Single pipeline** — all errors go through one route (backend DB →
   optional forwarders). There is no way to send directly to Sentry/Datadog
   from the browser while still keeping the builtin DB pipeline.
3. **No abstraction** — adding a client SDK today means threading SDK init
   through `main.ts` with env-var gating and no consistent interface for
   swapping providers.

## Decision

Introduce a **`MonitorBackend`** abstraction on the frontend — a plugin
interface that mirrors the backend's `BaseForwarder` but for client-side SDKs.
The existing ErrorTracker is refactored to hold a **registry of
MonitorBackends** and dispatches every captured event to all registered
backends in parallel.

### MonitorBackend interface

```typescript
interface UserInfo {
  id: string
  email?: string
  name?: string
  role?: string
}

interface MonitorBackend {
  /** Unique key used in config and logs. */
  readonly key: string

  /** Called once at app startup. Return false to skip registration. */
  init(config: MonitorConfig): boolean | Promise<boolean>

  /** Capture a pre-serialized error event. */
  captureError(event: ErrorEventInput): void

  /**
   * Capture a raw Error object (richer context for third-party SDKs).
   *
   * Implement EITHER captureError OR captureRawError for a given event,
   * not both, to avoid double-counting. The dispatch layer sends the raw
   * Error to backends implementing this method and the serialized event
   * to those that don't.
   */
  captureRawError?(error: Error, context?: Record<string, unknown>): void

  /** Capture a message at a given severity level. */
  captureMessage(message: string, level: 'error' | 'warning' | 'critical'): void

  /** Add a breadcrumb for services that support structured breadcrumbs. */
  addBreadcrumb?(breadcrumb: Breadcrumb): void

  /** Identify the current user. Pass null on logout to clear. */
  setUser?(user: UserInfo | null): void

  /** Set session-level tags (org ID, plan tier, environment). */
  setTags?(tags: Record<string, string>): void

  /** Called when the Vue app unmounts. */
  dispose?(): void
}
```

### Built-in backends (shipped with the app)

| Key | Class | Dependencies | Import style | Features |
|---|---|---|---|---|
| `builtin` | `BuiltinMonitorBackend` | None | Static | DB-backed error storage, alerting, backend forwarder dispatch |
| `sentry` | `SentryMonitorBackend` | `@sentry/vue` (optional) | Dynamic `import()` in `init()` | Error grouping, session replays, source maps, performance |
| `datadog-rum` | `DatadogRumMonitorBackend` | `@datadog/browser-rum` (optional) | Dynamic `import()` in `init()` | RUM, session replays, real user metrics, logs |
| `grafana-faro` | `GrafanaFaroMonitorBackend` | `@grafana/faro-web-sdk` (optional) | Dynamic `import()` in `init()` | OpenTelemetry-based, open source, Grafana Cloud |

Each backend receives a typed subset of `MonitorConfig`:

```typescript
interface MonitorConfig {
  builtin?: { enabled: boolean }
  sentry?: { dsn: string; environment?: string; replaysSessionSampleRate?: number }
  'datadog-rum'?: { clientToken: string; site?: string; service?: string; env?: string; version?: string }
  'grafana-faro'?: { url: string; apiKey?: string; appName?: string }
}
```

### CSP strategy

Third-party SDKs connect to external domains. Since CSP `<meta>` tags cannot
relax a CSP set by HTTP header (they can only tighten it — both policies are
enforced independently, resources must pass both), the frontend cannot
supplement the backend's CSP from the client side.

**Approach:** The backend CSP header is a **superset** that includes all known
third-party monitoring domains. The frontend does not inject or manage CSP.

**Backend CSP** (update `SecurityHeadersMiddleware`):

```
default-src 'self';
connect-src 'self' ws: wss: *.ingest.sentry.io *.datadoghq.com *.dd.dg *.rum.browserevents.com;
script-src 'self' 'unsafe-inline';
style-src 'self' 'unsafe-inline';
frame-ancestors 'none';
```

The `ws:`/`wss:` addition covers existing WebSocket connections. The
monitoring domains are well-known and slow-changing — using a superset avoids
per-backend CSP complexity.

**Custom domains** (for Grafana Faro collectors, self-hosted Sentry, etc.):
Add `MODULO_MONITOR_DOMAINS` to `Settings` (comma-separated list of origins
merged into `connect-src`). Default: empty.

**Documentation:** Each MonitorBackend's privacy data sheet lists its required
domains. Operators adding a custom collector set `MODULO_MONITOR_DOMAINS`.

### ErrorTracker refactor

The current ErrorTracker holds the registry and all handler setup becomes
instance methods to prevent double-counting:

```typescript
class ErrorTracker {
  private backends: MonitorBackendRegistry

  constructor(config?: ErrorTrackerConfig) {
    this.backends = new MonitorBackendRegistry(config?.monitorBackends)
    this.breadcrumbs = new BreadcrumbCollector(50)
    if (!isDisabled()) {
      this.breadcrumbs.startAutoCapture()
      initTransport(onAuthChange)
      this.installWindowHandlers()
    }
  }

  captureError(error: Error, context?: Record<string, unknown>): void {
    if (isDisabled()) return
    const event = buildErrorEvent(error, context)
    if (event) {
      // Dispatch raw Error to backends that implement captureRawError
      this.backends.dispatchFiltered('captureRawError', error, context)
      // Dispatch serialized event to backends that DON'T implement captureRawError
      this.backends.dispatchFiltered('captureError', event, { excludeBackendsWith: 'captureRawError' })
    }
  }

  captureMessage(message: string, level): void {
    if (isDisabled()) return
    const event = buildMessageEvent(message, level)
    this.backends.dispatch('captureMessage', event, level)
  }

  setUser(user: UserInfo | null): void {
    this.backends.dispatch('setUser', user)
  }

  setTags(tags: Record<string, string>): void {
    this.backends.dispatch('setTags', tags)
  }

  /** Replaces the old module-level createVuePlugin(). */
  installVuePlugin(app: App): void {
    app.config.errorHandler = (err, _instance, info) => {
      if (isDisabled()) return
      console.error(`[vue] ${info}:`, err)
      const error = err instanceof Error ? err : new Error(String(err))
      this.captureError(error, { vueInfo: info })
    }

    app.config.warnHandler = (msg, instance, trace) => {
      if (isDisabled()) return
      this.captureMessage(msg, 'warning')
      if (app.config.warnHandler._original) {
        app.config.warnHandler._original(msg, instance, trace)
      }
    }
  }

  private installWindowHandlers(): void {
    window.addEventListener('error', (event) => { this._onError(event) })
    window.addEventListener('unhandledrejection', (event) => { this._onRejection(event) })
  }
}

class MonitorBackendRegistry {
  private backends: MonitorBackend[] = []

  dispatch(method: string, ...args: unknown[]): void {
    for (const backend of this.backends) {
      try {
        const fn = (backend as any)[method]
        if (typeof fn === 'function') fn.apply(backend, args)
      } catch (e) {
        console.warn(`[monitor] ${backend.key}.${method} failed:`, e)
      }
    }
  }

  dispatchFiltered(method: string, ...args: unknown[]): void {
    // Exclude backends that have a specific other method
    const excludeMethod = typeof args[args.length - 1] === 'object'
      ? (args.pop() as any)?.excludeBackendsWith : undefined
    for (const backend of this.backends) {
      if (excludeMethod && typeof (backend as any)[excludeMethod] === 'function') continue
      try {
        const fn = (backend as any)[method]
        if (typeof fn === 'function') fn.apply(backend, args)
      } catch (e) {
        console.warn(`[monitor] ${backend.key}.${method} failed:`, e)
      }
    }
  }
}
```

The module-level `_errorHandler`, `_rejectionHandler`, `installWindowHandlers`,
and `removeWindowHandlers` functions are **removed** — they are replaced by the
instance methods above.

**No double-counting:** A backend that implements `captureRawError` receives
the raw Error but NOT the serialized `ErrorEventInput`. A backend that only
implements `captureError` receives the serialized event. No backend should
implement both.

### Runtime activation (not just build-time)

Backend selection uses **two layers**:

1. **Build-time** (`VITE_MONITOR_BACKEND`) — determines which SDK code is
   compiled into the bundle. The default Docker build includes all backends:
   `VITE_MONITOR_BACKEND=builtin,sentry,datadog-rum,grafana-faro`. Power
   users can narrow this to reduce bundle size.

2. **Runtime** (`window.__MODULO_CONFIG__`) — determines which backends
   actually call `init()`. Injected via the container environment variable
   `MODULO_MONITOR_CONFIG` which the backend (or a startup script) writes
   into `index.html` as:

```html
<script>
window.__MODULO_CONFIG__ = JSON.parse('{"monitorBackends":["builtin","sentry"],"sentry":{"dsn":"https://xxx@o123.ingest.sentry.io/123"}}');
</script>
```

A self-hoster can switch from "builtin only" to "builtin + sentry" without
rebuilding:

```yaml
# docker-compose.yml
services:
  app:
    environment:
      MODULO_MONITOR_CONFIG: '{"monitorBackends":["builtin","sentry"],"sentry":{"dsn":"https://xxx@o123.ingest.sentry.io/123"}}'
```

The `loadMonitorConfig()` function on the frontend merges both layers:

```typescript
// src/monitor/config.ts
export function loadMonitorConfig(): MonitorConfig {
  // 1. Runtime config (from container env var, injected into index.html)
  const runtimeConfig: { monitorBackends?: string[]; [key: string]: unknown }
    = (window as any).__MODULO_CONFIG__?.monitor
      ?? { monitorBackends: undefined }

  // 2. Build-time config (VITE_* env vars, baked into bundle)
  const buildTimeBackends = (
    import.meta.env.VITE_MONITOR_BACKEND ?? 'builtin'
  ).split(',').map(s => s.trim())

  // 3. Runtime takes precedence, but only backends in build-time can load
  const activeBackends = runtimeConfig.monitorBackends ?? buildTimeBackends

  // 4. Per-backend config: runtime object, fallback to build-time VITE_* vars
  return {
    builtin: activeBackends.includes('builtin') ? { enabled: true } : undefined,
    sentry: activeBackends.includes('sentry') ? {
      dsn: (runtimeConfig.sentry as any)?.dsn ?? import.meta.env.VITE_SENTRY_DSN,
      environment: import.meta.env.MODE,
    } : undefined,
    'datadog-rum': activeBackends.includes('datadog-rum') ? {
      clientToken: (runtimeConfig['datadog-rum'] as any)?.clientToken
        ?? import.meta.env.VITE_DATADOG_RUM_CLIENT_TOKEN,
      site: (runtimeConfig['datadog-rum'] as any)?.site ?? 'datadoghq.com',
      service: (runtimeConfig['datadog-rum'] as any)?.service ?? 'modulo',
      env: import.meta.env.MODE,
    } : undefined,
    'grafana-faro': activeBackends.includes('grafana-faro') ? {
      url: (runtimeConfig['grafana-faro'] as any)?.url ?? import.meta.env.VITE_GRAFANA_FARO_URL,
    } : undefined,
  }
}
```

### Builtin backend: fixing existing bugs

The `BuiltinMonitorBackend` fixes the two existing bugs:

1. **Hardcodes `source: 'frontend'`** in all events sent to the backend.
2. **Reads `data.key`** from the session-key response (matching the backend's
   actual field name).

Additionally, it handles **unauthenticated errors** via a new public ingest
endpoint: `POST /api/v1/errors/ingest/public`. This endpoint:
- Requires no auth
- Accepts only `source=frontend` and `level` (warning or lower)
- Rate-limited to 1/60s per IP
- Max body size 10,000 bytes
- Daily cap of 100 events per IP
- Stores errors with a 48-hour TTL (auto-pruned, no permanent storage)
- Is exposed to the frontend when `getAccessToken()` is null

**Transitional note:** The public ingest endpoint is added in the
pre-implementation step (backend change) but only becomes effective once
the `BuiltinMonitorBackend` auth-detection logic is deployed (step 2 of the
implementation plan). Between these two steps, unauthenticated frontend
errors are still dropped.

### i18n missing-key handler

```typescript
const i18n = createI18n({
  // ...
  missing: (locale, key) => {
    console.warn(`[i18n] Missing translation key: ${key} (locale=${locale})`)
  },
})
```

The `missing` handler logs to console. The **existing** `warnHandler` on the
Vue app (now routed through `this.captureMessage()` in the refactored
ErrorTracker) already captures `console.warn` messages. This means the i18n
missing-key warning is automatically routed to all MonitorBackends.

To prevent DB spam, `MonitorBackendRegistry` rate-limits `captureMessage`:
- Per-message content hash: max 1 per 60s per unique key
- Global session cap: max 100 captureMessage calls per minute per tracker
  instance

### Unauthenticated error ingest

Errors that occur on pages where the user is not authenticated (login page,
expired session, public pages) are handled by:

1. The `BuiltinMonitorBackend` checks `getAccessToken()` before sending.
   If null, it routes to `POST /api/v1/errors/ingest/public` instead of the
   authenticated ingest endpoint.
2. The public endpoint is rate-limited (1/60s per IP, 100/day per IP,
   10KB max body) and stores events with a 48-hour TTL only.
3. Third-party backends (Sentry, Datadog, Grafana) do not need auth — they
   authenticate with their own DSN/client token, so unauthenticated errors
   work out of the box.

### Auth flow wiring (design requirement, not just implementation note)

The auth store must be wired to `ErrorTracker.setUser()` and `setTags()`:

```
Login success → tracker.setUser({ id, email, name, role })
               tracker.setTags({ orgId, tier, env })
               
Logout / 401  → tracker.setUser(null)
               tracker.setTags({})
```

This is required for third-party backends to correlate errors to users.
If not wired, Sentry/Datadog reports show errors with no user context and
no indication that the wiring is missing.

### Per-backend privacy data sheets

Every MonitorBackend implementation must include a JSDoc block documenting:

- **Domains contacted** (e.g., `*.ingest.sentry.io`)
- **Data fields collected** (page URL, user agent, console logs, DOM snapshots)
- **Cookies set** (names, purpose, expiration)
- **Config knobs to limit data** (e.g., `replaysSessionSampleRate: 0` to disable replays)
- **Known data residency** (US, EU, configurable)

Example (SentryMonitorBackend):

```typescript
/**
 * Sentry MonitorBackend
 *
 * Domains: *.ingest.sentry.io, *.sentry.io
 * Data: error stack traces, breadcrumbs, user-agent, URL,
 *       performance metrics, session replays (if enabled)
 * Cookies: sentry* (session replay opt-out, ~1yr)
 * Config: replaysSessionSampleRate (0 = no replays),
 *         replaysOnErrorSampleRate (0 = no error replays)
 * Residency: configurable via DSN endpoint
 */
```

**Enforcement:** All shipped backends must have an approved data sheet.
The `backends/index.ts` PR template includes a data-sheet checklist.
Backends without completed data sheets are rejected at review.

### Dependency strategy

Third-party SDKs (`@sentry/vue`, `@datadog/browser-rum`,
`@grafana/faro-web-sdk`) are listed in **`optionalDependencies`** in
`package.json`. This means:

- `npm ci` without `--omit=optional` downloads all SDKs (default Docker build).
- Self-hosters who want a lean image add `--omit=optional` to `npm ci`:
  `npm ci --omit=optional && npm run build`
- The dynamic `import()` calls in each backend gracefully handle missing
  packages (catch the module-not-found error and log a clear message).

Each backend's `init()` wraps `import()` in a try/catch:

```typescript
async init(config: MonitorConfig): Promise<boolean> {
  try {
    const Sentry = await import('@sentry/vue')
    Sentry.init({ dsn: config.sentry?.dsn, /* ... */ })
    return true
  } catch (e) {
    if ((e as any)?.code === 'MODULE_NOT_FOUND') {
      console.warn('[monitor] @sentry/vue not installed — skipping Sentry backend')
    } else {
      console.error('[monitor] Sentry init failed:', e)
    }
    return false
  }
}
```

The `@datadog/browser-rum` backend uses the same dynamic `import()` pattern,
but calls it inside `init()` (before any user interaction) so the SDK is
loaded early enough to capture initial page metrics. The import is dynamic,
not static — if the package is absent, the catch block handles it gracefully.
The word "eager" describes the *timing* (called immediately in `init()`),
not the import syntax.

### File layout

```
frontend/src/monitor/
├── types.ts               # MonitorBackend, MonitorConfig, UserInfo interfaces
├── registry.ts            # MonitorBackendRegistry class
├── config.ts              # loadMonitorConfig() from VITE_* + __MODULO_CONFIG__
├── backends/
│   ├── builtin.ts         # BuiltinMonitorBackend — transport.ts wrapper, source: 'frontend'
│   ├── sentry.ts          # SentryMonitorBackend — @sentry/vue (dynamic import)
│   ├── datadog-rum.ts     # DatadogRumMonitorBackend — @datadog/browser-rum (dynamic import in init)
│   ├── grafana-faro.ts    # GrafanaFaroMonitorBackend — @grafana/faro-web-sdk (dynamic import)
│   └── index.ts           # loadBackends() factory
└── __tests__/
    ├── registry.spec.ts
    ├── builtin.spec.ts
    ├── sentry.spec.ts
    ├── datadog-rum.spec.ts
    └── grafana-faro.spec.ts
```

### Changes to existing files

| File | Change |
|---|---|
| `src/main.ts` | `createErrorTracker()` moved before `app.use(i18n)`. Auth flow wires `tracker.setUser()`/`setTags()` on login/logout. |
| `src/lib/error-tracking/index.ts` | ErrorTracker gains `MonitorBackendRegistry`. Vue plugin + window handlers become instance methods using `this.backends.dispatch()`. Add `setUser()`, `setTags()`. **Remove** module-level `_errorHandler`, `_rejectionHandler`, `installWindowHandlers`, `removeWindowHandlers` — replaced by instance methods. |
| `src/lib/error-tracking/types.ts` | Add `monitorBackends?: MonitorConfig` to `ErrorTrackerConfig`. Add `UserInfo` type. |
| `src/lib/error-tracking/transport.ts` | No change (used by `BuiltinMonitorBackend`). |
| `src/i18n/index.ts` | No change needed — the Vue `warnHandler` (now routed through registry) captures `console.warn` from the `missing` handler. |
| `vite.config.ts` | No change. |
| `Dockerfile.prod` | Default `VITE_MONITOR_BACKEND=builtin,sentry,datadog-rum,grafana-faro`. Add `ARG VITE_SENTRY_DSN` etc. as pass-through. |
| `index.html` | Add `<script>` injection point for `window.__MODULO_CONFIG__` populated from `MODULO_MONITOR_CONFIG` env var. |
| Backend `SecurityHeadersMiddleware` | Update CSP: add `ws:`/`wss:` to `connect-src`, add `*.ingest.sentry.io *.datadoghq.com *.dd.dg *.rum.browserevents.com`. |
| Backend `Settings` | Add `modulo_monitor_domains` (comma-separated, merged into CSP `connect-src`). |
| Backend `ErrorIngestionService` | Add `POST /api/v1/errors/ingest/public` endpoint. |

### Data flow diagram

```
                          ┌─ builtin ──→ POST /api/v1/errors/ingest ──→ ErrorIngestionService ──→ BackendForwarders
Browser Error ─→ ErrorTracker ─┼─ sentry ──→ Sentry SDK (via dynamic import)
                          ├─ datadog ─→ Datadog RUM SDK (via dynamic import in init)
                          └─ faro ────→ Grafana Faro SDK (via dynamic import)
                                                                              │
                                                                              ↓
                                                                         DB (error_groups, error_events)
```

### Upgrade impact for existing self-hosters

- **Do I need to change docker-compose.yml?** No — the default Docker build
  includes all backends, but only `builtin` activates (no `MODULO_MONITOR_CONFIG`
  set → runtime config is undefined → `activeBackends` falls back to
  `['builtin']` from the build-time default).
- **Will existing errors still be visible?** They were never reaching the DB
  (the `source: 'frontend'` bug caused 422 on every ingest). After this ADR,
  frontend errors will successfully reach the DB for the first time — this is
  a free improvement, not a regression.
- **Do npm dependencies increase?** Yes — `node_modules` grows by the three
  optional SDK packages (~2–5 MB each) in the default Docker build. Run
  `npm ci --omit=optional` to exclude them.
- **What breaks if I do nothing?** Nothing. The builtin backend is the default
  and the ErrorTracker pipeline is unchanged (except the critical bugfixes).
- **Can I add Sentry later without rebuilding?** Yes — set `MODULO_MONITOR_CONFIG`
  on the container and redeploy (same image).

## Consequences

**Positive:**

- Self-hosters choose zero-config (builtin only), single-provider, or multi-provider
  via runtime config — no rebuild for switching.
- The `builtin` backend is always available and requires zero external services,
  preserving the current privacy posture.
- Frontend SDK features (session replays, source maps, RUM) become available
  without backend changes.
- The i18n missing-key handler is automatically captured via the existing Vue
  `warnHandler` — no custom wiring needed.
- Each backend is independently optional — failure in one does not affect others.
- Adding a new provider is a single file plus data sheet; the `MonitorBackend`
  interface is small (9 methods, 4 optional).
- Per-backend privacy data sheets equip self-hosters to evaluate compliance.

**Negative:**

- Third-party SDK packages increase `node_modules` size when installed. Mitigated
  by `optionalDependencies` — self-hosters can omit them.
- Third-party SDKs may set cookies, collect performance data, or send additional
  telemetry. This is documented per-backend in the privacy data sheet.
- The runtime `__MODULO_CONFIG__` injection mechanism adds a new deployment
  pattern (container env var → `index.html` injection).
- CSP header becomes more permissive (`*.ingest.sentry.io` etc. in connect-src),
  reducing the blast radius if an XSS is found. Mitigated by keeping
  `script-src 'self'` and `frame-ancestors 'none'` as hard boundaries.

## Alternatives considered

### Proxy all frontend errors through backend only (status quo)

Keep the current architecture where the frontend ErrorTracker sends everything
to `POST /api/v1/errors/ingest` and rely solely on backend forwarders.

Rejected because: client-side SDK features (session replays, RUM performance,
source map resolution) are unavailable. The backend cannot attach browser
context (viewport, interaction timeline, console log history) that Sentry's
browser SDK collects automatically.

### Frontend sets CSP via `<meta>` tag (no backend CSP for connect-src)

Rejected because CSP `<meta>` tags cannot relax an HTTP header CSP —
both are enforced independently, making it impossible for the frontend to
add monitoring domains that the header blocks. The backend superset approach
is simpler and correct.

### Runtime config from backend /me/settings (no runtime override)

Rejected because: client SDKs must be loaded and initialised at page load —
before any API call completes. The dual-layer approach (build-time for SDK
availability, runtime for activation) is the pragmatic midpoint.

### OpenTelemetry for frontend

Use OpenTelemetry JS SDK as the single abstraction on the frontend, with
OTLP exporters for all targets.

Rejected because: OpenTelemetry JS does not have the rich session replay or
RUM capabilities that Sentry/Datadog provide. Its Vue integration is thin
compared to `@sentry/vue` or `@datadog/browser-rum`. The backend uses OTel
for *tracing*, not error monitoring — these are separate concerns.

## Pre-implementation checklist (must be merged first)

1. **Fix `source` field bug** — change `buildBaseEvent()` in
   `src/lib/error-tracking/index.ts` to send `source: 'frontend'` instead of
   `source: config.appName`.
2. **Fix `session_key` field name** — update frontend `SessionKeyResponse` to
   expect `key` (matching backend's `{"key": ...}` response). Update test
   mocks.
3. **Add `POST /api/v1/errors/ingest/public` endpoint** — unauthenticated,
   rate-limited (1/60s per IP, 100/day per IP, 10KB max body),
   `source=frontend` only, `level` warning or lower, 48-hour TTL.

## Implementation plan

1. **Pre-implementation bugfixes** — fix `source` field, `session_key` field,
   add public ingest endpoint. Merge to main.
2. **Create `src/monitor/`** — `types.ts`, `registry.ts`, `config.ts`,
   `backends/builtin.ts`, `backends/index.ts`
3. **Refactor ErrorTracker** — move Vue plugin + window handlers to instance
   methods, add `MonitorBackendRegistry`, pipe `captureError`/`captureMessage`
   through it, add `setUser()`/`setTags()`. Remove old module-level handlers.
4. **Add Sentry backend** — `backends/sentry.ts` with dynamic `@sentry/vue` import
5. **Add Datadog RUM backend** — `backends/datadog-rum.ts` with dynamic import
   in `init()`, catch absence gracefully
6. **Add Grafana Faro backend** — `backends/grafana-faro.ts` with dynamic import
7. **Update CSP** — add monitoring domains to `SecurityHeadersMiddleware` CSP,
   add `MODULO_MONITOR_DOMAINS` to `Settings`
8. **Update Dockerfile.prod** — default `VITE_MONITOR_BACKEND` includes all
   backends, add `index.html` injection for `__MODULO_CONFIG__`
9. **Wire main.ts** — move `createErrorTracker()` before i18n, wire
   `authStore.onAuthChange` → `tracker.setUser()`/`setTags()` on login/logout
10. **Write docs** — per-backend privacy data sheets, operation docs page
    covering env vars and Docker config, upgrade guide for existing self-hosters
