# Network Egress Audit

> Last updated: 2026-09-30
>
> This document enumerates all outbound network connections made by Modulo
> components. It is the single source of truth for data residency compliance
> (principle: `docs/core-principles.md` §9 "Self-hosted, no telemetry by
> default"; the PRD is retired) and SOC 2 evidence.

---

## 1. Default Configuration – Zero Egress

With default settings and **no connectors configured**, Modulo makes **zero
external network calls**. There are no hardcoded DNS resolutions, phone-home
mechanisms, telemetry endpoints, or cloud API calls in the base runtime.

This satisfies the "no external DNS calls in default config" requirement.

### What runs locally (no egress)

| Service | Port | Notes |
|---|---|---|
| FastAPI backend | 8000 | Local listener, no egress |
| Vue frontend (dev) | 5173 | Dev server, no egress |
| Postgres | 5434 (local Docker) | Local container, no egress |
| Redis | 6380 (local Docker) | Local container, no egress |

---

## 2. Telemetry & Observability

### OpenTelemetry (default: **disabled**)

| Setting | Default | Egress When Enabled |
|---|---|---|
| `MODULO_TELEMETRY_ENABLED` | `false` | When `true`: writes JSON lines to stdout (no network) |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | not set | When set + telemetry enabled: HTTPS POST to configured endpoint |
| OTel test connection (`POST /api/v1/settings/observability/test`) | – | Manual test: HTTPS POST to user-specified endpoint |

✅ **Data residency OK**: Telemetry is off by default. When opted in via `MODULO_TELEMETRY_ENABLED=true` (Settings > Runtime Configuration), only aggregate counters and sanitised error categories are exported, no personal data, pipeline content, API keys, or raw error messages. Organisation ids are truncated hashes, not raw identifiers.

> **Cross-process note:** the runtime-config override applies to the web process immediately. The SAQ worker is a separate process with its own store instance, so for consistent cross-process behaviour, set `MODULO_TELEMETRY_ENABLED` in the deployment environment rather than relying on the runtime override.

### LangSmith

LangSmith tracing is disabled by default. It can be enabled per-org via the
settings UI, which stores an encrypted API key. Egress uses the configured
LangSmith endpoint.

---

## 3. Connectors (user-configured, opt-in)

All third-party API calls require explicit operator configuration. No connector
makes outbound calls until a user creates a ConnectorInstance with credentials.

| Connector | Default Base URL | Egress |
|---|---|---|
| GitHub | `https://api.github.com` | API calls on user-configured triggers |
| GitLab | `https://gitlab.com/api/v4` | API calls on user-configured triggers |
| Linear | `https://api.linear.app` | API calls on user-configured triggers |
| Jira | User-configured instance URL | API calls on user-configured triggers |
| Slack | `https://slack.com/api` | API calls on user-configured triggers |
| GitHub Actions CI | `https://api.github.com` | API calls on user-configured triggers |
| GitLab CI Runner | `https://gitlab.com/api/v4` | API calls on user-configured triggers |
| Filesystem | N/A (local) | No network egress |

### Default credential URLs

The Ollama model backend defaults to `http://localhost:11434/v1` – a local-only
address. All other model backends (Anthropic, OpenAI) require explicit
configuration of API keys and endpoints.

---

## 4. Webhooks (user-configured, opt-in)

Webhooks are fully user-configured. The operator provides the target URL. No
webhook payloads are sent to hardcoded endpoints.

- Delivery retries: up to 4 attempts (1 initial + 3 retries) with backoff
  delays of 1s, 5s, 30s (`MAX_ATTEMPTS` / `RETRY_DELAYS` in
  `backend/src/modulo/core/notifier/__init__.py`); a 429 `Retry-After`
  response header is honoured (capped at 60s)
- Signing: HMAC-SHA256 with per-webhook secret
- Payload: JSON body with run/event context

---

## 5. SSO / OIDC (user-configured, opt-in)

| Protocol | Egress |
|---|---|
| OIDC | HTTPS GET to the IdP's discovery URL (configured by operator) |
| SAML 2.0 | HTTPS POST to IdP's ACS endpoint (configured by operator) |

---

## 6. Plugin Registry / Library

The library registry is a local database table. Community library
browse/install is implemented (FAR-363): when `MODULO_LIBRARY_ENDPOINT` is
set (default empty, meaning no library is configured), the SAQ
`library_sync` cron polls that endpoint for the signed manifest (outbound
HTTPS every `MODULO_LIBRARY_SYNC_INTERVAL_SECONDS`, default 300s) and caches
the catalog locally; installs fetch content-addressed blobs from the same
endpoint with SHA-256 verification. With the default empty endpoint,
library browsing makes no outbound calls.

---

## 7. Licensing

License validation is local-only. No phone-home calls are made. The license
key is verified against a local algorithm.

---

## 8. Frontend

The frontend makes API calls exclusively to the backend it was built for.
No third-party CDNs, analytics scripts, or tracking pixels are loaded.

- All JS/CSS is self-hosted (no CDN)
- No Google Analytics, Mixpanel, Segment, or similar
- No external font loading
- No tracking pixels

---

## 9. Summary

| Category | Default Egress | User-Configurable Egress |
|---|---|---|
| Telemetry (OTel) | None | Yes – when `MODULO_TELEMETRY_ENABLED=true` |
| Connectors | None | Yes – per-connector API config |
| Webhooks | None | Yes – per-webhook URL config |
| SSO/OIDC | None | Yes – IdP discovery URL |
| Library registry | None | None (TBD v2) |
| License check | None | None |
| Frontend | None | None |

All outbound network calls require explicit operator action. Modulo
never initiates external connections without configuration.

---

## Appendix: Verification Commands

```bash
# Check if telemetry is enabled
docker compose -f deploy/compose/docker-compose.prod.yml exec modulo env | grep MODULO_TELEMETRY

# Check if OTLP endpoint is configured
docker compose -f deploy/compose/docker-compose.prod.yml exec modulo env | grep OTEL_EXPORTER_OTLP

# List all active connector instances (API)
curl -H "Authorization: Bearer $TOKEN" "$MODULO_URL/api/v1/connectors"

# List all configured webhook endpoints
curl -H "Authorization: Bearer $TOKEN" "$MODULO_URL/api/v1/admin/notifications"

# Verify no unexpected egress (requires host firewall / network policy monitoring)
docker compose -f deploy/compose/docker-compose.prod.yml exec modulo netstat -tlnp  # listening only
```
