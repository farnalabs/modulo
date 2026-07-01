---
id: feat-core-audit-trail
prd: 8.12
delivery-tasks: [task-nv0-immutable-audit]
bdd:
  - backend/tests/features/audit/event_recording.feature
  - backend/tests/features/audit/audit_viewer.feature
code:
  - backend/src/modulo/core/audit_logger/__init__.py
  - backend/src/modulo/core/audit_logger/append_only.py
  - backend/src/modulo/db/models/audit_event.py
  - backend/src/modulo/api/routes/audit.py
  - backend/src/modulo/api/routes/admin_rotation.py
  - backend/src/modulo/api/routes/admin.py
  - backend/src/modulo/api/routes/pipelines.py
  - backend/src/modulo/core/hitl_manager/__init__.py
  - backend/src/modulo/core/hitl_manager/expiry_job.py
  - backend/src/modulo/core/pipeline_engine/recovery.py
unit-tests:
  - backend/tests/unit/audit_logger/test_audit_logger.py
  - backend/tests/unit/audit_logger/test_append_only.py
  - backend/tests/integration/test_audit_append_only.py

depends-on: []
status: partial
---

# Audit Trail

Immutable SHA-256-linked audit event chain per organisation. Each event records a timestamp, actor, resource, and payload. UPDATE/DELETE blocked at both Postgres and ORM levels. V1: viewer UI, export, chain verification.

## Behaviours

### Event Creation

- [x] Create AuditEvent with organisation_id, event_type, payload_json
- [x] First event in an org: previous_hash is None, creates AuditChainHead
- [x] Subsequent events: previous_hash = SHA-256 of prior event
- [x] Subsequent events: AuditChainHead.event_count incremented
- [x] SHA-256 hash computed from canonical JSON (sort_keys=True, separators=",:")
- [x] Hash is deterministic — same inputs produce same hash
- [x] actor_user_id, resource_type, resource_id, payload_json, request_id all optional
- [x] payload_json defaults to {} when None
- [x] Event timestamp set to UTC now

### Append-Only Enforcement (two layers) **Postgres trigger:**

- [x] UPDATE on audit_events table rejected by Postgres trigger
- [x] DELETE on audit_events table rejected by Postgres trigger
- [x] Error message contains "append-only" or "not permitted"
- [x] After failed DELETE, event still exists (clean rollback)
- [x] INSERT and SELECT still work on audit_events **ORM listener (**``audit_logger/append_only.py``**):**
- [x] ORM `before_update` raises RuntimeError with "append-only"
- [x] ORM `before_delete` raises RuntimeError with "append-only"
- [x] `register_append_only_guard()` is idempotent

### Chain Verification

- [x] Verifies every event's previous_hash matches recomputed hash
- [x] Returns valid: True when chain is intact
- [x] Returns valid: False + first_tampered_id when break found
- [x] Returns first_gap_index for broken link position
- [x] Empty chain returns valid: True, total_events=0
- [x] Validates last hash against AuditChainHead
- [x] Respects max_events limit (default 10000)

### Event Listing — Cursor Pagination

- [x] Returns items, total, next_cursor, prev_cursor
- [x] Filters: event_type, actor_user_id, resource_type, from_date, to_date
- [x] Invalid cursor UUID silently ignored (falls back to first page)
- [x] Default limit=50, max 200
- [x] Events ordered newest-first
- [x] Returns limit+1 items internally to detect has_more

### Event Export & Batch Detail

- [x] Paginated export with page/page_size (offset-based)
- [x] Batch detail accepts list of event IDs, returns full details
- [x] Invalid UUIDs in batch request silently skipped
- [x] RLS-scoped: only caller's org events returned

### API Endpoints

- [x] GET /api/v1/admin/audit — cursor-paginated listing
- [x] GET /api/v1/admin/audit/verify — chain integrity check
- [x] GET /api/v1/admin/audit/export — offset-paginated export
- [x] POST /api/v1/admin/audit/batch-detail — batch event detail
- [x] All endpoints require auth and are RLS-scoped to caller's org

### Event Types (PRD 8.12 table — 18 documented event types)

- [ ] `run_started` — pipeline_id, snapshot_id, trigger_type, user_id, input hash
- [ ] `hitl_claimed` — run_id, gate_id, user_id
- [ ] `hitl_approved` — run_id, gate_id, user_id
- [ ] `hitl_rejected` — run_id, gate_id, user_id, reason, reject_target
- [ ] `pipeline_changed` — pipeline_id, user_id, change summary
- [ ] `agent_prompt_changed` — agent_id, user_id, old_version, new_version
- [ ] `user_permission_changed` — target_user_id, changed_by, old_role, new_role
- [ ] `connector_credentials_updated` — connector_id, user_id
- [ ] `model_backend_credentials_updated` — backend_id, user_id
- [ ] `schema_version_deprecated` — schema_id, version, user_id
- [ ] `api_key_created` — key_id (not raw key), user_id
- [ ] `api_key_revoked` — key_id, revoked_by
- [ ] `auth_event` — type (login/logout/failed), user_id, ip
- [ ] `team_created` / `team_renamed` / `team_deleted` — team id/name, user
- [ ] `team_member_added` / `team_member_removed` / `team_member_role_changed` — team_id, user_id, role
- [ ] `resource_team_ownership_changed` — resource_type, resource_id, old/new team_id
- [ ] `team_membership_revoked` — team_id, user_id, revoked_by
- [ ] `hitl_output_delivered` — V1 event, data: run_id, gate_id, user_id, output hash

