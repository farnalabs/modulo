---
id: feat-core-system-config
prd: 6
delivery-tasks: []
code:
  - backend/src/modulo/api/routes/admin_system_config.py
  - backend/src/modulo/api/routes/admin_dev_mode.py
  - backend/src/modulo/db/models/system_config.py
  - backend/src/modulo/db/crud/system_config.py
bdd:
  - backend/tests/bdd/features/system_admin/system_admin_config.feature
unit-tests:
  - backend/tests/unit/api/test_admin_system_config.py
  - backend/tests/unit/db/test_system_config.py
  - backend/tests/unit/mcp/test_mcp_config_tools.py
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
- [x] Concurrent PUT of same key is safe — `set_config` reads the existing row with `SELECT ... FOR UPDATE`, so concurrent writes serialize and last-write-wins is the defined upsert semantics (no torn writes); verified by `test_set_config_locks_existing_row`
- [ ] Access controls beyond system-admin gate
- [ ] Config value schema validation

## Error Handling

- [x] Missing DB table returns 501 Not Implemented
- [x] Integrity errors return 409 Conflict
- [x] SQLAlchemyError returns 503 Service Unavailable
- [x] Delete nonexistent key returns 404
- [x] Catch-all Exception→500 with logger.exception
- [ ] Config value size limit not enforced — unbounded JSON values could cause DB issues

## Edge Cases

- [x] Empty config key returns 422
- [x] GET after DELETE returns 404
- [ ] Config key with special characters (dots, spaces) — stored as-is, no sanitisation
- [ ] JSON config values with circular references — stored as serialised string only

## Security

- [x] System admin role required for all operations
- [x] Config values are org-independent (deployment-wide scope)
- [x] Sensitive config values are masked in the list response — `admin_list_config` masks string values whose key matches the shared `is_sensitive_key` patterns (token/secret/api_key/password/key/credential/...), consistent with runtime-config; verified by `TestSensitiveKeyValueIsMasked` / `TestNonStringSensitiveValueNotMasked`
- [ ] No audit logging for config changes

## Known Gaps

- **No audit logging for config changes** — `set_config`/`delete_config` do not write an AuditEvent; changes are recoverable only from the table itself.
- **No schema validation or size limit on values** — any JSON value is accepted, and values are unbounded (a very large value could stress the `system_config.value` JSON column).
- **Keys are stored verbatim** — special characters (dots, spaces) are not sanitised or rejected.
- **Circular-reference values** cannot be stored — the JSON column serialises at the driver boundary, so a self-referencing Python object fails at write time (no dedicated validation/error message).
- **Access controls are limited to the system-admin gate** — no finer-grained permission model beyond `system.config.manage`.

## QA History

### 2026-07-12 — Round 3 QA

- **Fixed (MAJOR):** Added `delivery-tasks: []` to frontmatter (was missing entirely).
- **Fixed (MAJOR):** Added missing `SQLAlchemyError`→503 handler to all 3 system config routes (`admin_list_config`, `admin_set_config`, `admin_delete_config`). Previously only `IntegrityError`→409 and `ProgrammingError`→501 were caught; other SQL errors (connection failures, deadlocks) propagated as opaque 500.
- **Fixed (MINOR):** Added `SQLAlchemyError` to imports in `admin_system_config.py`.

### 2026-08-15 — distribute (partial→covered sweep)

- **Implemented (sensitive-value masking):** `admin_list_config` now masks string values whose key matches the shared `is_sensitive_key` patterns (token/secret/api_key/password/passwd/key/credential/database_url/encryption/signing/private), matching the runtime-config endpoint's behaviour — PRD §6.2 "never exposes secrets". Non-string values are returned verbatim. 2 new tests (`test_sensitive_key_value_is_masked`, `test_non_string_sensitive_value_not_masked`) + the existing `test_returns_entries` updated to use a non-sensitive key.
- **Marked [x]:** Concurrent-PUT safety. `set_config` locks the existing row (`SELECT ... FOR UPDATE`) before upserting, so concurrent writes to the same key serialize and last-write-wins is the defined semantics; verified by the new `test_set_config_locks_existing_row` in `test_system_config.py`.
- **Confirmed genuine gaps** (left unchecked): beyond-system-admin access controls, config value schema validation, size limits, key sanitisation, circular-reference handling, and audit logging.
