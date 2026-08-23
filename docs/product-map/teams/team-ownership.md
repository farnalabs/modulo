---
id: feat-teams-team-ownership
prd: 9.3
delivery-tasks: [task-nv1-team-ownership]
bdd:
  - backend/tests/bdd/features/workflows/import.feature
  - backend/tests/bdd/features/workflows/export.feature
  - backend/tests/bdd/features/library/copy_to_adapt.feature
code:
  - backend/src/modulo/db/models/pipeline.py
  - backend/src/modulo/db/models/connector_instance.py
  - backend/src/modulo/db/models/model_backend.py
  - backend/src/modulo/db/models/library_primitive.py
  - backend/src/modulo/db/models/run.py
  - backend/src/modulo/db/crud/pipeline.py
  - backend/src/modulo/db/crud/connector_instance.py
  - backend/src/modulo/db/crud/model_backend.py
  - backend/src/modulo/db/crud/library_primitive.py
  - backend/src/modulo/db/crud/run.py
  - backend/src/modulo/api/routes/pipelines.py
  - backend/src/modulo/core/team_visibility.py
  - backend/src/modulo/api/routes/connectors.py
  - backend/src/modulo/api/routes/model_backends.py
  - backend/src/modulo/api/routes/library.py
  - backend/src/modulo/api/routes/contributions.py
  - backend/src/modulo/api/routes/admin.py
  - backend/src/modulo/core/workflow_import_export/__init__.py
  - backend/src/modulo/core/library_service/__init__.py
  - backend/src/modulo/db/migrations/versions/0001_v2_identity_org.py
  - backend/src/modulo/db/migrations/versions/0003_v2_pipeline_runtime.py
unit-tests:
  - backend/tests/unit/library_service/test_workflow_import_export_resilience.py
  - backend/tests/unit/core/library_service/test_contribute.py
  - backend/tests/unit/api/test_contributions.py
  - backend/tests/unit/db/test_schema.py
  - backend/tests/unit/db/test_migration_team_visibility_rls.py
depends-on: [feat-teams-team-crud]
status: partial
---
# Team Ownership (Resource Ownership)

Resource-level team ownership model – pipelines, connector instances, model
backends, library primitives, and runs carry `owner_team_id` (nullable FK) and
`visibility` (`org` | `team`). Controls which team can see and use each resource
with enforcement via DB constraints, RLS policies, and ViewModel validation.

## Behaviours

### Schema & Models
- [x] Pipeline, ConnectorInstance, ModelBackend, LibraryPrimitive all carry `owner_team_id` (nullable UUID FK) and `visibility` (`org` | `team`, default `org`) – verified in ORM models
- [x] `owner_team_id` FK has `ondelete=RESTRICT` – verified on Pipeline model (line 39), prevents team deletion while resources exist
- [x] DB check constraint enforces `visibility = 'org' OR owner_team_id IS NOT NULL` on team-scoped entities – verified on Pipeline model (lines 20-23)
- [x] LibraryPrimitive has extended constraint: `visibility IN ('org', 'community') OR owner_team_id IS NOT NULL` – Pydantic validators exist; DB constraint `ck_library_primitives_team_owner` verified in migration 0002 (line 154) + ORM model
- [x] Community registry entries have `visibility='org'` and `owner_team_id=NULL` – verified in library_service
- [x] Run entity carries `owner_team_id` for team-level cost attribution – verified in CRUD
- [x] `owner_team_id` and `visibility` columns added in migrations 0002 (library_primitives) and 0003 (pipelines/connector_instances/model_backends/stages/environment_profiles/runs), not in the initial 0001
- [x] `owner_team_id` column added to runs in migration 0003_v2_pipeline_runtime

