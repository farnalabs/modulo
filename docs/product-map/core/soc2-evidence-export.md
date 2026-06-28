---
id: feat-core-soc2-evidence-export
prd: 8.12
delivery-tasks: [task-nv11-soc2-evidence-export]
bdd:
code:
  - backend/src/modulo/api/routes/audit.py
  - backend/src/modulo/core/audit_logger/__init__.py
  - frontend/src/views/AdminAuditView.vue
unit-tests:
  - backend/tests/unit/audit_logger/test_audit_logger.py
  - backend/tests/unit/api/test_audit.py
depends-on: [feat-core-audit-trail, feat-core-audit-viewer-ui]
status: partial

---

# SOC 2 Evidence Export

Paginated JSON export of audit events for SOC 2 compliance evidence. Builds on the core audit trail (immutable SHA-256-linked event chain) to produce downloadable bundles that an external auditor can verify independently.

Recording stays free; export is enterprise-gated.

## Behaviours

### Backend Export Endpoint
- [ ] `GET /api/v1/admin/audit/export` returns paginated JSON of audit events
- [ ] Default page=1, page_size=100 (max 1000)
- [ ] Response includes `items`, `total`, `page`, `page_size`
- [ ] Events ordered oldest-first (ascending by created_at, then id)
- [ ] Each event includes: id, event_type, actor_user_id, resource_type, resource_id, payload_json, request_id, previous_hash, created_at
- [ ] RLS-scoped: only caller's org events returned
- [ ] Authentication required (AuthenticatedPrincipal dependency)
- [ ] Enterprise-gated (requires `audit_viewer` feature flag in license)

### Export Filters
- [ ] Filter by event_type (query param)
- [ ] Filter by actor_user_id (query param)
- [ ] Filter by resource_type/entity_type (query param)
- [ ] Filter by from_date (ISO 8601)
- [ ] Filter by to_date (ISO 8601)
- [ ] Active filters preserved across pagination

### CSV Export (Frontend)
- [ ] Export CSV button in AdminAuditView header
- [ ] Export respects current active filters
- [ ] Fetches all pages via export endpoint (page_size=1000)
- [ ] CSV headers: Timestamp, Event Type, Actor ID, Target Type, Target ID, Summary, Request ID, Previous Hash
- [ ] CSV cells quoted and comma-separated
- [ ] Downloaded file named `audit-log-YYYY-MM-DD.csv`
- [ ] Button shows "Exporting..." with disabled state during export
- [ ] Export failure surfaces user-facing error message

### Chain Verification Evidence
- [ ] Export includes `previous_hash` on every event for chain verification
- [ ] Auditor can recompute SHA-256 chain from exported events
- [ ] Chain head hash available via `GET /api/v1/admin/audit/verify` endpoint
- [ ] Verification endpoint returns valid/total_events/checked_events/first_gap_index/first_tampered_id

### Export Integrity
- [ ] Events are immutable — same export at same point-in-time produces same data
- [ ] Export is append-only — new events after export don't invalidate prior export

### States
- [ ] Empty org (no events) → total=0, items=[]
- [ ] Page beyond available data → empty items, total still accurate
- [ ] Large org (100k+ events) → paginated, page_size capped at 1000
- [ ] Export with active filters returning zero results → items=[], total=0

### Error Handling
- [ ] Invalid page/page_size → 422 validation error
- [ ] No auth → 401 Unauthorized
- [ ] No enterprise license → 402 Payment Required (when gating implemented)
- [ ] DB connection failure → exception propagates (no fallback)
- [ ] Export failure in frontend → error message shown, table view unaffected

### Security
- [ ] RLS isolates events per organisation
- [ ] Authentication required for all endpoints
- [ ] Enterprise feature gate prevents unauthorized access
- [ ] Export never includes credentials or ciphertexts

## Known Gaps
- No date-range filter parameters on the `/export` endpoint (filters exist on list endpoint but not on export)
- No CSV generation on backend — frontend converts JSON to CSV client-side (large exports hit browser memory limits)
- No export bundle checksum/signature for tamper-evident SOC 2 artifacts
- No chain verification data bundled with export (auditor must call /verify separately)
- Export uses offset-based pagination which may skip/duplicate events if new events created during export
- Enterprise gating (`require_feature('audit_viewer')`) not yet applied to audit route in current codebase
- No BDD feature files for audit export flow
- No unit tests for the `/export` endpoint specifically (covered by general audit route tests)
- No integration test verifying full export flow (API → DB → paginated response)
- No CLI export command for offline/automated evidence collection (e.g., monthly audit bundle)
- No retention-aware export — deleted/expired events are invisible to export (no gap marking)
- No event type vocabulary enforcement — export includes whatever event_type strings callers wrote
