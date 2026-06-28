---
id: feat-core-audit-trail
prd: 8.12
delivery-tasks: [task-nv0-immutable-audit]
bdd:
  - backend/tests/bdd/features/audit/event_recording.feature
code:
  - backend/src/modulo/core/audit_logger/__init__.py
  - backend/src/modulo/core/audit_logger/append_only.py
  - backend/src/modulo/db/models/audit_event.py
  - backend/src/modulo/api/routes/audit.py
unit-tests:
  - backend/tests/unit/audit_logger/test_audit_logger.py
  - backend/tests/unit/api/test_audit.py
depends-on: []
status: partial
---

# Audit Trail

Immutable SHA-256-linked audit event chain per organisation. Each event
records a timestamp, actor, resource, and payload. UPDATE/DELETE blocked
at both Postgres and ORM levels. V1: viewer UI, export, chain verification.

## Behaviours

### Event Creation
- [ ] Create AuditEvent with organisation_id, event_type, payload_json
- [ ] First event in an org: previous_hash is None, creates AuditChainHead
- [ ] Subsequent events: previous_hash = SHA-256 of prior event
- [ ] Subsequent events: AuditChainHead.event_count incremented
- [ ] SHA-256 hash computed from canonical JSON (sort_keys=True, separators=",:")
- [ ] Hash is deterministic — same inputs produce same hash
- [ ] actor_user_id, resource_type, resource_id, payload_json, request_id all optional
- [ ] payload_json defaults to {} when None
- [ ] Event timestamp set to UTC now

### Append-Only Enforcement (two layers)

**Postgres trigger:**
- [ ] UPDATE on audit_events table rejected by Postgres trigger
- [ ] DELETE on audit_events table rejected by Postgres trigger
- [ ] Error message contains "append-only" or "not permitted"
- [ ] After failed DELETE, event still exists (clean rollback)
- [ ] INSERT and SELECT still work on audit_events

**ORM listener (**``audit_logger/append_only.py``**):**
- [ ] ORM `before_update` raises RuntimeError with "append-only"
- [ ] ORM `before_delete` raises RuntimeError with "append-only"
- [ ] `register_append_only_guard()` is idempotent

### Chain Verification
- [ ] Verifies every event's previous_hash matches recomputed hash
- [ ] Returns valid: True when chain is intact
- [ ] Returns valid: False + first_tampered_id when break found
- [ ] Returns first_gap_index for broken link position
- [ ] Empty chain returns valid: True, total_events=0
- [ ] Validates last hash against AuditChainHead
- [ ] Respects max_events limit (default 10000)

### Event Listing — Cursor Pagination
- [ ] Returns items, total, next_cursor, prev_cursor
- [ ] Filters: event_type, actor_user_id, resource_type, from_date, to_date
- [ ] Invalid cursor UUID silently ignored (falls back to first page)
- [ ] Default limit=50, max 200
- [ ] Events ordered newest-first
- [ ] Returns limit+1 items internally to detect has_more

### Event Export & Batch Detail
- [ ] Paginated export with page/page_size (offset-based)
- [ ] Batch detail accepts list of event IDs, returns full details
- [ ] Invalid UUIDs in batch request silently skipped
- [ ] RLS-scoped: only caller's org events returned

### API Endpoints
- [ ] GET /api/v1/admin/audit — cursor-paginated listing
- [ ] GET /api/v1/admin/audit/verify — chain integrity check
- [ ] GET /api/v1/admin/audit/export — offset-paginated export
- [ ] POST /api/v1/admin/audit/batch-detail — batch event detail
- [ ] All endpoints require auth and are RLS-scoped to caller's org

### Event Types (PRD §8.12 table — 18 documented event types)
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

### Edge Cases
- [ ] Concurrent event creation under same org → serialized by DB transaction (validates chain head consistency)
- [ ] verify_chain with >max_events → only checks first N, reports total_events correctly but may miss break after max_events
- [ ] Export with page beyond available data → empty items, total still accurate
- [ ] List with filter returning zero results → empty items, total=0, no cursors
- [ ] Actor user deleted (SET NULL on FK) → actor_user_id is None, event still valid
- [ ] Large payload_json → stored (JSON column), no explicit size limit
- [ ] Chain head deleted (SET NULL FK) → AuditChainHead.last_event_id is null, chain still verifiable
- [ ] Org migration → events stay in source org (no cross-org visibility, enforced by RLS)

### Error Handling
- [ ] UPDATE/DELETE at Postgres level → database error with "append-only" message
- [ ] UPDATE/DELETE at ORM level → RuntimeError with "append-only" message
- [ ] verify_chain with DB connection failure → exception propagates (no fallback)
- [ ] Invalid cursor in list endpoint → silently ignored, shows first page
- [ ] Invalid UUID in batch-detail → silently skipped
- [ ] Missing event_type → model validation error before DB write (event_type is NOT nullable in DB — String(100), no default, but SQLAlchemy model has no explicit nullable=False)

### Security
- [ ] RLS isolates events per organisation — org A cannot see org B's events ← set_rls_org in every route
- [ ] Authentication required for all endpoints ← AuthenticatedPrincipal dependency
- [ ] Audit chain is cryptographically tamper-evident — altering any event breaks the hash chain
- [ ] POSTGRES trigger + ORM listener = defense-in-depth (two independent layers)
- [ ] Event types are free-form strings — any caller can write any event_type (no enforced vocabulary; namespacing is a V2 concern)
- [ ] payload_json is arbitrary JSON — sensitive data can be embedded (caller's responsibility)

### Backward Compatibility
- [ ] Existing events remain readable after schema changes (JSON column, no typed fields)
- [ ] New event types can be added without migration (event_type is a string, not an enum)
- [ ] Chain verification logic is additive — old events always verifiable by same algorithm
- [ ] AuditChainHead.last_event_id FK uses ON DELETE SET NULL — deleting an event (if trigger removed) doesn't break chain

## Known Gaps
- No event type vocabulary enforcement (any string accepted)
- payload_json has no schema validation (free-form JSON)
- Event recording is free-tier but viewer/export is enterprise-gated (gate not visible in this code — enforced at route level via _require_enterprise in other routes)
- Cryptographic chaining is V2 in PRD but partially implemented (SHA-256 linking exists; reader UI is V1)
- verify_chain limited to 10000 events by default — large orgs may need higher limit or batched verification
- No event retention policy (events accumulate indefinitely)
- No event schema versioning (payload structure could change between event types)