### Ownership Semantics
- [x] `owner_team_id=NULL` + `visibility=org` = accessible to all org members (legacy/unowned) – default pattern, verified
- [x] `owner_team_id` set = resource is team-private, visible only to owning team members plus org admins – DB/RLS supports it; route layer now passes `owner_team_id` through for pipelines/connectors/model_backends (feat-teams-team-ownership index 336)
- [x] Each resource has exactly one `owner_team_id` – single FK, no multi-team ACL support (documented limitation)
- [x] `owner_team_id=NULL` + `visibility=team` is invalid – blocked by DB check constraint (verified on Pipeline)
- [x] Admin may reassign ownership of any resource regardless of current team – `PATCH /api/v1/pipelines/{id}` accepts `owner_team_id`; `_assert_team_transition_allowed` gives org `admin` an RLS-parity bypass of all team gates (PRD 9.3). Ownership transfer adds `resource_team_ownership_changed` audit + blocks while any non-terminal run exists (2026-08-15 sweep)

### Pipeline Ownership Changes
- [x] Changing pipeline's `owner_team_id` is blocked while any non-terminal run exists (`pending`, `running`, `awaiting_human`, `claimed`) – `update_pipeline` raises `PipelineHasActiveRunsError`; `PATCH /pipelines/{id}` maps it to 409 `pipeline_has_active_runs` (2026-08-15 sweep)
- [x] ViewModel returns `pipeline_has_active_runs` when blocked by active runs – structured 409 detail `pipeline_has_active_runs: <N> run(s) still in progress...` (2026-08-15 sweep)
- [x] After ownership change completes, UI warns about connector rebinding: re-save pipeline to rebind connectors for new team – `PipelineResponse.connector_rebind_required` set on the PATCH response when `owner_team_id` changed; UI rendering of the warning is a frontend follow-up (2026-08-15 sweep)
- [x] Old snapshots remain valid for historical run records after ownership change but should not start new runs – verified by design: new runs always compile from the pipeline's *current* snapshot; old snapshots are only referenced by historical run records and never start a run (2026-08-15 sweep)

### Connector & Model Backend Ownership
- [x] Team-private connector instance only usable within pipelines owned by the same team – enforced at pipeline-save (`core/team_visibility.py`, HTTP 409)
- [x] Cross-team connector binding returns `connector_team_mismatch` error at pipeline-save command layer – implemented (PRD 9.3 named error)
- [x] Team-private model backend only usable within pipelines owned by the same team – `model_backend_team_mismatch` rule + `find_model_backend_team_mismatches` in `core/team_visibility.py`; enforced at graph-save via `_resolve_graph_references` on both `PATCH /pipelines/{id}/graph` and the combined `PATCH /pipelines/{id}` with `graph_json` (which passes the effective owner team, incl. org-owned `None`), 409 `model_backend_team_mismatch` (2026-08-15 sweep)
- [x] ConnectorInstance and ModelBackend carry `visibility` consistent with all other resource types – verified in ORM models

### Library Primitive Ownership
- [x] Local library entries carry `owner_team_id` (nullable) and `visibility` (`org` | `team`) – verified in CRUD and route models
- [x] Community registry entries are always `visibility=org` – read-only, no team scope – verified in library_service
- [x] Copy-to-adapt with `target_team_id` assigns `owner_team_id` on the new primitive – verified in library_service and route
- [x] Copy-to-adapt without `target_team_id` defaults ownership to org-wide – verified
- [ ] Copy of team-private primitive defaults ownership picker to source team – VERIFIED 2026-08-15 (partial-small-b sweep): NOT implemented. Both `CopyPipelineWizard.vue` and `LibraryPipelineWizard.vue` initialise `ownership` to `{ owner_team_id: null, visibility: 'org' }` and never pre-select the source team on copy-to-adapt of a team-private primitive; the picker always starts org-wide (see Known Gaps)

### Bundle Export & Import
- [x] Export strips `owner_team_id` and `visibility` from bundle – both stripped, visibility defaults to `"org"` in export bundle (feat-teams-team-ownership index 336)
- [x] Export preserves pipeline name and graph nodes (owner_team_id removed) – verified in workflow_import_export
- [x] Import presents ownership picker before confirming – user selects org-wide or team ownership – verified in route models
- [x] Import with `owner_team_id` set validates the team exists and user has access – verified in materialize_import

