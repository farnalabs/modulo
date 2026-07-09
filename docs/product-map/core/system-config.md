---
id: feat-core-system-config
prd: 6
code:
  - backend/src/modulo/api/routes/admin_system_config.py
bdd: []
unit-tests: []
depends-on: []
status: partial
---

# System Configuration

Deployment-wide key-value configuration management via the `SystemConfig` table. Provides CRUD operations for arbitrary JSON configuration values scoped to the entire deployment.

## Behaviours

- [x] GET list all system config entries
- [x] PUT set a config entry by key
- [x] DELETE remove a config entry by key
- [x] 404 on delete of nonexistent key
- [x] System admin role required
- [x] Missing DB table returns 503 Service Unavailable
- [x] Integrity errors return 409 Conflict
- [ ] Access controls beyond system-admin gate
- [ ] Config value schema validation
