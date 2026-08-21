---
id: feat-observability-monitoring-config
prd: 8.25.1
delivery-tasks: []
code:
  - backend/src/modulo/api/routes/admin_monitor_config.py
  - frontend/src/views/SettingsMonitorConfigView.vue
  - frontend/src/monitor/
  - frontend/src/manifest.yaml
  - frontend/src/router/index.ts
bdd:
  - backend/tests/bdd/features/observability/monitor_config.feature
unit-tests:
  - frontend/src/__tests__/monitor-config.spec.ts
  - backend/tests/unit/api/test_admin_monitor_config.py
depends-on: [feat-core-system-config]
status: partial
---

# Monitoring Configuration

Admin-level configuration for monitoring backends (Sentry, DataDog RUM, Grafana Faro). Stores backend selection and per-backend config in the `SystemConfig` table under the `monitor_backends` key.

## Behaviours

- [x] GET current monitor backend configuration
- [x] PUT to update monitor backend configuration
- [x] Defaults to built-in monitoring when unconfigured
- [x] Admin role required
- [x] Missing DB table returns 501 Not Implemented
- [x] DB errors return 503 Service Unavailable
- [x] Known backend names validated on PUT
- [x] Unknown backend name returns 422
- [x] Empty backend list returns 422
- [x] Missing credentials return 401/403
- [x] Stored config is merged with defaults on read
- [x] Per-backend required-field validation on PUT (Sentry requires `dsn`, Datadog RUM requires `clientToken`, Grafana Faro requires `url` when enabled)
- [x] PUT enabling a backend without its required field returns 422 with the missing field named in the error
- [ ] Support for additional monitoring backends — VERIFIED 2026-08-15 (partial-small-b sweep): NOT implemented; backend set is fixed at built-in + Sentry + Datadog RUM + Grafana Faro. Extending requires a new type in `MonitorBackendType`, a `_PER_BACKEND_REQUIRED_FIELDS` entry, and frontend `MonitorConfig` union support (see Known Gaps)

## Error Handling

- [x] `ProgrammingError` (missing table) → 501 Not Implemented with migration hint
- [x] `SQLAlchemyError` → 503 Service Unavailable
- [x] Unexpected `Exception` → 500 Internal Server Error
- [x] Non-admin (viewer) role → 403 Forbidden

## Known Gaps

- The frontend test (`frontend/src/__tests__/monitor-config.spec.ts`) tests only the legacy `config.ts` loader, not the `SettingsMonitorConfigView` component or the API integration.
- Only the built-in + Sentry + Datadog RUM + Grafana Faro backends are supported — no pluggable backend registry for additional monitoring vendors (verified 2026-08-15).

## QA History
- **2026-08-15 (coverage sweep partial-small-b): Verified all 18 checked behaviours against `admin_monitor_config.py` + `test_admin_monitor_config.py` + `monitor_config.feature` (incl. per-backend required-field 422 validation). The single unchecked behaviour “Support for additional monitoring backends” is a genuine gap — the backend set is fixed at built-in + Sentry + Datadog RUM + Grafana Faro; added to Known Gaps. 17/18 behaviours covered.**

- 2026-08-08: improve-architecture (product-map walk). Fixed the per-backend field validation gap — `MonitorConfigUpdate` now runs a `model_validator` that rejects PUTs enabling a backend whose required field is missing/empty (Sentry `dsn`, Datadog RUM `clientToken`, Grafana Faro `url`), returning 422 with the missing field named in the detail. Required-field keys mirror the frontend's `MonitorConfig` types (`frontend/src/monitor/types.ts`). Added 6 unit tests (missing dsn, null config, missing clientToken, missing url, positive sentry-with-dsn, builtin-only still accepted; existing datadog round-trip test updated to the canonical `clientToken` key) + 3 BDD scenarios with step definitions (enabling sentry/datadog/grafana without required fields → 422). Updated product map (2 behaviours `[ ]`→`[x]`, Known Gap removed, QA History).
- 2026-07-31: improve-architecture: Fixed MAJOR — removed duplicated `@router.get`/`@router.put` decorators in `admin_monitor_config.py` that double-registered the routes (the inner registration served the raw, unwrapped handler, making `handle_db_errors` dead code). Added 13 backend unit tests covering GET/PUT success, auth 401/403, 422 validation, and 501/503/500 error paths. Added BDD feature file (8 scenarios) + step definitions for monitor config (defaults, stored config, update, unknown/empty backends, viewer 403, 501, 503). Status: partial (per-backend field validation remains).