### Team Deletion & Ownership Cleanup
- [x] Team deletion blocked (`team_has_resources` error) if any resource has `owner_team_id` pointing to the team – verified in teams.py and admin.py
- [x] Admin can bulk-reassign all team-owned resources to org-wide before confirming deletion – `POST /api/v1/admin/teams/{id}/reassign-all` sets `owner_team_id=NULL` + `visibility='org'` across pipelines, connectors, model backends, library primitives (2026-08-15 sweep)
- [x] Team deletion with no owned resources succeeds immediately – verified
- [x] Bulk-reassign followed by delete is idempotent (reassigning already-org resources succeeds) – returns 200 `reassigned=0` when the team has no owned resources; 404 only when the team itself is missing (2026-08-15 sweep)

### Audit Events
- [x] `resource_team_ownership_changed` audit event records `resource_type`, `resource_id`, `old_team_id`, `new_team_id`, `changed_by` – appended by `update_pipeline` (CRUD) whenever `owner_team_id` changes on the REST route path, in the same transaction (2026-08-15 sweep)

### RLS Enforcement
- [x] `rls_team_isolation` policy exists on pipelines, connector_instances, model_backends, and library_primitives – migration 0002 (`library_primitives`) + 0003 (`pipelines`, `connector_instances`, `model_backends`, `stages`, `environment_profiles`); 0109/0110 scope them to the current org; verified by `test_migration_team_visibility_rls.py`
- [x] Admin bypasses team scope via `current_setting('app.org_role') = 'admin'` check in RLS policy – verified in migration 0002:382 / 0003:918 and asserted by `test_migration_team_visibility_rls.py` (`nullif(current_setting('app.org_role', true), '') = 'admin'`)
- [x] User not in any team sees only org-visibility resources – no team-private leakage – policy `USING` clause is `(visibility='org' OR visibility IS NULL OR owner_team_id IS NULL OR membership-in-owning-team OR admin)`; a user with no membership rows passes only the org/legacy branch (2026-08-15 sweep)
- [x] User in multiple teams sees each team's resources independently with their respective team roles – policy membership clause is `owner_team_id IN (SELECT team_id FROM team_memberships WHERE account_id = ...)`: any membership row for the queried account qualifies per team (Phase-1 floor treats all team roles equally per ADR 017) (2026-08-15 sweep)
- [x] RLS policy evaluates `(owner_team_id IS NULL) OR (owner_team_id IN (...))` – legacy/org resources always visible – pattern from spec

### BDD Coverage
- [x] Import assigns `owner_team_id` from bundle selection (import.feature:46-49) – verified
- [x] Export strips `owner_team_id` from bundle (export.feature:21-24) – verified
- [x] Copy-to-adapt propagates `target_team_id` as `owner_team_id` (copy_to_adapt.feature:21-23) – verified

### Error States
- [x] Creating resource with `visibility=team` but no `owner_team_id` blocked by Pydantic validators – all 4 resource types (pipeline, connector, model_backend, library) now have `@model_validator` enforcing the constraint (feat-teams-team-ownership index 336)
- [x] Team deletion blocked when owned resources exist (`team_has_resources`) – verified in teams.py and admin.py
- [x] Cross-team connector binding blocked (`connector_team_mismatch`) – enforced at pipeline-save command layer (HTTP 409)
- [x] Pipeline ownership change blocked during active runs (`pipeline_has_active_runs`) – 409 structured error mapped from `PipelineHasActiveRunsError` in `PATCH /pipelines/{id}` (2026-08-15 sweep)
- [x] Non-admin using ownership change endpoint returns 403 – non-admin without membership of the current/new team is rejected 403 by `_assert_team_transition_allowed` + the `require_team_membership_or_admin` dependency; org `admin` bypasses (RLS parity) (2026-08-15 sweep)
- [x] Import with non-existent `owner_team_id` returns validation error – verified in materialize_import
- [x] Copy-to-adapt of community primitive via MCP returns 403 (community_primitive_read_only – must use browser UI) – `copy_to_adapt(via_mcp=True)` raises `CommunityPrimitiveReadOnlyError` when the source is `visibility='community'`; route maps to 403 (2026-08-15 sweep)

