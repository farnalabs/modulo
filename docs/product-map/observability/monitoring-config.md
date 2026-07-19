---
id: feat-observability-monitoring-config
prd: 8.25.1
code:
  - backend/src/modulo/api/routes/admin_monitor_config.py
  - frontend/src/views/SettingsMonitorConfigView.vue
  - frontend/src/monitor/
  - frontend/src/manifest.yaml
  - frontend/src/router/index.ts
bdd: []
unit-tests:
  - frontend/src/__tests__/monitor-config.spec.ts
depends-on: []
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
- [ ] Support for additional monitoring backends

## Known Gaps

- No backend unit tests — the route file `admin_monitor_config.py` has zero test coverage
- No BDD feature files for monitoring config — no Gherkin scenarios cover the GET/PUT endpoints
- The existing frontend test (`frontend/src/__tests__/monitor-config.spec.ts`) tests only the legacy `config.ts` loader, not the API integration or the SettingsMonitorConfigView component
- "Runtime validation of backend config schemas" — per-backend field validation (e.g. that Sentry's DSN is required when Sentry is enabled) is not implemented. The PUT endpoint validates backend names are from the known set but does not validate per-backend field schemas.
