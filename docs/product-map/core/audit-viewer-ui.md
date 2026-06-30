---
id: feat-core-audit-viewer-ui
prd: 8.12
delivery-tasks: [task-nv11-audit-viewer-ui]
  - backend/tests/bdd/features/audit/event_recording.feature
code:
  - frontend/src/views/AdminAuditView.vue
  - backend/src/modulo/api/routes/audit.py
unit-tests:
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

- [ ] Cursor-paginated event listing via `GET /api/v1/admin/audit`
- [ ] Events ordered newest-first by default
- [ ] Default 50 events per page, configurable via API (max 200)
- [ ] Previous / Next page navigation using cursor tokens
- [ ] Page indicator: "Page N · M of T total events"
- [ ] Expandable row toggles inline detail panel
- [ ] Expanded panel shows formatted payload JSON in `<pre>` block
- [ ] Expanded panel shows Previous Hash (truncated, mono font)
- [ ] Expanded panel shows Event ID (truncated, mono font)
- [ ] Expanded panel shows Request ID when present
- [ ] Timestamp formatted as locale-aware short date + time
- [ ] Actor formatted as `usr_` + first 8 hex chars of user UUID
- [ ] Resource type displayed with truncated resource ID
- [ ] Missing resource_type renders em-dash
- [ ] Event type rendered as coloured badge grouped by category
- [ ] Summary column: action noun + resource type + optional name from payload
- [ ] Chevron icon rotates on expanded row

### Filtering (free + enterprise)

- [ ] Event type dropdown filter grouped by category
- [ ] Actor (user_id) text input filter
- [ ] From date (date picker) filter
- [ ] To date (date picker) filter
- [ ] Target type (entity_type) dropdown filter
- [ ] Apply Filters button triggers reload from page 1
- [ ] Reset button clears all filters

### Chain Verification (free)

- [ ] Verify Chain button calls `GET /api/v1/admin/audit/verify`
- [ ] Success result: green banner with event count
- [ ] Failure result: red banner with error message
- [ ] Loading state: button shows "Verifying..." and is disabled
- [ ] No enterprise license needed — chain verification is always available

### Export (enterprise-gated)

- [ ] CSV export button downloads all events matching current filters
- [ ] JSONL export button downloads all events matching current filters
- [ ] Export paginates internally (1000 per page, loops until complete)
- [ ] Button shows "Exporting..." while in progress
- [ ] 402 Payment Required returned when `audit_viewer` not in license

### Edge Cases

- [ ] Zero events: empty state with guidance text
- [ ] Network error: ErrorAlert with retry button
- [ ] Expired or invalid cursor: silently falls back to first page
- [ ] Actor user deleted (FK SET NULL): shows "—" for actor
- [ ] Large payload_json: rendered as formatted JSON in `<pre>`

## Known Gaps

- No event type vocabulary enforcement (any string accepted)
- Export buttons visible on free tier but return 402 on click (no FeatureGate
  wrapper in the view)