### Edge Cases
- [x] Changing a pipeline's team then assigning a different-team connector bound in an old snapshot – old snapshot unusable for new runs, new runs use rebinding – enforced by design: cross-team connector/model-backend binding is blocked at every graph-save (409), and runs always compile from the pipeline's *current* snapshot, never an old one (2026-08-15 sweep)
- [x] Unsetting `owner_team_id` (reassign to org-wide) clears team visibility – resource becomes org-visible – `PATCH /pipelines/{id}` with `owner_team_id=null` + `visibility=org` clears team scope (audited `old_team_id`→`new_team_id`); a non-admin clearing while keeping `visibility=team` is rejected 422 by the transition gate (2026-08-15 sweep)
- [x] Team rename does not affect resource ownership – `owner_team_id` references team UUID, not name – verified by FK design
- [x] Multiple resources owned by same team – bulk team deletion blocked until all reassigned – verified in resource check logic

### Error Handling (API Resilience)
- [x] All DB-backed ownership routes catch `ProgrammingError` and return 501 Not Implemented – verified: connectors.py (5 routes), pipelines.py, model_backends.py all have ProgrammingError catches (templates.py fixed 503→501 in feat-teams-team-ownership index 336)
- [x] All DB-backed ownership routes catch `SQLAlchemyError` and return 503 Service Unavailable – verified: connectors.py (5 routes), pipelines.py, model_backends.py all have SQLAlchemyError catches
- [x] Connector credential validation failures (GitHub scope check) return structured 422 with scope details – `_github_missing_scope_detail` reports the missing classic scopes or fine-grained permissions; exercised by `test_connectors_endpoint.py` (2026-08-15 sweep)
- [ ] Team deletion audit event recording is in a separate transaction from the delete – if audit recording fails, deletion has already occurred. Accepted best-effort (fail-open) design: the audit is deliberately isolated so an audit-write failure never rolls back a completed team deletion; recorded in Known Gaps. Making it atomic would flip the design to fail-closed (audit failure blocks the delete) which contradicts the audit's best-effort role

### Resilience
- [x] Missing DB table (migration not applied) does not crash the API – all 4 resource route files (pipelines.py, connectors.py, model_backends.py, library.py) enforce ProgrammingError→501 on every route
- [x] Concurrent resource assignment to a team being deleted does not produce inconsistent state – the resource-check and delete are in one transaction (mitigates TOCTOU for the delete itself)
- [x] Ownership validation failures surface as structured 4xx errors, not opaque 500s – all 4 resource types now have Pydantic cross-field validators for `visibility='team'` requiring `owner_team_id` (feat-teams-team-ownership index 336)

## QA History
- **2026-08-15 – distribute (final-pass sweep C)**: Documented the unchecked copy-ownership-picker gap in Known Gaps – copy-to-adapt of a team-private primitive does not default the ownership picker to the source team. Status: partial.

