---
id: feat-observability-data-residency
prd: 10.5, 6.6, 6.2
delivery-tasks: [task-nv0-data-residency]
bdd:
  - backend/tests/features/personas/marcus-ciso.feature
  - backend/tests/bdd/features/observability/otel_traces.feature
  - backend/tests/bdd/features/observability/metrics.feature
  - backend/tests/bdd/steps/test_observability.py
unit-tests:
  - backend/tests/unit/otel_bridge/test_telemetry_toggle.py
  - backend/tests/unit/otel_bridge/test_export.py
  - backend/tests/unit/otel_bridge/test_handler.py
  - backend/tests/unit/connector_hub/test_traced_connector.py
  - backend/tests/unit/api/test_environments.py
  - backend/tests/integration/crud/test_environment_profiles.py
code:
  - backend/src/modulo/settings.py
  - backend/src/modulo/otel_bridge/export.py
  - backend/src/modulo/otel_bridge/handler.py
  - backend/src/modulo/api/main.py
  - backend/src/modulo/core/runtime_provider/__init__.py
  - backend/src/modulo/db/models/environment_profile.py
  - backend/src/modulo/db/crud/environment_profile.py
  - backend/src/modulo/api/routes/environments.py
  - backend/src/modulo/core/library_service/__init__.py
depends-on:
  - feat-observability-otel-config-ui
  - feat-core-runtime-provider-core
status: partial
---

# Data Residency

Self-hosted Modulo deployments keep all data within the organisation's infrastructure.
Telemetry is opt-in and disabled by default. Network egress requires explicit operator
configuration at every layer.

## Behaviours

### Telemetry & Observability

- [x] `MODULO_TELEMETRY_ENABLED` defaults to `false` at startup
- [x] When telemetry is disabled, a no-op OTel provider is registered (no exporters)
- [x] When telemetry is enabled, stdout (ConsoleSpanExporter) is configured
- [x] When telemetry is enabled and `OTEL_EXPORTER_OTLP_ENDPOINT` is set, OTLP exporter is also configured
- [x] Invalid `OTEL_EXPORTER_OTLP_ENDPOINT` is logged and does not crash startup
- [x] `setup_otel()` is idempotent — safe to call multiple times
- [x] `shutdown_otel()` flushes and shuts down the global provider — safe to call multiple times
- [x] No credential fields, API keys, or user content appear in OTel span attributes
- [ ] No telemetry data leaves the process without explicit operator configuration
- [x] `LangGraphOtelBridge` maps LangGraph node events to OTel spans
- [ ] Pipeline runs emit OTel spans for LLM calls, connector operations, and trigger events
- [x] Span attributes include `organisation_id` and `pipeline_id`
- [x] Decrypted credentials never enter OTel span attributes (credential-in-state rule)

### Network Egress Control

- [ ] Default configuration makes zero external network calls
- [ ] No hardcoded DNS resolutions, phone-home mechanisms, or cloud API calls in base runtime
- [x] `EnvironmentProfile.egress_policy` defaults to `null` (unrestricted)
- [x] Egress policy can be set to `deny_all`, `allow_all`, or `allow_listed` (validated via regex)
- [x] Invalid egress_policy value is rejected at the API layer with 422
- [ ] Library primitives declare `required_environment_capabilities` (e.g. `egress:github.com`)
- [ ] Runtime provider enforces egress_policy on workspace creation
- [ ] VPC deployment checklist verifies all egress is to known internal services only

### Self-Hosted Data Residency

- [x] Self-hosted deployment keeps all data within the organisation's infrastructure
- [x] No agent output, source code, or credentials leave the VPC
- [ ] Modulo can be deployed with zero internet access (air-gapped)
- [ ] Connectors require explicit operator configuration before making outbound calls
- [ ] Webhooks are fully user-configured — no hardcoded endpoints
- [ ] Notifications are sent only to operator-configured webhook URLs
- [ ] SSO/OIDC requires operator-configured IdP URL
- [ ] License validation is local-only — no phone-home
- [ ] Frontend loads no third-party CDNs, analytics scripts, or tracking pixels
- [ ] Network egress audit (`docs/operations/network-egress.md`) is the single source of truth for SOC 2 evidence

### Multi-Region (V3 / SaaS — deferred)

- [ ] Region encoded in org metadata for multi-region routing
- [ ] Separate Postgres clusters per region
- [ ] modulo-cloud routing layer routes tenants to their regional cluster

### BDD & Test Coverage

- [x] `backend/tests/features/personas/marcus-ciso.feature` — `@goal-marcus-data-residency`
  delivered (no data leaves infrastructure)
- [x] `backend/tests/bdd/features/observability/otel_traces.feature` — 4 scenarios with step definitions in test_observability.py
- [x] `backend/tests/bdd/features/observability/metrics.feature` — 4 scenarios with step definitions in test_observability.py
- [x] `backend/tests/bdd/features/observability/run_logs.feature` — 4 scenarios with step definitions in test_observability.py
- [ ] `backend/tests/unit/otel_bridge/test_telemetry_toggle.py` — telemetry disabled by default; enabled configures exporters
- [ ] `backend/tests/unit/otel_bridge/test_export.py` — exporter configuration
- [ ] `backend/tests/unit/otel_bridge/test_handler.py` — OTel span creation
- [ ] `backend/tests/unit/connector_hub/test_traced_connector.py` — connector OTel tracing
- [ ] `backend/tests/unit/api/test_environments.py` — egress_policy API validation (invalid values return 422)
- [ ] `backend/tests/integration/crud/test_environment_profiles.py` — egress_policy CRUD roundtrip

## Known Gaps

- Multi-region data residency (V3 SaaS) is documented but not implemented
- No automated test enforces that telemetry is opt-in at the integration level
- No air-gapped deployment integration test exists
- PRD §10.5 describes an anonymous startup ping (`MODULO_TELEMETRY`) that is not implemented — no code sends an anonymous ping on startup
- Environment variable name mismatch: PRD §10.5 says `MODULO_TELEMETRY`, code uses `MODULO_TELEMETRY_ENABLED`
- `shutdown_otel()` multi-call safety has no dedicated unit test
- No integration test verifies null egress_policy defaults to `deny_all` at runtime (code in _build_workspace_spec treats null as deny_all, diverging from model default of null=unrestricted)
- Frontend loads no third-party CDNs, analytics scripts, or tracking pixels — needs audit
- Library primitives declaring `required_environment_capabilities` is unimplemented
