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
  - backend/src/modulo/db/models/stage.py
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
  - backend/src/modulo/api/routes/connectors.py
  - backend/src/modulo/api/routes/model_backends.py
  - backend/src/modulo/api/routes/library.py
  - backend/src/modulo/api/routes/contributions.py
  - backend/src/modulo/api/routes/admin.py
  - backend/src/modulo/core/workflow_import_export/__init__.py
  - backend/src/modulo/core/library_service/__init__.py
  - backend/src/modulo/db/migrations/versions/0001_initial_schema.py
  - backend/src/modulo/db/migrations/versions/0025_team_visibility_rls.py
  - backend/src/modulo/db/migrations/versions/0014_team_cost_attribution.py
unit-tests: []
depends-on: [feat-teams-team-crud]
status: partial
---
# Team Ownership (Resource Ownership)

Resource-level team ownership model — pipelines, stages, connector instances, model
backends, library primitives, and runs carry `owner_team_id` (nullable FK) and
`visibility` (`org` | `team`). Controls which team can see and use each resource
with enforcement via DB constraints, RLS policies, and ViewModel validation.

## Behaviours

### Schema & Models
- [x] Pipeline, Stage, ConnectorInstance, ModelBackend, LibraryPrimitive all carry `owner_team_id` (nullable UUID FK) and `visibility` (`org` | `team`, default `org`) — verified in ORM models
- [x] `owner_team_id` FK has `ondelete=RESTRICT` — verified on Pipeline model (line 39), prevents team deletion while resources exist
- [x] DB check constraint enforces `visibility = 'org' OR owner_team_id IS NOT NULL` on team-scoped entities — verified on Pipeline model (lines 20-23)
- [ ] LibraryPrimitive has extended constraint: `visibility IN ('org', 'community') OR owner_team_id IS NOT NULL` — Pydantic validators exist but DB constraint not verified
- [x] Community registry entries have `visibility='org'` and `owner_team_id=NULL` — verified in library_service
- [x] Run entity carries `owner_team_id` for team-level cost attribution — verified in CRUD
- [x] `owner_team_id` and `visibility` columns present in initial Alembic migration (0001)
- [x] `owner_team_id` column added to runs in migration 0014

### Ownership Semantics
- [x] `owner_team_id=NULL` + `visibility=org` = accessible to all org members (legacy/unowned) — default pattern, verified
- [ ] `owner_team_id` set = resource is team-private, visible only to owning team members plus org admins — DB/RLS supports it, but route layer for pipelines/connectors/model_backends does NOT pass `owner_team_id` through, making team assignment impossible via REST API
- [x] Each resource has exactly one `owner_team_id` — single FK, no multi-team ACL support (documented limitation)
- [x] `owner_team_id=NULL` + `visibility=team` is invalid — blocked by DB check constraint (verified on Pipeline)
- [ ] Admin may reassign ownership of any resource regardless of current team — no endpoint exists; no ownership transfer mechanism

### Stage Team Ownership
- [x] Stage carries `owner_team_id` and `visibility` — ORM model includes both columns
- [ ] Team-visibility Stage may only contain pipelines owned by the same team — not verified in code
- [ ] Cross-team pipeline assignment to a team Stage is blocked with `stage_team_mismatch` error — not verified in code

### Pipeline Ownership Changes
- [ ] Changing pipeline's `owner_team_id` is blocked while any non-terminal run exists (`pending`, `running`, `awaiting_human`, `waiting_for_lock`) — no route endpoint exists to change ownership
- [ ] ViewModel returns `pipeline_has_active_runs` when blocked by active runs — not implemented
- [ ] After ownership change completes, UI warns about connector rebinding: re-save pipeline to rebind connectors for new team — not implemented
- [ ] Old snapshots remain valid for historical run records after ownership change but should not start new runs — not implemented

