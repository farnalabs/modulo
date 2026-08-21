---
prd: 10.4
id: feat-admin-housekeeping
delivery-tasks: [task-housekeeping-admin]
bdd:
  - backend/tests/bdd/features/admin/housekeeping.feature
unit-tests:
  - backend/tests/unit/api/test_admin_housekeeping.py
  - backend/tests/unit/core/test_housekeeping.py
  - backend/tests/unit/mcp/test_mcp_config_tools.py
code:
  - backend/src/modulo/core/housekeeping.py
  - backend/src/modulo/api/routes/admin_housekeeping.py
  - backend/src/modulo/api/mcp_server.py
  - frontend/src/views/AdminHousekeepingView.vue
depends-on: []
status: partial
---

# Housekeeping Admin Page

Admin page for scanning and cleaning up unused/orphaned resources.

## Behaviours

### Scan
- [x] GET /api/v1/admin/housekeeping returns 16 categories of cleanup candidates
- [x] Results are org-scoped (RLS enforced via `set_rls_org`)
- [x] Only org admins can access the endpoint (403 for non-admins)
- [x] Each candidate has id, name, detail, created_at
- [x] Each candidate includes an `entity_type` matching the cleanup API contract
- [x] Scanners are isolated — one failing scanner returns an empty category, others still run
- [x] Unknown entity types in cleanup are reported as errors without aborting the batch

### Cleanup
- [x] POST /api/v1/admin/housekeeping/cleanup deletes selected candidates
- [x] Deletions are grouped by entity type with savepoints
- [x] FK violations on one type do not block other types

### MCP
- [x] list_housekeeping tool returns scan results (runner scope)
- [x] perform_housekeeping tool deletes selected items (operator scope)

### UI
- [x] Housekeeping page accessible under the admin sidebar group
- [x] Select-all with indeterminate state
- [x] Category-level checkboxes with indeterminate state
- [x] Individual item checkboxes
- [x] Confirmation dialog with type-summary breakdown
- [x] Empty state when no candidates found
- [x] Error state with retry button

## Error Handling

- [x] Missing credentials → 401 via HTTPBearer dependency
- [x] Non-admin role → 403 "Admin role required"
- [x] `ProgrammingError` (missing migration) → 501 Not Implemented on scan and cleanup
- [x] `SQLAlchemyError` (connection loss) → 503 Service Unavailable on scan and cleanup
- [x] Unexpected exception → 500 Internal Server Error on scan and cleanup
- [x] Cleanup `IntegrityError` (FK violation) → per-item error entry, batch continues
- [x] Unknown `entity_type` in cleanup request → per-item error entry, batch continues
- [x] MCP tools catch `MCPAuthorizationError` → `insufficient_scope`, `ProgrammingError` → `migration_required`

## Known Gaps
- (Resolved) MCP tools (`list_housekeeping`, `perform_housekeeping`) have no dedicated unit tests — covered by `backend/tests/unit/mcp/test_mcp_config_tools.py`
- No e2e/Playwright coverage for the housekeeping UI (checkbox interactions, confirm dialog)
- Cleanup is performed by individual `DELETE` calls per item rather than a bulk operation
- The scan runs 16 sequential category queries per request — no parallelism or caching

## QA History

### 2026-08-15 — improve-architecture (product-map walk)

- **Fixed (MINOR):** `webhook_dedup` candidates reported their `expires_at` timestamp as the `created_at` display value in `_scan_expired_webhook_dedups` (`core/housekeeping.py`) — a semantic mismatch that made expired dedup hashes appear in the housekeeping UI as if created at expiry time. The `WebhookDedupHash` entity carries a real `created_at` (via `TimestampMixin`/`OrgScoped`), so the candidate now reports the row's creation time, matching every other scanner (`_scan_stale_api_keys`, `_scan_unused_environment_profiles`, etc.).
- Added unit coverage in `backend/tests/unit/core/test_housekeeping_scanners.py` (`TestExpiredWebhookDedups`): `test_flags_only_expired_hashes_in_org` now asserts the org-scoped filter plus candidate detail, and new `test_reports_creation_time_not_expiry` proves a hash whose creation time (30 days ago) differs from its expiry (1 hour ago) is reported with its creation time, not its expiry.
- Verification: 51/51 housekeeping unit tests (scanners + core + api) + 6/6 housekeeping BDD scenarios pass, ruff check + format clean, mypy --strict clean on `core/housekeeping.py`. Status: partial (e2e/Playwright UI coverage, per-item cleanup DELETE, 16 sequential scan queries remain).
