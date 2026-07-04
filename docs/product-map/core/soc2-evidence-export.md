---
id: feat-core-soc2-evidence-export
prd: 8.12
delivery-tasks: [task-nv11-soc2-evidence-export]
code:
  - backend/src/modulo/api/routes/audit.py
  - backend/src/modulo/core/audit_logger/__init__.py
  - frontend/src/views/AdminAuditView.vue
unit-tests:
  - backend/tests/unit/audit_logger/test_audit_logger.py
  - backend/tests/unit/api/test_audit.py
depends-on: [feat-core-audit-trail, feat-core-audit-viewer-ui]
bdd:
  - backend/tests/bdd/features/admin/audit_export.feature
status: partial
---

# SOC 2 Evidence Export

Paginated JSON export of audit events for SOC 2 compliance evidence. Builds on the core audit trail (immutable SHA-256-linked event chain) to produce downloadable bundles that an external auditor can verify independently. Recording stays free; export is enterprise-gated.

## Behaviours

### Backend Export Endpoint

- [x] `GET /api/v1/admin/audit/export` returns paginated JSON of audit events
- [x] Default page=1, page_size=100 (max 1000)
- [x] Response includes `items`, `total`, `page`, `page_size`
- [x] Events ordered oldest-first (ascending by created_at, then id)
- [x] Each event includes: id, event_type, actor_user_id, resource_type, resource_id, payload_json, request_id, previous_hash, created_at
- [x] RLS-scoped: only caller's org events returned
- [x] Authentication required (AuthenticatedPrincipal dependency)
- [x] Enterprise-gated (requires `audit_viewer` feature flag in license)

### Export Filters

- [x] Filter by event_type (query param)
- [x] Filter by actor_user_id (query param)
- [x] Filter by resource_type/entity_type (query param)
- [x] Filter by from_date (ISO 8601)
- [x] Filter by to_date (ISO 8601)
- [x] Active filters preserved across pagination

### CSV Export (Frontend)

- [x] Export CSV button in AdminAuditView header
- [x] Export respects current active filters
- [x] Fetches all pages via export endpoint (page_size=1000)
- [x] CSV headers: Timestamp, Event Type, Actor ID, Target Type, Target ID, Summary, Request ID, Previous Hash
- [x] CSV cells quoted and comma-separated
- [x] Downloaded file named `audit-log-YYYY-MM-DD.csv`
- [x] Button shows "Exporting..." with disabled state during export
- [x] Export failure surfaces user-facing error message

### Chain Verification Evidence

- [x] Export includes `previous_hash` on every event for chain verification
- [x] Auditor can recompute SHA-256 chain from exported events
- [x] Chain head hash available via `GET /api/v1/admin/audit/verify` endpoint
- [x] Verification endpoint returns valid/total_events/checked_events/first_gap_index/first_tampered_id

### Export Integrity

- [x] Events are immutable — same export at same point-in-time produces same data
- [x] Export is append-only — new events after export don't invalidate prior export

### Error Handling

- [x] ProgrammingError → 501 on all 4 audit routes (list, batch-detail, verify, export)
- [x] SQLAlchemyError → 503 on all 4 routes
- [x] 401/403 on no auth (all endpoints)
- [x] Admin role required (all endpoints)
- [x] 402 when feature not in license (require_feature("audit_viewer") on list, batch-detail, export)
- [x] Invalid page/page_size → 422 validation error
- [x] Malformed UUID in user_id query param → 422
- [x] Cursor decode failure logged in list_audit_events
- [x] Export failure surfaces user-facing error in frontend
- [ ] No non-admin BDD scenario for 403 on export
- [ ] No integration test for export flow end-to-end

### Edge Cases

- [x] Empty org (no events) → total=0, items=[]
- [x] Page beyond available data → empty items, total still accurate
- [x] Large org (100k+ events) → paginated, page_size capped at 1000
- [x] Export with active filters returning zero results → items=[], total=0
- [x] Malformed user_id UUID string → 422
- [x] Malformed cursor JSON → 501 (ProgrammingError) or logged warning

### Resilience & Integration Robustness

- [x] SQLAlchemyError → 503 (connection failure, deadlock) on all 4 routes
- [ ] No retry/backoff on DB connection failure
- [ ] No circuit breaker for export of very large orgs (100k+)

### QA History

#### 2026-07-04 — Cross-cutting QA (improve-architecture index 165)
- Fixed CRITICAL: Added SQLAlchemyError → 503 catch to all 4 audit routes (previously only caught ProgrammingError, allowing connection/deadlock failures to propagate as 500)
- Fixed CRITICAL: Added logging to cursor decode failure in list_audit_events (previously silently swallowed)
- Fixed MAJOR: Added ValueError catch → 422 for malformed UUID in actor_user_id query param on list and export routes
- Fixed MAJOR: Added unit tests for export programming error (501), SQLAlchemyError (503), and non-admin (403) paths
- Updated Error Handling section (12 checkboxes), Edge Cases section (7 checkboxes), Resilience section (3 checkboxes)
- Status: partial (13 known gaps remain)

### Security

- [x] RLS isolates events per organisation
- [x] Authentication required for all endpoints
- [x] Enterprise feature gate prevents unauthorized access
- [x] Export never includes credentials or ciphertexts

## Known Gaps
- No CSV generation on backend — frontend converts JSON to CSV client-side (large exports hit browser memory limits)
- No export bundle checksum/signature for tamper-evident SOC 2 artifacts
- No chain verification data bundled with export (auditor must call /verify separately)
- Export uses offset-based pagination which may skip/duplicate events if new events created during export
- No BDD feature files for audit export flow
- No unit tests for the `/verify` endpoint ProgrammingError path (not covered)
- No integration test verifying full export flow (API → DB → paginated response)
- No CLI export command for offline/automated evidence collection (e.g., monthly audit bundle)
- No retention-aware export — deleted/expired events are invisible to export (no gap marking)
- No event type vocabulary enforcement — export includes whatever event_type strings callers wrote
- No auth_provider check for 402 vs 401 distinction on verify endpoint (verify is intentionally community)
- max_events=10000 limit on verify_chain may silently cap large orgs
