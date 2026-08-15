---
id: feat-core-audit-trail
prd: 8.12
delivery-tasks: [task-nv0-immutable-audit]
bdd:
  - backend/tests/bdd/features/audit/event_recording.feature
  - backend/tests/bdd/features/audit/audit_viewer.feature
code:
  - backend/src/modulo/core/audit_logger/__init__.py
  - backend/src/modulo/core/audit_logger/append_only.py
  - backend/src/modulo/db/models/audit_event.py
  - backend/src/modulo/api/routes/audit.py
  - backend/src/modulo/api/routes/api_keys.py
  - backend/src/modulo/api/routes/admin_rotation.py
  - backend/src/modulo/api/routes/admin.py
  - backend/src/modulo/api/routes/pipelines.py
  - backend/src/modulo/core/hitl_manager/__init__.py
  - backend/src/modulo/core/hitl_manager/expiry_job.py
  - backend/src/modulo/core/pipeline_engine/executor.py
  - backend/src/modulo/core/pipeline_engine/recovery.py
  - backend/src/modulo/api/routes/teams.py
unit-tests:
  - backend/tests/unit/audit_logger/test_audit_logger.py
  - backend/tests/unit/audit_logger/test_append_only.py
  - backend/tests/integration/test_audit_append_only.py
  - backend/tests/unit/pipeline_engine/test_executor.py
  - backend/tests/unit/hitl_manager/test_hitl_manager.py
  - backend/tests/unit/api/test_api_keys_endpoint.py
  - backend/tests/unit/api/test_teams.py

depends-on: [feat-core-db-abstraction-core]
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

- [x] `run_started` — pipeline_id, snapshot_id, trigger_type, user_id, input hash
- [x] `hitl_claimed` — run_id, gate_id, user_id
- [ ] `hitl_approved` — run_id, gate_id, user_id
- [ ] `hitl_rejected` — run_id, gate_id, user_id, reason, reject_target
- [ ] `pipeline_changed` — pipeline_id, user_id, change summary
- [ ] `agent_prompt_changed` — agent_id, user_id, old_version, new_version
- [ ] `user_permission_changed` — target_user_id, changed_by, old_role, new_role
- [ ] `connector_credentials_updated` — connector_id, user_id
- [ ] `model_backend_credentials_updated` — backend_id, user_id
- [ ] `schema_version_deprecated` — schema_id, version, user_id
- [x] `api_key_created` — key_id (not raw key), user_id
- [x] `api_key_revoked` — key_id, revoked_by
- [ ] `auth_event` — type (login/logout/failed), user_id, ip
- [x] `team_created` — team id/name, user (dispatched as `team_created`)
- [x] `team_renamed` / `team_deleted` — team id/name, user (deletion dispatched as `team_deleted`; rename dispatched as `team_updated` — PRD-name divergence remains)
- [x] `team_member_added` — team_id, user_id, role
- [x] `team_member_removed` — team_id, user_id, role (role the removed member held)
- [x] `team_member_role_changed` — team_id, user_id, old_role, new_role
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
- [x] `hitl.output_rejected` — run_id, gate_id, user_id, reason, reject_target
- [x] `hitl.claim_expired` — run_id, gate_id, claim_token
- [x] `node.recovery` — run_id, node_id, recovery_strategy
- [x] `pipeline.node.convert_to_agent` — pipeline_id, node_id, agent_id
- [x] `pipeline.node.revert_to_manual` — pipeline_id, node_id, snapshot_id
- [x] `schema_inference_completed` — connector_name, resource, sample_count, model_backend_id
- [x] `schema_migration_completed` — from_schema_id, to_schema_id, dry_run, field/type/rename change counts
- [x] `schema_migration_planned` — field_additions, field_removals, type_changes, renames (inline plan preview)
- [x] `sso_provider.created` — provider_name
- [x] `sso_provider.deleted` — provider_name
- [x] `sso_provider.toggled` — provider_name
- [x] `sso_provider.updated` — provider_name
- [x] `team_deleted` — team_id
- [x] `run_started` — pipeline_id (dispatched from `PipelineExecutor._check_capacity` at the pending→running claim transition; fires once per run — the resume() path is excluded)
- [x] `hitl_claimed` — run_id, gate_id, user_id, team_id, expiry_minutes (dispatched from `HITLManager.claim`)
- [x] `api_key_created` — name, role, team_id (dispatched from the api-keys create route, key id as `resource_id`)
- [x] `api_key_revoked` — revoked_by (dispatched from the api-keys revoke route, key id as `resource_id`)
- [x] `team_member_added` — team_id, user_id, role (dispatched from `teams.add_member_endpoint`, membership id as `resource_id`)
- [x] `team_member_removed` — team_id, user_id, role (dispatched from `teams.remove_member_endpoint`, membership id as `resource_id`)
- [x] `team_member_role_changed` — team_id, user_id, old_role, new_role (dispatched from `teams.change_member_role_endpoint`, membership id as `resource_id`)