- **2026-08-15 (improve-architecture)**: Drove the entry from 39/61 → 59/61 covered. IMPLEMENTED the ownership-transfer path: (1) `update_pipeline` (CRUD) now blocks `owner_team_id` changes while any non-terminal run exists (`PipelineHasActiveRunsError` → 409 `pipeline_has_active_runs`) and appends the `resource_team_ownership_changed` audit event (resource_type, resource_id, old_team_id, new_team_id, changed_by) in the same transaction; (2) `PipelineResponse.connector_rebind_required` set on ownership-change PATCH responses; (3) `POST /api/v1/admin/teams/{id}/reassign-all` bulk-reassigns all team-owned resources to org-wide (idempotent, `reassigned` count); (4) team-private model-backend enforcement mirroring the connector rule (`model_backend_team_mismatch`, 409) at graph-save via `_resolve_graph_references`. VERIFIED (marked `[x]`): `ck_library_primitives_team_owner` DB constraint, admin ownership reassignment + RLS-parity bypass, non-admin 403 gate, GitHub-scope 422 detail, MCP community copy-to-adapt 403, all 4 RLS enforcement items (policy SQL + admin bypass + no-team/multi-team visibility), unsetting `owner_team_id` clears team scope, old-snapshot semantics. New tests in `test_teams.py` (route rebind flag, 409 mapping, CRUD active-runs block + audit payload, transition gates, model-backend mismatch rule/finder), `test_admin.py` (bulk reassign + idempotence + 403/404), `test_error_handling.py` (reassign-all 501/503). 2 unchecked remain (frontend ownership-picker default; team-deletion audit best-effort isolation), both documented as Known Gaps.

- **2026-08-01 (improve-architecture)**: Implemented cross-team connector binding enforcement (`connector_team_mismatch`, PRD 9.3) at the pipeline-save command layer and on the MCP binding paths. New `backend/src/modulo/core/team_visibility.py` (pure rule + async DB check); `api/routes/pipelines.py` raises HTTP 409 on `PATCH /pipelines/{id}/graph` and `PATCH /pipelines/{id}` (with `graph_json`); `api/mcp_server.py` returns the named error from `update_pipeline_graph`/`bind_connector_to_node`. Marked 3 behaviours `[x]`, resolved the `connector_team_mismatch` BDD test-level gap. Model-backend team-private enforcement remains an open gap.

- **2026-07-08 (index 336)**: Cross-cutting QA by improve-architecture. Fixed CRITICAL – `owner_team_id` missing from PipelineCreate/Update/Response, ConnectorCreate/Update/Response, and ModelBackendCreate/Update/Response route models despite DB/CRUD/RLS support. Added field to all 6 Create+Update models + 3 Response models with `@model_validator` cross-field validation (`visibility='team'` requires `owner_team_id`). Fixed MAJOR – export bundle did not strip `visibility`, risking `visibility=team` + `owner_team_id=NULL` on re-import; export now sets `visibility: "org"`. Fixed MAJOR – templates.py had duplicate dead `except IntegrityError` handler in both list and create endpoints; ProgrammingError returned 503 instead of project-standard 501. Fixed MAJOR – list_templates Endpoint had duplicate IntegrityError handler. Fixed MINOR – `ConnectorResponse.model_config = {"from_attributes": False}` changed to `True` to support automatic model_validate. All 5 resource route files now enforce ProgrammingError→501 and SQLAlchemyError→503. Marked 8 [ ]→[x], resolved 3 Known Gaps. Tests pass.

## Known Gaps

