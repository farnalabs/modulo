---
id: feat-teams-team-ownership
prd: §9.3
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
depends-on: [task-nv1-team-entity]
status: partial
---

# Team Ownership (Resource Ownership)

Resource-level team ownership model — pipelines, stages, connector instances, model
backends, library primitives, and runs carry `owner_team_id` (nullable FK) and
`visibility` (`org` | `team`). Controls which team can see and use each resource,
with enforcement via DB constraints, RLS policies, and ViewModel validation.

## Behaviours

### Schema & Models

- [ ] Pipeline, Stage, ConnectorInstance, ModelBackend, LibraryPrimitive all carry `owner_team_id` (nullable UUID FK) and `visibility` (`org` | `team`, default `org`)
- [ ] `owner_team_id` FK has `ondelete=RESTRICT` — prevents team deletion while resources exist
- [ ] DB check constraint enforces `visibility = 'org' OR owner_team_id IS NOT NULL` on team-scoped entities
- [ ] LibraryPrimitive has extended constraint: `visibility IN ('org', 'community') OR owner_team_id IS NOT NULL`
- [ ] Community registry entries have `visibility='org'` and `owner_team_id=NULL` — no team scope for remote primitives
- [ ] Run entity carries `owner_team_id` for team-level cost attribution
- [ ] `owner_team_id` and `visibility` columns present in initial Alembic migration (0001)
- [ ] `owner_team_id` column added to runs in migration 0014

### Ownership Semantics

- [ ] `owner_team_id=NULL` + `visibility=org` = accessible to all org members (legacy/unowned)
- [ ] `owner_team_id` set = resource is team-private, visible only to owning team members plus org admins
- [ ] Each resource has exactly one `owner_team_id` — multi-team ACLs not supported (documented limitation)
- [ ] `owner_team_id=NULL` + `visibility=team` is invalid — blocked by DB check constraint
- [ ] Admin may reassign ownership of any resource regardless of current team

### Stage Team Ownership

- [ ] Stage carries `owner_team_id` and `visibility` — consistent with other resource types
- [ ] Team-visibility Stage may only contain pipelines owned by the same team
- [ ] Cross-team pipeline assignment to a team Stage is blocked with `stage_team_mismatch` error

### Pipeline Ownership Changes

- [ ] Changing pipeline's `owner_team_id` is blocked while any non-terminal run exists (`pending`, `running`, `awaiting_human`, `waiting_for_lock`)
- [ ] ViewModel returns `pipeline_has_active_runs` when blocked by active runs
- [ ] After ownership change completes, UI warns about connector rebinding: re-save pipeline to rebind connectors for new team
- [ ] Old snapshots remain valid for historical run records after ownership change but should not start new runs

### Connector & Model Backend Ownership

- [ ] Team-private connector instance only usable within pipelines owned by the same team
- [ ] Cross-team connector binding returns `connector_team_mismatch` error at ViewModel layer
- [ ] Team-private model backend only usable within pipelines owned by the same team
- [ ] ConnectorInstance and ModelBackend carry `visibility` consistent with all other resource types

### Library Primitive Ownership

- [ ] Local library entries carry `owner_team_id` (nullable) and `visibility` (`org` | `team`)
- [ ] Community registry entries are always `visibility=org` — read-only, no team scope
- [ ] Copy-to-adapt with `target_team_id` assigns `owner_team_id` on the new primitive
- [ ] Copy-to-adapt without `target_team_id` defaults ownership to org-wide
- [ ] Copy of team-private primitive defaults ownership picker to source team

### Bundle Export & Import

- [ ] Export strips `owner_team_id` and `visibility` from bundle — org-internal reference, meaningless outside source org
- [ ] Export preserves pipeline name and graph nodes (owner_team_id removed)
- [ ] Import presents ownership picker before confirming — user selects org-wide or team ownership
- [ ] Import with `owner_team_id` set validates the team exists and user has access

### Team Deletion & Ownership Cleanup

- [ ] Team deletion blocked (`team_has_resources` error) if any resource has `owner_team_id` pointing to the team
- [ ] Admin can bulk-reassign all team-owned resources to org-wide before confirming deletion
- [ ] Team deletion with no owned resources succeeds immediately
- [ ] Bulk-reassign followed by delete is idempotent (reassigning already-org resources succeeds)

### Audit Events

- [ ] `resource_team_ownership_changed` audit event records `resource_type`, `resource_id`, `old_team_id`, `new_team_id`, `changed_by`

### RLS Enforcement

- [ ] `rls_team_isolation` policy exists on pipelines, stages, connector_instances, model_backends, and library_primitives
- [ ] Admin bypasses team scope via `current_setting('app.org_role') = 'admin'` check in RLS policy
- [ ] User not in any team sees only org-visibility resources — no team-private leakage
- [ ] User in multiple teams sees each team's resources independently with their respective team roles
- [ ] RLS policy evaluates `(owner_team_id IS NULL) OR (owner_team_id IN (...))` — legacy/org resources always visible

### BDD Coverage

- [ ] Import assigns `owner_team_id` from bundle selection (import.feature:46-49)
- [ ] Export strips `owner_team_id` from bundle (export.feature:21-24)
- [ ] Copy-to-adapt propagates `target_team_id` as `owner_team_id` (copy_to_adapt.feature:21-23)

### Error States

- [ ] Creating resource with `visibility=team` but no `owner_team_id` blocked by DB constraint
- [ ] Team deletion blocked when owned resources exist (`team_has_resources`)
- [ ] Cross-team pipeline to team-stage assignment blocked (`stage_team_mismatch`)
- [ ] Cross-team connector binding blocked (`connector_team_mismatch`)
- [ ] Pipeline ownership change blocked during active runs (`pipeline_has_active_runs`)
- [ ] Non-admin using ownership change endpoint returns 403
- [ ] Import with non-existent `owner_team_id` returns validation error
- [ ] Copy-to-adapt of community primitive via MCP returns 403 (community_primitive_read_only — must use browser UI)

### Edge Cases

- [ ] Changing a pipeline's team then assigning a different-team connector bound in an old snapshot — old snapshot unusable for new runs, new runs use rebinding
- [ ] Unsetting `owner_team_id` (reassign to org-wide) clears team visibility — resource becomes org-visible
- [ ] Team rename does not affect resource ownership — `owner_team_id` references team UUID, not name
- [ ] Multiple resources owned by same team — bulk team deletion blocked until all reassigned

## Known Gaps

- No dedicated BDD feature file for team ownership exists — only import/export/copy-to-adapt BDD features cover ownership propagation
- No BDD scenarios for `stage_team_mismatch` error path
- No BDD scenarios for `connector_team_mismatch` error path
- No BDD scenarios for pipeline ownership change blocked during active runs
- No BDD scenarios for `resource_team_ownership_changed` audit event
- No BDD scenarios for the `visibility=team + owner_team_id=NULL` invalid state DB constraint
- No BDD scenarios for team deletion blocked by owned resources at the API level
- No integration tests for ownership change with concurrent active runs
- No BDD feature file at `backend/tests/bdd/features/teams/` directory — the PRD lists 10 planned team feature files but none exist yet
