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
- [ ] Support for additional monitoring backends

## Error Handling

- [x] `ProgrammingError` (missing table) → 501 Not Implemented with migration hint
- [x] `SQLAlchemyError` → 503 Service Unavailable
- [x] Unexpected `Exception` → 500 Internal Server Error
- [x] Non-admin (viewer) role → 403 Forbidden

## Known Gaps

- Per-backend field validation (e.g. that Sentry's DSN is required when Sentry is enabled) is not implemented. The PUT endpoint validates backend names are from the known set but does not validate per-backend field schemas.
- The frontend test (`frontend/src/__tests__/monitor-config.spec.ts`) tests only the legacy `config.ts` loader, not the `SettingsMonitorConfigView` component or the API integration.

## QA History

- 2026-07-31: improve-architecture: Fixed MAJOR — removed duplicated `@router.get`/`@router.put` decorators in `admin_monitor_config.py` that double-registered the routes (the inner registration served the raw, unwrapped handler, making `handle_db_errors` dead code). Added 13 backend unit tests covering GET/PUT success, auth 401/403, 422 validation, and 501/503/500 error paths. Added BDD feature file (8 scenarios) + step definitions for monitor config (defaults, stored config, update, unknown/empty backends, viewer 403, 501, 503). Status: partial (per-backend field validation remains).