### Code-Level Gaps
- **Copy of a team-private primitive does not default the ownership picker to the source team** – both `CopyPipelineWizard.vue` and `LibraryPipelineWizard.vue` initialise `ownership` to `{ owner_team_id: null, visibility: 'org' }` and never pre-select the source team on copy-to-adapt of a team-private primitive; the picker always starts org-wide (see the `copy_to_adapt` frontend-default gap below)
- ~~**No `owner_team_id` in Pipeline/Connector/ModelBackend route models**~~ – RESOLVED in feat-teams-team-ownership index 336. All 3 resource types now have `owner_team_id` on Create/Update/Response models with cross-field Pydantic validators.
- ~~**connectors.py has zero ProgrammingError/SQLAlchemyError catches**~~ – RESOLVED (product map was stale). Connectors.py already had catches on all 5 routes from prior QA passes.
- **Ownership transfer exists for pipelines only** – `PATCH /pipelines/{id}` reassigns `owner_team_id` (blocked on active runs + audited). Connectors, model backends, library primitives and environment profiles have no dedicated reassignment path beyond the new bulk `POST /api/v1/admin/teams/{id}/reassign-all`; per-resource transfer for those types is unimplemented.
- ~~**Export strips `owner_team_id` but not `visibility`**~~ – RESOLVED in feat-teams-team-ownership index 336. Export now sets `visibility: "org"` in bundle.
- **`owner_team_id` type inconsistency** – `contributions.py` uses `str | None` instead of `uuid.UUID | None`, converted at call time.
- **`create_pipeline_from_template` route does not accept `owner_team_id`** – pipelines created from templates cannot be team-assigned.
- ~~**`PATCH /pipelines/{id}` with `graph_json` does not enforce model-backend team scope**~~ – RESOLVED (2026-08-16, branch-fixer): the combined ownership-change + graph-json path now calls `_resolve_graph_references` with `effective_owner_team_id` and enforces `model_backend_team_mismatch` (409) exactly like the dedicated `PATCH /pipelines/{id}/graph` endpoint, including for org-owned pipelines (`owner_team_id=None`). Covered by `test_update_pipeline_with_graph_uses_effective_owner_team` and `test_replace_graph_org_pipeline_pinning_team_private_model_backend_is_409`.
- **`connector_rebind_required` is an API response field only** – the PATCH response flag exists and is tested; the UI warning ("re-save pipeline to rebind connectors for the new team") is a frontend follow-up. (2026-08-15)
- **Team deletion audit event in separate transaction** – both `admin.py` and `teams.py` record the audit event in a separate `session.begin()` after the delete, so if audit recording fails, the deletion already happened. Accepted best-effort (fail-open) design: making it atomic would roll back a completed team deletion on audit failure. Not treated as a fixable TOCTOU. (2026-08-15)
- **Org-admin clearing `owner_team_id` while `visibility` stays `team` surfaces a DB CHECK-constraint 500** – `_assert_team_transition_allowed` gives admins a full RLS-parity bypass (no 422 for them), so an admin who clears `owner_team_id` without also setting `visibility='org'` reaches the DB `visibility='org' OR owner_team_id IS NOT NULL` constraint and gets a 500 instead of a clean 4xx. Non-admins get a clean 422. Callers must set `visibility='org'` when clearing; a friendlier admin-path 422 is a small follow-up. (2026-08-15)

### Frontend-Level Gaps
- **Copy-to-adapt of a team-private primitive does not default the ownership picker to the source team** – the backend `copy_to_adapt` supports `target_team_id` (verified) and org-wide default (verified), but the frontend picker defaulting to the source team's `owner_team_id` is unverified/unimplemented. Requires a frontend change; deferred.

### Test-Level Gaps
- No dedicated BDD feature file for team ownership exists – only import/export/copy-to-adapt BDD features (`import.feature`, `export.feature`, `copy_to_adapt.feature`) cover ownership propagation
- ~~No BDD scenarios for `connector_team_mismatch` error path~~ – RESOLVED (2026-08-01): `cross_team_isolation.feature` scenario "Cross-team pipeline binding is blocked" now runs and exercises the real rule via `core/team_visibility.py`
- No BDD scenarios for pipeline ownership change blocked during active runs
- No BDD scenarios for `resource_team_ownership_changed` audit event
- No BDD scenarios for the `visibility=team + owner_team_id=NULL` invalid state DB constraint
- No BDD scenarios for team deletion blocked by owned resources at the API level
- No integration tests for ownership change with concurrent active runs

### 2026-07-31 – improve-architecture (product-map walk)

- Fixed stale CODE refs: `0001_initial_schema.py`/`0025_team_visibility_rls.py`/`0014_team_cost_attribution.py` renamed in v2 squash → `0001_v2_identity_org.py` + `0003_v2_pipeline_runtime.py`. Mapped `test_migration_0025.py` → `test_migration_team_visibility_rls.py`.