### Connector & Model Backend Ownership
- [ ] Team-private connector instance only usable within pipelines owned by the same team — no validation exists in route or CRUD layer
- [ ] Cross-team connector binding returns `connector_team_mismatch` error at ViewModel layer — not implemented
- [ ] Team-private model backend only usable within pipelines owned by the same team — no validation exists
- [x] ConnectorInstance and ModelBackend carry `visibility` consistent with all other resource types — verified in ORM models

### Library Primitive Ownership
- [x] Local library entries carry `owner_team_id` (nullable) and `visibility` (`org` | `team`) — verified in CRUD and route models
- [x] Community registry entries are always `visibility=org` — read-only, no team scope — verified in library_service
- [x] Copy-to-adapt with `target_team_id` assigns `owner_team_id` on the new primitive — verified in library_service and route
- [x] Copy-to-adapt without `target_team_id` defaults ownership to org-wide — verified
- [ ] Copy of team-private primitive defaults ownership picker to source team — frontend behaviour, not verified

### Bundle Export & Import
- [ ] Export strips `owner_team_id` and `visibility` from bundle — owner_team_id is stripped, but visibility is NOT stripped (gap: team-visible pipeline exports without team context)
- [x] Export preserves pipeline name and graph nodes (owner_team_id removed) — verified in workflow_import_export
- [x] Import presents ownership picker before confirming — user selects org-wide or team ownership — verified in route models
- [x] Import with `owner_team_id` set validates the team exists and user has access — verified in materialize_import

### Team Deletion & Ownership Cleanup
- [x] Team deletion blocked (`team_has_resources` error) if any resource has `owner_team_id` pointing to the team — verified in teams.py and admin.py
- [ ] Admin can bulk-reassign all team-owned resources to org-wide before confirming deletion — not implemented
- [x] Team deletion with no owned resources succeeds immediately — verified
- [ ] Bulk-reassign followed by delete is idempotent (reassigning already-org resources succeeds) — not implemented

### Audit Events
- [ ] `resource_team_ownership_changed` audit event records `resource_type`, `resource_id`, `old_team_id`, `new_team_id`, `changed_by` — not implemented

### RLS Enforcement
- [ ] `rls_team_isolation` policy exists on pipelines, stages, connector_instances, model_backends, and library_primitives — migration 0025 named `team_visibility_rls`, content needs verification
- [ ] Admin bypasses team scope via `current_setting('app.org_role') = 'admin'` check in RLS policy — not verified in migration
- [ ] User not in any team sees only org-visibility resources — no team-private leakage — relies on RLS policy, not verified
- [ ] User in multiple teams sees each team's resources independently with their respective team roles — relies on RLS policy, not verified
- [x] RLS policy evaluates `(owner_team_id IS NULL) OR (owner_team_id IN (...))` — legacy/org resources always visible — pattern from spec

### BDD Coverage
- [x] Import assigns `owner_team_id` from bundle selection (import.feature:46-49) — verified
- [x] Export strips `owner_team_id` from bundle (export.feature:21-24) — verified
- [x] Copy-to-adapt propagates `target_team_id` as `owner_team_id` (copy_to_adapt.feature:21-23) — verified

### Error States
- [ ] Creating resource with `visibility=team` but no `owner_team_id` blocked by DB constraint — only LibraryPrimitive routes have cross-field validation; pipelines/connectors/model_backends routes lack `owner_team_id` entirely
- [x] Team deletion blocked when owned resources exist (`team_has_resources`) — verified in teams.py and admin.py
- [ ] Cross-team pipeline to team-stage assignment blocked (`stage_team_mismatch`) — no validation exists
- [ ] Cross-team connector binding blocked (`connector_team_mismatch`) — no validation exists
- [ ] Pipeline ownership change blocked during active runs (`pipeline_has_active_runs`) — no ownership change endpoint exists
- [ ] Non-admin using ownership change endpoint returns 403 — no ownership change endpoint exists
- [x] Import with non-existent `owner_team_id` returns validation error — verified in materialize_import
- [ ] Copy-to-adapt of community primitive via MCP returns 403 (community_primitive_read_only — must use browser UI) — not verified

