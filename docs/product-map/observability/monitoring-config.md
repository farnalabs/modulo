---
id: feat-observability-monitoring-config
prd: 8
code:
  - backend/src/modulo/api/routes/admin_monitor_config.py
bdd: []
unit-tests: []
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
- [ ] Support for additional monitoring backends
- [ ] Runtime validation of backend config schemas