### Edge Cases

- [x] Concurrent event creation under same org → serialized by DB transaction (validates chain head consistency)
- [x] verify_chain with >max_events → only checks first N, reports total_events correctly but may miss break after max_events
- [x] Export with page beyond available data → empty items, total still accurate
- [x] List with filter returning zero results → empty items, total=0, no cursors
- [x] Actor user deleted (SET NULL on FK) → actor_user_id is None, event still valid
- [x] Large payload_json → stored (JSON column), no explicit size limit
- [x] Chain head deleted (SET NULL FK) → AuditChainHead.last_event_id is null, chain still verifiable
- [x] Org migration → events stay in source org (no cross-org visibility, enforced by RLS)
- [x] Rotation started audit failure does not deadlock `_rotation_in_progress` flag

### Error Handling

- [x] UPDATE/DELETE at Postgres level → database error with "append-only" message
- [x] UPDATE/DELETE at ORM level → RuntimeError with "append-only" message
- [x] verify_chain with DB connection failure → exception propagates (no fallback)
- [x] Invalid cursor in list endpoint → silently ignored, shows first page
- [x] Invalid UUID in batch-detail → silently skipped
- [x] Missing event_type → model validation error before DB write (event_type is NOT nullable in DB — String(100), no default, but SQLAlchemy model has no explicit nullable=False)
- [x] `hitl_manager/expiry_job.py` — `hitl.claim_expired` audit dispatch wrapped in try/except
- [x] `pipeline_engine/recovery.py` — `node.recovery` audit dispatch wrapped in try/except
- [x] `db/crud/sso_provider.py` — all 4 SSO provider audit dispatches wrapped in try/except (failure logged, not re-raised)
- [x] `api/routes/admin.py` — team deletion audit dispatch wrapped in try/except (ProgrammingError catch only)
- [x] `api/routes/admin.py` — `run_purge` audit dispatch moved inside session transaction (now atomic with purge)
- [x] `api/routes/admin_rotation.py` — `fernet_key_rotation_started` flag only set AFTER successful audit write

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
- Event recording is free-tier; read-only event listing and chain verification are also free (no gate). Bulk export and batch-detail are team-gated via `require_feature("audit_viewer")` on the route.
- Cryptographic chaining is V2 in PRD but partially implemented (SHA-256 linking exists; reader UI is V1)
- verify_chain limited to 10000 events by default — large orgs may need higher limit or batched verification
- No event retention policy (events accumulate indefinitely)
- No event schema versioning (payload structure could change between event types)
- **PRD-vs-implementation divergence**: 14 of the 18 PRD-specified event types (`hitl_approved`, `hitl_rejected`, `team_created`, etc.) are NOT dispatched. Production code uses 19 different dot-notation event types (`pipeline.autonomy_level_changed`, `hitl.output_delivered`, etc.) with no overlap to the PRD table. The naming convention, granularity, and payload structure differ entirely. **[RESOLVED 2026-08-15]** — 4 PRD event types are now dispatched under their PRD names: `run_started`, `hitl_claimed`, `api_key_created`, `api_key_revoked`.
- ~~**No `run_started` event**: pipeline runs start without an audit event. The `run_started` PRD event is not dispatched anywhere.~~ **[RESOLVED 2026-08-15]** — dispatched from `PipelineExecutor._check_capacity` at the pending→running claim transition (fires once per run; resumes excluded).
- ~~**No `hitl_claimed` audit event**: Claim acquisition is not recorded in the audit trail (`hitl.claim_expired` is dispatched, but the initial claim itself is not).~~ **[RESOLVED 2026-08-15]** — dispatched from `HITLManager.claim()` with run_id/gate_id/user_id/team_id/expiry_minutes.
- ~~**No team CRUD audit events**: team creation, rename, deletion, membership changes, and role changes are not audited.~~ **[RESOLVED]** — team create/update/delete were already dispatched (`team_created`/`team_updated`/`team_deleted`); team membership add/remove/role-change are now dispatched too (`team_member_added`/`team_member_removed`/`team_member_role_changed` from the teams membership routes). Remaining divergence: the rename event fires as `team_updated` rather than PRD's `team_renamed`.
- **No permission change audit**: `user_permission_changed` event not dispatched.
- ~~**No API key audit**: `api_key_created`/`api_key_revoked` not dispatched.~~ **[RESOLVED 2026-08-15]** — dispatched from the api-keys create/revoke routes (key id as `resource_id`, raw key never logged).
- **No auth event audit**: login, logout, and failed auth attempts not recorded.
- **BDD placeholder steps**: Scenarios 'Audit events have cryptographic chaining', 'Claim expiry is audited', 'HITL output delivery is audited', 'Org deletion request is audited' have @then step implementations at `backend/tests/bdd/steps/test_alpha_audit.py` that were placeholders (pass). Not a functional bug (the scenarios verify existence at code level) but serve as regression safety net. **[Now fixed: assertions added for event_type matching and gate metadata.]**
- **BDD feature file uses wrong event types** (resolved): `event_recording.feature` previously referenced `pipeline.created`, `pipeline.deleted`, `run.created`, `hitl.approved` — none of which match either the PRD table or the actual dispatched event types. **[RESOLVED]** — Feature file now uses correct event types matching actual dispatched events (`pipeline.autonomy_level_changed`, `hitl.output_delivered`, `hitl.claim_expired`, `org_deletion_requested`).
- **`hitl.output_rejected` was already dispatched** (product map was stale — claimed no audit event). However, `reject_gate` route was missing `actor_id=principal.account_id`, so the audit event had `actor_id=None`. Fixed in index 246 cross-cutting QA.
- **No API key audit events**: `api_keys.py` has zero audit dispatches — PRD specifies `api_key_created` and `api_key_revoked` **[RESOLVED 2026-08-15]** — both now dispatched (see QA History).
- ~~**No `run_started` audit event**: Pipeline runs start without an audit event~~ **[RESOLVED 2026-08-15]** — dispatched from `PipelineExecutor._check_capacity`.
- **8 unguarded audit dispatch calls fixed**: All previously uncovered dispatch calls now have error handling protection (expiry_job, recovery, sso_provider, admin team deletion, run_purge transaction scoping, rotation deadlock)