### Implemented Event Types (actually dispatched in code)

- [x] `pipeline.autonomy_level_changed` — pipeline_id, user_id, old_autonomy, new_autonomy
- [x] `fernet_key_rotation_started` — key_version, user_id
- [x] `fernet_key_rotation_completed` — key_version, new_key_count, user_id
- [x] `org_deletion_requested` — org_id, user_id, scheduled_date
- [x] `run_purge` — run_count, user_id
- [x] `hitl.output_modified` — run_id, gate_id, user_id
- [x] `hitl.output_delivered` — run_id, gate_id, user_id, output_hash
- [x] `hitl.output_delivery_failed` — run_id, gate_id, error
- [x] `hitl.manual_delivery` — run_id, gate_id, user_id
- [x] `hitl.claim_expired` — run_id, gate_id, claim_token
- [x] `node.recovery` — run_id, node_id, recovery_strategy

### Edge Cases

- [x] Concurrent event creation under same org → serialized by DB transaction (validates chain head consistency)
- [x] verify_chain with >max_events → only checks first N, reports total_events correctly but may miss break after max_events
- [x] Export with page beyond available data → empty items, total still accurate
- [x] List with filter returning zero results → empty items, total=0, no cursors
- [x] Actor user deleted (SET NULL on FK) → actor_user_id is None, event still valid
- [x] Large payload_json → stored (JSON column), no explicit size limit
- [x] Chain head deleted (SET NULL FK) → AuditChainHead.last_event_id is null, chain still verifiable
- [x] Org migration → events stay in source org (no cross-org visibility, enforced by RLS)

### Error Handling

- [x] UPDATE/DELETE at Postgres level → database error with "append-only" message
- [x] UPDATE/DELETE at ORM level → RuntimeError with "append-only" message
- [x] verify_chain with DB connection failure → exception propagates (no fallback)
- [x] Invalid cursor in list endpoint → silently ignored, shows first page
- [x] Invalid UUID in batch-detail → silently skipped
- [x] Missing event_type → model validation error before DB write (event_type is NOT nullable in DB — String(100), no default, but SQLAlchemy model has no explicit nullable=False)

### Security

- [x] RLS isolates events per organisation — org A cannot see org B's events ← set_rls_org in every route
- [x] Authentication required for all endpoints ← AuthenticatedPrincipal dependency
- [x] Audit chain is cryptographically tamper-evident — altering any event breaks the hash chain
- [x] POSTGRES trigger + ORM listener = defense-in-depth (two independent layers)
- [x] Event types are free-form strings — any caller can write any event_type (no enforced vocabulary; namespacing is a V2 concern)
- [x] payload_json is arbitrary JSON — sensitive data can be embedded (caller's responsibility)

### Backward Compatibility

- [x] Existing events remain readable after schema changes (JSON column, no typed fields)
- [x] New event types can be added without migration (event_type is a string, not an enum)
- [x] Chain verification logic is additive — old events always verifiable by same algorithm
- [x] AuditChainHead.last_event_id FK uses ON DELETE SET NULL — deleting an event (if trigger removed) doesn't break chain

## Known Gaps
- No event type vocabulary enforcement (any string accepted)
- payload_json has no schema validation (free-form JSON)
- Event recording is free-tier; read-only event listing and chain verification are also free (no gate). Bulk export and batch-detail are enterprise-gated via `require_feature("audit_viewer")` on the route.
- Cryptographic chaining is V2 in PRD but partially implemented (SHA-256 linking exists; reader UI is V1)
- verify_chain limited to 10000 events by default — large orgs may need higher limit or batched verification
- No event retention policy (events accumulate indefinitely)
- No event schema versioning (payload structure could change between event types)
- **PRD-vs-implementation divergence**: all 18 PRD-specified event types (`run_started`, `hitl_approved`, `team_created`, etc.) are NOT dispatched. Production code uses 11 different dot-notation event types (`pipeline.autonomy_level_changed`, `hitl.output_delivered`, etc.) with no overlap to the PRD table. The naming convention, granularity, and payload structure differ entirely.
- **No `run_started` event**: pipeline runs start without an audit event. The `run_started` PRD event is not dispatched anywhere.
- **No `hitl_claimed`/`hitl_approved`/`hitl_rejected` events**: HITL lifecycle decisions are not recorded in the audit trail. HITL-related events in production are limited to output delivery (`hitl.output_delivered`, `hitl.output_delivery_failed`, `hitl.output_modified`, `hitl.manual_delivery`) and claim expiry (`hitl.claim_expired`).
- **No team CRUD audit events**: team creation, rename, deletion, membership changes, and role changes are not audited.
- **No permission change audit**: `user_permission_changed` event not dispatched.
- **No API key audit**: `api_key_created`/`api_key_revoked` not dispatched.
- **No auth event audit**: login, logout, and failed auth attempts not recorded.
- **BDD feature file uses wrong event types**: `event_recording.feature` references `pipeline.created`, `pipeline.deleted`, `run.created`, `hitl.approved` — none of which match either the PRD table or the actual dispatched event types.
