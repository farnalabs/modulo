---
id: feat-core-audit-viewer-ui
prd: 8.12
delivery-tasks: [task-nv11-audit-viewer-ui]
bdd:
  - backend/tests/features/audit/audit_viewer.feature
  - backend/tests/bdd/features/admin/audit_export.feature
code:
  - frontend/src/views/AdminAuditView.vue
  - backend/src/modulo/api/routes/audit.py
unit-tests:
  - backend/tests/unit/api/test_audit.py
  - backend/tests/unit/api/test_audit_gating.py
  - backend/tests/unit/api/test_audit_bdd.py
  - backend/tests/unit/audit_logger/test_audit_logger.py
depends-on: [feat-core-audit-trail]
status: partial
---

# Audit Viewer UI

Cursor-paginated audit event viewer with filtering, expandable detail rows,
CSV/JSONL export, and chain verification.

## Feature Gating

The audit viewer is split into free and enterprise tiers:

- **Free tier:** read-only recent-events view (`GET /api/v1/admin/audit`,
  max 50 events, no export) and chain verification
  (`GET /api/v1/admin/audit/verify`). Always available to all orgs —
  audit capability must be verifiable during evaluation for regulated teams.
- **Enterprise tier** (requires `audit_viewer` in license key): bulk export
  (`GET /api/v1/admin/audit/export` as CSV/JSONL) and batch-detail
  (`POST /api/v1/admin/audit/batch-detail`). Recording stays free on all
  tiers.

Only the export and batch-detail endpoints are behind
`require_feature("audit_viewer")`.

## Behaviours

### Viewing (free + enterprise)

- [x] Cursor-paginated event listing via `GET /api/v1/admin/audit`
- [x] Events ordered newest-first by default
- [x] Default 50 events per page, configurable via API (max 200)
- [x] Previous / Next page navigation using cursor tokens
- [x] Page indicator: "Page N · M of T total events"
- [x] Expandable row toggles inline detail panel
- [x] Expanded panel shows formatted payload JSON in `<pre>` block
- [x] Expanded panel shows Previous Hash (truncated, mono font)
- [x] Expanded panel shows Event ID (truncated, mono font)
- [x] Expanded panel shows Request ID when present
- [x] Timestamp formatted as locale-aware short date + time
- [x] Actor formatted as `usr_` + first 8 hex chars of user UUID
- [x] Resource type displayed with truncated resource ID
- [x] Missing resource_type renders em-dash
- [x] Event type rendered as coloured badge grouped by category
- [x] Summary column: action noun + resource type + optional name from payload
- [x] Chevron icon rotates on expanded row

### Filtering (free + enterprise)

- [x] Event type dropdown filter grouped by category
- [x] Actor (user_id) text input filter
- [x] From date (date picker) filter
- [x] To date (date picker) filter
- [x] Target type (entity_type) dropdown filter
- [x] Apply Filters button triggers reload from page 1
- [x] Reset button clears all filters

### Chain Verification (free)

- [x] Verify Chain button calls `GET /api/v1/admin/audit/verify`
- [x] Success result: green banner with event count
- [x] Failure result: red banner with error message
- [x] Loading state: button shows "Verifying..." and is disabled
- [x] No enterprise license needed — chain verification is always available

### Export (enterprise-gated)

- [x] CSV export button downloads all events matching current filters
- [x] JSONL export button downloads all events matching current filters
- [x] Export paginates internally (1000 per page, loops until complete)
- [x] Button shows "Exporting..." while in progress
- [x] 402 Payment Required returned when `audit_viewer` not in license

### Edge Cases

- [x] Zero events: empty state with guidance text
- [x] Network error: ErrorAlert with retry button
- [x] Expired or invalid cursor: silently falls back to first page
- [x] Actor user deleted (FK SET NULL): shows "—" for actor
- [x] Large payload_json: rendered as formatted JSON in `<pre>`

### Error Handling

- [x] 401 Unauthenticated returns 401 on all audit endpoints
- [x] 403 Non-admin role returns 403 on all audit endpoints
- [x] 402 Payment Required when audit_viewer not in license (export, batch-detail, list)
- [x] 501 Not Implemented — ProgrammingError caught on all 4 DB-accessing routes
- [x] Empty audit log returns 200 with 0 events (not an error)
- [x] Expired/invalid cursor silently falls back to first page
- [x] Network error shows ErrorAlert with retry button in frontend

## Known Gaps

- No event type vocabulary enforcement (any string accepted)
- Export buttons visible via show-disabled FeatureGate — free tier users see
  disabled buttons that return 402 on click (intentional per design)
- No BDD scenarios for export step definitions wired to live production
  pipeline (mocking-only)
- ~~No website docs page at Website/modulo-website/src/docs/audit-viewer.md~~ (created)