## QA History

### 2026-08-15 — improve-architecture (team membership audit gaps)

**RESOLVED the "No team CRUD audit events" known gap — team membership changes now audited** (`api/routes/teams.py`):
- **`team_member_added`** — `add_member_endpoint` now appends the event after the primary operation commits (fresh transaction, RLS re-established), payload `team_id`/`user_id`/`role`, membership id as `resource_id`.
- **`team_member_removed`** — `remove_member_endpoint` now appends the event (payload `team_id`/`user_id`/`role` — the role the removed member held before revocation), membership id as `resource_id`; a 404 revoke emits nothing.
- **`team_member_role_changed`** — `change_member_role_endpoint` now captures the pre-update role (`get_membership` before `update_member_role`) and appends the event with `old_role`/`new_role`; a 404 emits nothing.
- **Failure isolation hardened to the api_keys pattern** — all six team audit appends (create/update/delete + the three membership events) now catch `asyncio.CancelledError: raise` + broad `except Exception` (logged warning, never fails the completed operation), replacing the previous IntegrityError/ProgrammingError/SQLAlchemyError-only catches that let a generic audit failure bubble into a 500 (and previously turned a successful team create with an audit INSERT conflict into a 409).
- **Tests** — 11 new unit tests in `test_teams.py`: 2 per membership route (emits event with full payload; audit failure does not block the operation) + add-member not-found-no-emit, remove-member 404 no-emit, and 4 new `TestChangeMemberRole` cases (200, 404, invalid role 422, audit failure isolation).
- Updated product map `core/audit-trail.md` (2 PRD event-type rows `[ ]`→`[x]` + `team_member_*` rows, 3 new implemented-event entries, Known Gap → RESOLVED, `code:` frontmatter + `team-rbac.md` role-change Known Gap → RESOLVED). Verification: 124/124 teams + audit-logger + route-introspection unit tests pass, ruff check + format clean, mypy --strict clean on `teams.py`. Status: partial (14 remaining PRD event types undispatched; `team_renamed` still fires as `team_updated`).

