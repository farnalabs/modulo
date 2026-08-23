# Monitoring Configuration

Modulo ships a built-in error tracking system (Community tier) and supports
optional third-party monitoring backends (Team tier – require a license key).
This page covers configuration for all monitoring options.

## Backends

Four monitor backends are shipped with the app. At least one is always active.

| Backend | Tier | SDK | Default | Purpose |
|---|---|---|---|---|
| `builtin` | Community | None | Always | DB-backed error storage, dashboard, alerting |
| `sentry` | Team | `@sentry/vue` | Optional | Sentry error tracking + session replays |
| `datadog-rum` | Team | `@datadog/browser-rum` | Optional | Datadog RUM + Logs |
| `grafana-faro` | Team | `@grafana/faro-web-sdk` | Optional | Grafana Faro / OpenTelemetry |

## Configuration

Backend selection uses two layers:

### Build-time (`VITE_MONITOR_BACKEND`)

Set as a Docker build arg. Controls which SDK code is compiled into the bundle.
Default: `builtin,sentry,datadog-rum,grafana-faro`

```dockerfile
ARG VITE_MONITOR_BACKEND="builtin,sentry"
ENV VITE_MONITOR_BACKEND=${VITE_MONITOR_BACKEND}
```

Set to `builtin` to exclude all third-party SDKs from the bundle:
```bash
docker build --build-arg VITE_MONITOR_BACKEND=builtin -t modulo .
```

### Runtime (`MODULO_MONITOR_CONFIG`)

Set as a container environment variable. Controls which backends activate at
runtime without rebuilding the Docker image. Takes precedence over build-time.

```yaml
# docker-compose.yml
services:
  app:
    environment:
      MODULO_MONITOR_CONFIG: '{"monitorBackends":["builtin","sentry"],"sentry":{"dsn":"https://xxx@o123.ingest.sentry.io/123"}}'
```

### Per-backend env vars

| Env var | For | Required |
|---|---|---|
| `VITE_SENTRY_DSN` | Sentry DSN URL | Yes (for Sentry) |
| `VITE_DATADOG_RUM_CLIENT_TOKEN` | Datadog RUM client token | Yes (for Datadog RUM) |
| `VITE_GRAFANA_FARO_URL` | Grafana Faro collector URL | Yes (for Grafana Faro) |

These can be set at build time (`ARG`/`ENV` in Dockerfile) or at runtime via
`MODULO_MONITOR_CONFIG`.

## CSP

When third-party monitoring backends are enabled, the backend
`SecurityHeadersMiddleware` includes the required domains in the
`Content-Security-Policy` `connect-src` directive.

### Default allowed domains

| Backend | Domains in `connect-src` |
|---|---|
| `sentry` | `*.ingest.sentry.io` |
| `datadog-rum` | `*.datadoghq.com`, `*.dd.dg`, `*.rum.browserevents.com` |
| `grafana-faro` | User-configured collector URL |

### Custom domains

For self-hosted Sentry, custom Grafana Faro collectors, or other custom
endpoints, set `MODULO_MONITOR_DOMAINS`:

```yaml
environment:
  MODULO_MONITOR_DOMAINS: "sentry.example.com,faro-collector.example.com"
```

These are appended to the `connect-src` directive. Note: semicolons are
rejected to prevent CSP injection.

## Upgrade Impact (from no monitoring to monitoring)

- **No `MODULO_MONITOR_CONFIG` set**: only the `builtin` backend activates.
  No data leaves the deployment. No behavior change.
- **npm dependencies increase**: the default Docker build installs all
  optional SDKs (`@sentry/vue`, `@datadog/browser-rum`, `@grafana/faro-web-sdk`).
  Run `pnpm install --omit=optional` to exclude them (the frontend uses pnpm, not npm).
- **Existing errors**: the `source: 'frontend'` bugfix means frontend errors
  now successfully reach the DB for the first time. This is a free improvement.
- **No rebuild needed to switch backends**: use `MODULO_MONITOR_CONFIG` at
  runtime.

## Privacy Data Sheets

### Builtin

| Item | Detail |
|---|---|
| Domains contacted | None (same-origin only) |
| Data collected | Error message, stack trace, page URL, user agent |
| Cookies | None |
| Residency | Your own deployment |
| CSP required | None |

### Sentry

| Item | Detail |
|---|---|
| Domains contacted | `*.ingest.sentry.io`, `*.sentry.io` |
| Data collected | Error stack traces, breadcrumbs, user-agent, URL, performance metrics, session replays (if enabled) |
| Cookies | `sentry*` (session replay opt-out, ~1yr persistence) |
| Config knobs | `replaysSessionSampleRate` (0 = no replays), `replaysOnErrorSampleRate` (0 = no error replays), `tracesSampleRate` (0 = no performance) |
| Residency | Configurable via DSN endpoint (US: `o1.ingest.us.sentry.io`, EU: `o1.ingest.sentry.io`) |
| CSP required | `connect-src *.ingest.sentry.io` |

### Datadog RUM

| Item | Detail |
|---|---|
| Domains contacted | `*.datadoghq.com`, `*.dd.dg`, `*.rum.browserevents.com` |
| Data collected | RUM performance metrics, resource timings, user interactions, console logs (if enabled), viewport, page URL, user-agent |
| Cookies | `_dd_*`, `dd_*` (session replay, persistent ~1yr) |
| Config knobs | `sessionSampleRate` (0-100), `sessionReplaySampleRate` (0-100), `trackUserInteractions`, `trackResources`, `trackLongTasks` |
| Residency | Configurable via `site` parameter (US: `datadoghq.com`, EU: `datadoghq.eu`, US3: `us3.datadoghq.com`) |
| CSP required | `connect-src *.datadoghq.com *.dd.dg *.rum.browserevents.com` |

### Grafana Faro

| Item | Detail |
|---|---|
| Domains contacted | User-configured collector URL |
| Data collected | Error stack traces, OTEL traces, user-agent, page URL, resource timings, console logs |
| Cookies | None (Faro intentionally does not set cookies) |
| Config knobs | `url` (collector endpoint, required), `apiKey` (optional, for authenticated collectors) |
| Residency | Determined by collector URL (user-controlled) |
| CSP required | `connect-src <collector-url>` |