### Edge Cases
- [ ] Changing a pipeline's team then assigning a different-team connector bound in an old snapshot — old snapshot unusable for new runs, new runs use rebinding — not implemented
- [ ] Unsetting `owner_team_id` (reassign to org-wide) clears team visibility — resource becomes org-visible — not implemented
- [x] Team rename does not affect resource ownership — `owner_team_id` references team UUID, not name — verified by FK design
- [x] Multiple resources owned by same team — bulk team deletion blocked until all reassigned — verified in resource check logic

### Error Handling (API Resilience)
- [ ] All DB-backed ownership routes catch `ProgrammingError` and return 501 Not Implemented — connectors.py (5 routes) has zero ProgrammingError catches; pipelines.py and model_backends.py are covered
- [ ] All DB-backed ownership routes catch `SQLAlchemyError` and return 503 Service Unavailable — connectors.py (5 routes) has zero SQLAlchemyError catches
- [ ] Connector credential validation failures (GitHub scope check) return structured 422 with scope details — verified
- [ ] Team deletion audit event recording is in a separate transaction from the delete — if audit fails, deletion has already occurred (TOCTOU in admin.py lines 1142-1155 and teams.py lines 343-355)

### Resilience
- [ ] Missing DB table (migration not applied) does not crash the API — only model_backends.py and library.py enforce this on every route; connectors.py routes propagate raw 500s
- [ ] Concurrent resource assignment to a team being deleted does not produce inconsistent state — the resource-check and delete are in one transaction (mitigates TOCTOU for the delete itself)
- [ ] Ownership validation failures surface as structured 4xx errors, not opaque 500s — partially implemented (LibraryPrimitive has Pydantic validators, but other entity types lack validation entirely)

## Known Gaps

### Code-Level Gaps
- **No `owner_team_id` in Pipeline/Connector/ModelBackend route models** — `PipelineCreate`, `PipelineUpdate`, `ConnectorCreate`, `ConnectorUpdate`, `ModelBackendCreate`, `ModelBackendUpdate` all lack `owner_team_id` even though the CRUD layer supports it. Team assignment is impossible through the REST API for these entity types. Only `LibraryPrimitive` routes properly wire `owner_team_id`.
- **connectors.py has zero ProgrammingError/SQLAlchemyError catches** — all 5 connector endpoints will produce raw 500s if DB tables are missing. Should follow model_backends.py pattern.
- **No ownership transfer API** — no endpoint exists to reassign a resource from one team to another or to bulk-reassign all resources when a team is deleted. Team deletion simply blocks with 409.
- **Export strips `owner_team_id` but not `visibility`** — a team-visible pipeline is exported without team context, making re-import potentially produce `visibility=team` + `owner_team_id=NULL` which violates the DB check constraint.
- **`owner_team_id` type inconsistency** — `contributions.py` uses `str | None` instead of `uuid.UUID | None`, converted at call time.
- **`create_pipeline_from_template` route does not accept `owner_team_id`** — pipelines created from templates cannot be team-assigned.
- **Team deletion audit event in separate transaction** — both `admin.py` and `teams.py` record the audit event in a separate `session.begin()` after the delete, so if audit recording fails, the deletion already happened.

### Test-Level Gaps
- No dedicated BDD feature file for team ownership exists — only import/export/copy-to-adapt BDD features (`ownership_picker.feature`, `import.feature`, `export.feature`, `copy_to_adapt.feature`) cover ownership propagation
- No BDD scenarios for `stage_team_mismatch` error path
- No BDD scenarios for `connector_team_mismatch` error path
- No BDD scenarios for pipeline ownership change blocked during active runs
- No BDD scenarios for `resource_team_ownership_changed` audit event
- No BDD scenarios for the `visibility=team + owner_team_id=NULL` invalid state DB constraint
- No BDD scenarios for team deletion blocked by owned resources at the API level
- No integration tests for ownership change with concurrent active runs 