### 2026-08-15 — improve-architecture (audit event gaps)

**RESOLVED 3 known gaps / 4 new dispatched event types** (PRD §8.12 vocabulary):
- **`run_started`** — `PipelineExecutor._check_capacity` now appends a `run_started` audit event (resource_type `run`, resource_id = run id, payload `pipeline_id`) at the pending→running claim transition in the execute() path. The resume() path sets `running` directly and never calls `_check_capacity`, so the event fires exactly once per run. Failure-isolated (broken append logs a warning and never blocks run admission). 3 unit tests in `test_executor.py`: admitted-run emits event with correct payload, capacity-blocked run emits nothing, audit failure does not block admission.
- **`hitl_claimed`** — `HITLManager.claim()` now appends a `hitl_claimed` audit event (actor = claimant, resource_type `hitl_claim`, resource_id = claim id, payload `pipeline_run_id`/`node_id`/`team_id`/`expiry_minutes`) after the atomic claim UPDATE + TOCTOU re-verification pass. Failure-isolated. 2 unit tests in `test_hitl_manager.py`: success emits event with full payload, audit failure does not fail the claim.
- **`api_key_created`** / **`api_key_revoked`** — the api-keys create and revoke routes now append the PRD-specified audit events (key id as `resource_id`, raw key never logged; payload `name`/`role`/`team_id` for create, `revoked_by` for revoke). Written in a fresh transaction after the primary operation commits and failure-isolated (a broken append never fails a completed create/revoke; a 404 revoke emits nothing). 5 unit tests in `test_api_keys_endpoint.py`: create emits, create audit failure isolated, revoke emits, 404 revoke emits nothing, revoke audit failure isolated.

Verification: 1081 unit tests across `tests/unit/pipeline_engine` + `tests/unit/hitl_manager` + `tests/unit/api/test_api_keys_endpoint.py` + `tests/unit/audit_logger` pass; `tests/unit/auth` + route-introspection + audit endpoint tests (542 + 73) pass; ruff check + format clean; mypy --strict clean on all 3 modified source modules. Status: partial (14 PRD event types still undispatched).

### Cross-cutting QA (index 246)
**Findings discovered and fixed:**
- CRITICAL: `reject_gate` route (hitl.py:325) was missing `actor_id=principal.account_id` — every `hitl.output_rejected` audit event had `actor_id=None`. Now passes the authenticated user's account_id.
- MAJOR: `append_audit_event` (audit_logger/__init__.py:152) only caught `IntegrityError`, allowing `ProgrammingError` and `SQLAlchemyError` to propagate as raw 500s from all 13 call sites. Added `except ProgrammingError` and `except SQLAlchemyError` with structured logging.
- Fixed 3 stale product map claims: "No hitl.rejected audit event" was wrong (code dispatched `hitl.output_rejected`); "No hitl_claimed/hitl_approved/hitl_rejected" was partially wrong; added `hitl.output_rejected` to implemented event types list.
- Status: partial (14 remaining gaps — PRD event type divergence, team CRUD audit missing, API key audit, run_started, actor_id fix applied)

### Cross-cutting QA (index 142)
**Findings discovered and fixed:**
- CRITICAL: 8 audit dispatch calls had no error handling — wrapped in try/except (expiry_job, recovery, sso_provider x4, admin team delete, rotation started deadlock)
- CRITICAL: `run_purge` audit event written outside `session.begin()` — purge committed before audit; moved inside transaction for atomicity
- CRITICAL: `fernet_key_rotation_started` audit failure deadlocked `_rotation_in_progress` flag — flag now set after successful audit write
- All 21 implemented event types verified against code (19 unique dispatched + 2 paths)
- Confirmed all 4 API routes have ProgrammingError→501 catches
- All BDD feature files have real step implementations (not placeholders)
- Status: partial (PRD event type divergence remains, team CRUD audit missing, API key audit missing, run_started missing, reject_gate actor_id fixed)
