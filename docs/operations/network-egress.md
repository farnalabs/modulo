# Network Egress Audit

> Last updated: 2026-06-25
>
> This document enumerates all outbound network connections made by Modulo
> components. It is the single source of truth for data residency compliance
> (PRD §10.5) and SOC 2 evidence.

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

✅ **Data residency OK**: Telemetry is opt-in. No telemetry data leaves the
process without explicit operator configuration.

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

- Delivery retries: up to 3 times with exponential backoff (5s, 25s, 125s)
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

The library registry is a local database table. Community registry protocol
(v2) is planned but not yet implemented. No outbound calls occur during
library browsing.

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
