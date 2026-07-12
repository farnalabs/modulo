---
id: feat-core-system-config
prd: 6
delivery-tasks: []
code:
  - backend/src/modulo/api/routes/admin_system_config.py
  - backend/src/modulo/db/models/system_config.py
  - backend/src/modulo/db/crud/system_config.py
bdd:
  - backend/tests/bdd/features/system_admin/system_admin_config.feature
unit-tests:
  - backend/tests/unit/api/test_admin_system_config.py
  - backend/tests/unit/db/test_system_config.py
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
- [x] Missing DB table returns 501 Not Implemented
- [x] Integrity errors return 409 Conflict
- [x] SQLAlchemyError returns 503 Service Unavailable
- [ ] Access controls beyond system-admin gate
- [ ] Config value schema validation

## Known Gaps

- BDD coverage is incomplete: missing scenarios for DELETE endpoint, 404 on nonexistent key, 501 on missing DB table, and 409 on integrity errors

## QA History

### 2026-07-12 — Round 3 QA

- **Fixed (MAJOR):** Added `delivery-tasks: []` to frontmatter (was missing entirely).
- **Fixed (MAJOR):** Added missing `SQLAlchemyError`→503 handler to all 3 system config routes (`admin_list_config`, `admin_set_config`, `admin_delete_config`). Previously only `IntegrityError`→409 and `ProgrammingError`→501 were caught; other SQL errors (connection failures, deadlocks) propagated as opaque 500.
- **Fixed (MINOR):** Added `SQLAlchemyError` to imports in `admin_system_config.py`.
