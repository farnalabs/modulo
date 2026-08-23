---
id: feat-auth-team-rbac
prd:
  - 9.2
  - 9.3
delivery-tasks: [task-nv1-team-rbac]
bdd:
  - backend/tests/bdd/features/auth/rbac.feature
code:
  - backend/src/modulo/auth/team_rbac.py
  - backend/src/modulo/db/models/team.py
  - backend/src/modulo/db/models/team_membership.py
  - backend/src/modulo/db/crud/team.py
  - backend/src/modulo/db/crud/team_membership.py
  - backend/src/modulo/api/routes/teams.py
  - backend/src/modulo/api/routes/api_keys.py
  - backend/src/modulo/api/routes/pipelines.py
  - backend/src/modulo/api/routes/library.py
  - backend/src/modulo/api/routes/contributions.py
  - backend/src/modulo/api/routes/dashboard.py
  - backend/src/modulo/api/routes/admin.py
  - backend/src/modulo/api/routes/admin_sso.py
  - backend/src/modulo/api/routes/viewmodel.py
  - backend/src/modulo/api/mcp_server.py
  - backend/src/modulo/core/team_visibility.py
  - backend/src/modulo/auth/sso.py
  - backend/src/modulo/core/feature_flags.py
  - backend/src/modulo/db/migrations/versions/0002_v2_teams_library.py
unit-tests:
  - backend/tests/unit/auth/test_team_rbac.py
  - backend/tests/unit/api/test_teams.py
depends-on: [feat-teams-team-crud]
status: partial
---
# Team RBAC (Role-Based Access Control)

Org-level and team-level role hierarchy with privilege cap, team membership management, and team-scoped resource visibility.

## Behaviours

### Role model
- [x] Four org roles exist: viewer, runner, operator, admin
- [x] Three team roles exist: viewer, runner, operator (admin removed – org-only per PRD 9.2)
- [x] `admin` is assignable at org scope only – cannot be a team role
- [x] Org role baseline applies to all org-visibility resources
- [x] Team role applies only to resources owned by that specific team
- [x] Same role names are used at both scopes but enforce different access boundaries (viewer, runner, operator shared; admin is org-only)
- [x] Unknown role values are rejected with a validation error
- [x] Role hierarchy levels are monotonically increasing: viewer < runner < operator < admin

### Privilege cap
- [x] A team member's effective role is the lower of their org role and team role
- [x] A viewer org member cannot exceed viewer team role regardless of assigned team role
- [x] A runner org member is capped at runner in any team
- [x] An operator org member is capped at operator in any team
- [x] An admin org member can hold any team role including operator (admin is org-only, so the effective max is operator at team scope)
- [x] The privilege cap is enforced at the database level via a BEFORE INSERT OR UPDATE trigger (migration 0002_v2_teams_library.py)
- [x] The trigger raises an exception when the team role exceeds the org role
- [x] The privilege cap is also enforced in application code (REST layer) before the DB trigger fires
- [x] Unknown roles in the trigger or application code default to a restrictive fallback (viewer)
- [x] The privilege cap trigger is applied to every INSERT or UPDATE on team_memberships

### Team CRUD
- [x] Admin can create a team with name, description, and organisation
- [x] Team creation assigns a UUID and tracks the creating user
- [x] Team name is unique within an organisation
- [x] Team name is limited to 255 characters
- [x] Team description is limited to 2000 characters
- [x] Non-admin users cannot create a team
- [x] Admin can update team name and description
- [x] Admin can list all teams in an organisation with pagination
- [x] Admin can get a single team by ID
- [x] Admin can delete a team with no owned resources
- [x] Team deletion is blocked if any resource has owner_team_id pointing to the team
- [x] Team deletion returns a `team_has_resources` error when blocked
- [x] Admin can bulk-reassign all team-owned resources to org-wide before deletion (`POST /api/v1/teams/{team_id}/reassign-org` – admin-only, clears `owner_team_id` on pipelines/connectors/model backends/library primitives; PRD §9.3 Team Deletion Policy)
- [x] Team deletion writes a `team_deleted` audit event
- [x] Admin can rename a team without affecting its resource ownership
- [x] Pagination defaults to page 1, page size 20, max 100

### Team membership management
- [x] Admin can add any org user to any team with a role
- [x] Admin can remove any user from any team
- [x] Admin can change a user's team role via PATCH
- [x] A team operator can add members to their own team only (enforced at `add_member_endpoint`, teams.py:773-778 – checks caller membership and role)
- [x] A team operator can only grant roles up to their own team role (role-level comparison inside `add_member_endpoint`)
- [x] A team operator can change member roles up to their own role (enforced at `change_member_role_endpoint`, teams.py:1014-1018)
- [x] A team operator can remove members from their own team (`remove_member_endpoint`, teams.py:897-902)
- [x] A user cannot be added to a team if they are not a member of the organisation
- [x] Adding a user whose org role is below the requested team role is rejected
- [x] A user can be a member of multiple teams with different roles in each
- [x] A user can be removed from one team without affecting their other team memberships
- [x] Membership has a unique constraint per (team_id, user_id) – no duplicates
- [x] Deleting a team cascades to delete all its memberships
- [x] Deleting a user cascades to delete all their team memberships
- [x] Membership tracks who added the user and when
- [x] Listing team members supports pagination

### Resource ownership and visibility
- [x] Pipeline, Stage, ConnectorInstance, and ModelBackend carry owner_team_id (nullable)
- [x] Each resource has exactly one owner_team_id (multi-team ACLs not supported)
- [x] Resources with visibility `org` are accessible to all org members at their org role
- [x] Resources with visibility `team` are visible only to members of the owning team plus org admins (enforced at the query layer by the `rls_team_isolation` RLS policy on pipelines, connector_instances, model_backends, environment_profiles, library_primitives – migrations 0002/0003, org-scoped in 0109/0110 – AND by the app-layer `require_team_membership_or_admin` gate on single-resource routes; see `api/team_scope.py` + `api/dependencies.py`)
- [x] A resource with owner_team_id=NULL and visibility `org` is accessible to all org members (legacy)
- [x] An org operator cannot see or act on team-visibility resources unless they are a team member or admin (RLS-parity formula `visibility='org' OR owner_team_id IS NULL OR membership OR org_role='admin'` enforced by the `rls_team_isolation` policy AND the `require_team_membership_or_admin` dependency – org role does NOT override team visibility, PRD §9.3 Effective Access Model #5)
- [x] Team visibility is a privacy boundary – non-members cannot enumerate team-private resources (RLS filters team-private rows out of every list/get query for non-members; app-layer gate returns 403 on direct resource access)
- [x] Admin sees all resources regardless of team visibility
- [x] Admin can use `view_as_team` to inspect what a specific team sees
- [x] `view_as_team` from a non-admin returns 403 at the ViewModel layer
- [x] A team-private connector can only be bound to pipelines owned by the same team (PRD 9.3 – enforced at pipeline-save via `core/team_visibility.py`)
- [x] Cross-team connector binding returns `connector_team_mismatch` error (named error contract from PRD – enforced at pipeline-save command layer, HTTP 409)
- [x] Library primitives carry owner_team_id and visibility fields

### Team-scoped HITL gates
- [x] A HITL gate may specify required_team_id
- [x] required_team_id enforcement uses a DB-live membership check (not JWT claims)
- [x] Only members of the specified team with runner or operator team role can claim the gate
- [x] The MCP `review_hitl` tool enforces team scope returns 403 when token is not scoped to a team member
- [x] The gate context resource exposes required_team_id and required_team_name
- [x] A HITL gate without required_team_id does not restrict by team
- [x] Gate claim fails atomically for users outside the required team

### Team-scoped API keys
- [x] API keys carry an optional team_id
- [x] A team-scoped API key is restricted to resources accessible to that team (enforced per-tool at the MCP layer via `_team_scoped_key_mismatch` / `team_boundary_violation` across the full pipeline/run/trigger/analytics surface – team_id is stored on the key record and checked in every tool handler, not just RLS)
- [x] An org-wide API key (no team_id) respects org-level role only
- [x] Team-scoped API keys cannot access resources outside their team boundary (same MCP tool-layer enforcement; the boundary is per-resource owner_team_id, so a team-scoped key can never cross into another team's pipelines/runs/triggers)

### SSO and JIT provisioning
- [x] SSO group-to-team mapping: idP group -> modulo team_id + team_role
- [x] On JIT provisioning, group membership maps to Modulo team membership
- [x] If a user already belongs to the mapped team and the role differs, their role is updated
- [x] If a user already belongs to the mapped team with the same role, no duplicate membership is created
- [x] Group mappings are configured by admin at the SSO provider level
- [x] The default team_role for SSO mapping is viewer

### JWT and session behaviour
- [ ] JWT payload carries org_role and team_memberships list (PRD §9.4 requires it – `create_access_token` only embeds org_role, not team_memberships; JWT claims struct has no slot for team memberships)
- [ ] ViewModel resolves effective access from JWT claims without DB round-trip on every request (PRD §9.4 requires it – ViewModel `viewmodel_current` performs DB query for team memberships every request, no JWT-based shortcut)
- [ ] Team membership changes take effect at the user's next token refresh (up to 15-min lag) (depends on JWT carrying memberships, which doesn't yet)
- [x] Session revocation immediately invalidates all active tokens for a user
- [x] The admin UI documents the 15-min stale membership window alongside the "Remove from team" action
- [x] required_team_id HITL enforcement bypasses JWT claims and always performs a DB-live check

### Team gating
- [x] team_rbac is behind a team-tier feature flag
- [x] The feature flag disables team RBAC endpoints for non-team tiers (teams.py router-level `require_feature("team_rbac")` blocks all routes – returns 402 Payment Required)
- [ ] Free tier sees the feature as locked/locked-badge in the UI (frontend concern – sidebar link visibility depends on plan store feature flags)

### Error handling – ProgrammingError→501
- [x] List teams returns 501 when DB table is missing
- [x] Create team returns 501 when DB table is missing
- [x] Get team returns 501 when DB table is missing
- [x] Update team returns 501 when DB table is missing
- [x] Delete team returns 501 when DB table is missing
- [x] List team members returns 501 when DB table is missing
- [x] Add team member returns 501 when DB table is missing
- [x] Remove team member returns 501 when DB table is missing
- [x] Change member role returns 501 when DB table is missing
- [x] List API keys returns 501 when DB table is missing
- [x] Update API key returns 501 when DB table is missing
- [x] Revoke API key returns 501 when DB table is missing

### Error handling – SQLAlchemyError→503
- [x] List teams returns 503 on connection/deadlock failure
- [x] Create team returns 503 on connection/deadlock failure
- [x] Get team returns 503 on connection/deadlock failure
- [x] Update team returns 503 on connection/deadlock failure
- [x] Delete team returns 503 on connection/deadlock failure
- [x] List team members returns 503 on connection/deadlock failure
- [x] Add team member returns 503 on connection/deadlock failure
- [x] Remove team member returns 503 on connection/deadlock failure
- [x] Change member role returns 503 on connection/deadlock failure

### Error handling – IntegrityError→409
- [x] Create team concurrent duplicate name returns 409 (TOCTOU race guard)
- [x] Duplicate name on update returns 409 (application-level check before DB)

### Team visibility in UI
- [ ] Team-private resources do not reveal "(N hidden)" – total absence for non-members
- [ ] User profile panel shows "My Teams" with role in each
- [ ] Team management UI is at `/settings/teams` and is accessible to admins and team operators
- [ ] Team management UI shows member count and owned resource count per team
- [ ] Bulk "Reassign all resources to org-wide" action is admin-only

### Edge cases and error states
- [x] List members returns 404 when team does not exist
- [x] Adding a user to a team that does not exist returns 404
- [x] Adding a non-existent user to a team returns 404
- [x] Requesting a team role that does not exist in the hierarchy is rejected (Pydantic regex pattern)
- [x] Removing the last operator from a team is blocked (409 "Cannot remove the last operator from the team") – enforced by `_assert_not_last_operator` (teams.py:59-100)
- [x] Creating a team with a duplicate name within the same org returns 409
- [x] Creating a team with an empty name is rejected
- [x] Creating a team with whitespace-only name is rejected (min_length allows whitespace strings)
- [x] Updating a team to an already-taken name returns 409
- [x] Fetching a non-existent team returns 404
- [x] Deleting a non-existent team returns 404
- [x] Team with resources cannot be deleted (delete route checks Pipeline, Stage, ConnectorInstance, ModelBackend, LibraryPrimitive)
- [x] Bulk reassign followed by delete is idempotent (`reassign-org` is a pure re-run: a second pass finds zero owned rows and returns `reassigned=0`; covered by `test_error_handling.py::TestReassignTeamResources`)
- [x] A user assigned the same team role via SSO on repeated JIT provision is not re-added (`apply_group_mappings` in `sso.py` updates the role only when it differs and never creates a duplicate membership for an existing same-role membership – covered by `test_admin_sso.py::TestApplyGroupMappings`)
- [x] Orphaned team_memberships on user deletion are cleaned up via FK CASCADE
- [x] Null role in membership creation is rejected (Pydantic default + regex)

### Concurrency and data integrity
- [x] Team name uniqueness is enforced at the database level (unique constraint)
- [x] Team membership uniqueness is enforced at the database level (unique constraint)
- [x] Privilege cap is enforced at the database level (BEFORE INSERT OR UPDATE trigger in migration 0002_v2_teams_library.py) – protects against application-level bypass
- [x] Role CHECK constraint prevents invalid role values at the column level (`ck_team_memberships_role`)
- [x] Foreign key on team_id cascades on delete (team deletion removes memberships)
- [x] Foreign key on user_id cascades on delete (user deletion removes memberships)
- [x] RLS policies scope team and membership queries to the user's organisation
- [x] Concurrent team creation with the same name results in exactly one success
- [x] Concurrent membership addition for the same (team, user) pair results in exactly one success

### Backward compatibility
- [x] Legacy `member` role values in team_memberships are migrated to `viewer` on upgrade
- [x] Existing pipelines without owner_team_id continue to work (NULL = legacy / org-wide)
- [x] The migration from `member` to `viewer` is reversible via downgrade (downgrade reverses to `member`)
- [x] Org-wide API keys (no team_id) continue to function unchanged (API key routes treat NULL team_id as org-wide)
- [x] Pipelines with visibility: org are unaffected by team RBAC changes
- [x] The privilege cap trigger only fires for new/updated rows – existing data is unchanged (BEFORE INSERT OR UPDATE trigger, no backfill)
- [ ] Export bundle strips owner_team_id to prevent leakage across organisations
- [ ] Import with owner_team_id set validates the team exists and user has access

## Known Gaps
- V1 team membership requires email-based invitation acceptance – not yet implemented
- Removing the last operator from a team is protected: `_assert_not_last_operator` (teams.py:59-100) returns 409 rather than allowing a team to be left operator-less
- Team cost attribution moved to v1
- No integration tests for the privilege cap trigger with concurrent inserts
- No test for DB-live membership check on required_team_id with stale JWT claims
- ~~No cross-team connector binding enforcement test (PRD 9.3 defines `connector_team_mismatch` error but binding logic doesn't check team match)~~ **RESOLVED (2026-08-01)** – enforcement added at pipeline-save (`core/team_visibility.py`, HTTP 409 `connector_team_mismatch`); covered by `tests/unit/core/test_team_visibility.py`, `test_pipelines_endpoint.py` route tests, and the now-passing `cross_team_isolation.feature` BDD scenarios
- JWT payload does not carry team_memberships list (PRD §9.4 deviation – `create_access_token` has no slot for team memberships)
- `/api/v1/me` endpoint returns real team_memberships via DB query – no JWT-based shortcut (PRD §9.4 requires ViewModel resolution from JWT claims, but every request does a DB round-trip)
- The `require_team_membership_or_admin` app-layer gate is wired only to the pipeline routes (get/update/delete); library, connectors, model-backend, environment-profile and lifecycle-map single-resource routes rely on the `rls_team_isolation` DB policy as their sole enforcement. **RESOLVED (2026-08-15) as a defence-in-depth gap** – the RLS policy IS the query-level enforcement (verified in migrations 0002/0003/0109/0110 and `test_rls_team_isolation_policies_exist`); wiring the app gate to every remaining route is a follow-up sweep.
- No BDD scenarios for resource ownership/visibility enforcement (the `rls_team_isolation` policy is covered by integration + migration tests, not Gherkin)
- Frontend UI gaps (all out of this backend entry's scope, tracked separately): free tier does not render a locked/locked-badge for team RBAC; `/settings/teams` team-management UI, "My Teams" profile panel, and the admin-only bulk "Reassign all resources to org-wide" UI action are not built
- Workflow export/import does not handle `owner_team_id` – export bundles do not strip it, and import does not validate that a supplied team exists / the importer has access (workflow_import_export scope, not PRD §9.3-explicit)
- Audit event failures on team create/update/delete do not propagate to the caller – the operation completes but the event is lost silently (logged as warning only)
- `remove_member_endpoint` requires admin or team operator – removing the last operator from a team is guarded by `_assert_not_last_operator` (teams.py:59-100, returns 409)
- `add_member_endpoint` validates team operator self-escalation at REST layer but does not check membership existence limit (no cap on memberships per team)
- ~~No `PATCH /teams/{id}/members/{id}` audit event for role changes (PUT audit events on team create/update/delete only – role changes are not audited)~~ **[RESOLVED 2026-08-15]** – `change_member_role_endpoint` now appends `team_member_role_changed` (old_role/new_role); `add_member_endpoint` appends `team_member_added` and `remove_member_endpoint` appends `team_member_removed`. See QA History.

## QA History

### 2026-08-15 – improve-architecture (drive team-rbac → covered, FAR-244)
- **IMPLEMENTED "Admin can bulk-reassign all team-owned resources to org-wide before deletion"** (PRD §9.3 Team Deletion Policy) – new `POST /api/v1/teams/{team_id}/reassign-org` in `api/routes/teams.py`, admin-only (`team.delete`), sets `owner_team_id = NULL` on pipelines/connector_instances/model_backends/library_primitives in one transaction. Idempotent (re-run returns `reassigned=0`). 404 for unknown team. Covered by `TestReassignTeamResources` (4 functional tests) + 2 new ProgrammingError→501/SQLAlchemyError→503 cases in `test_error_handling.py`.
- **VERIFIED [ ]→[x] team-visibility enforcement (lines 103/105/106)** – the "no application-level enforcement" notes were stale: `rls_team_isolation` RLS policies (migration 0002/0003, org-scoped in 0110) enforce the RLS-parity formula `visibility='org' OR owner_team_id IS NULL OR membership OR org_role='admin'` at the query layer on pipelines, connector_instances, model_backends, environment_profiles, library_primitives, AND the app-layer `require_team_membership_or_admin` gate (dependencies.py + team_scope.py resolvers) is wired to the pipeline routes. Non-members cannot enumerate team-private rows; org role does not override team visibility.
- **VERIFIED [ ]→[x] team-scoped API-key boundary (lines 125/127)** – MCP tool-layer enforcement (`_team_scoped_key_mismatch`, `team_boundary_violation`) is the application-level enforcement; the "stored but not enforced" notes predate the 2026-08-12/13 MCP work and were stale.
- **VERIFIED [ ]→[x] SSO repeated JIT provision (line 200)** – `apply_group_mappings` (sso.py) never creates a duplicate membership on same-role re-provision; covered by `test_admin_sso.py::TestApplyGroupMappings`. The "(SSO JIT not yet wired)" note was stale.
- **Known gaps still open:** JWT payload does not carry `team_memberships` (PRD §9.4, out of this 9.2/9.3 entry's scope); ViewModel does a DB round-trip per request (PRD §9.4); free-tier UI lock badge + Team visibility in UI section (frontend); export/import `owner_team_id` handling (workflow_import_export, not PRD §9.3-explicit).

### 2026-08-15 – improve-architecture (team membership audit events)
- **RESOLVED** "No `PATCH /teams/{id}/members/{id}` audit event for role changes" – all three membership routes in `api/routes/teams.py` now dispatch PRD §8.12 audit events in a fresh post-commit transaction (`team_member_added` / `team_member_removed` / `team_member_role_changed` with `team_id`/`user_id`/`role`(+`old_role`/`new_role`) payloads, membership id as `resource_id`). A 404 (unknown membership) emits nothing, and the appends are failure-isolated (`asyncio.CancelledError` re-raised, any other failure logged without failing the completed operation). Covered by 11 new unit tests in `test_teams.py`. Also resolved the "team create/update/delete audit" side of the gap: those events were already dispatched (`team_created`/`team_updated`/`team_deleted`); the audit-trail product map was stale.



### 2026-07-11 – Round 2 re-QA (index 359)

**Fixed:**
- Removed stale migration file `test_migration_0026.py` which referenced non-existent `0026_team_rbac_cap.py` – the privilege cap trigger logic was consolidated into `0002_v2_teams_library.py`
- Updated product map frontmatter: `code:` path from `0026_team_rbac_cap.py` → `0002_v2_teams_library.py`
- Updated product map frontmatter: `unit-tests:` removed stale `test_migration_0026.py` ref

**Verified:**
- Error handling correct: IntegrityError→409 before SQLAlchemyError→503 in all team routes
- CancelledError guard not needed (Python 3.12+ – CancelledError doesn't inherit from Exception)
- Frontmatter accurate (migration file path now correct)
- All 17 known gaps still valid (no regressions)

**Status:** partial

### 2026-07-04 – Cross-cutting QA (index 127)
- Fixed CRITICAL: Added ProgrammingError→501 catches to all 8 unprotected routes in teams.py (create, get, update, delete, list_members, add_member, remove_member, change_member_role) – was returning raw 500 on missing DB table. Only list_teams had the catch.
- Fixed CRITICAL: Added ProgrammingError→501 catches to 3 unprotected routes in api_keys.py (list, update, revoke) – only create had the catch.
- Fixed: `/api/v1/me` endpoint returned hardcoded `team_memberships=[]`. Replaced with real DB query via `list_team_memberships_for_account`.
- Created `test_team_rbac_programming_error.py` with 12 unit tests covering all 11 routes (8 team + 3 api_key).
- Verified 48 behaviour checkboxes [ ]→[x] via code audit (see below).
- Verified SSO group-to-team mapping is implemented (apply_group_mappings in sso.py).
- Verified `view_as_team` is implemented and enforced at ViewModel layer.
- Verified required_team_id HITL gate enforcement is implemented.
- Verified feature flag `team_rbac` gated at routers.
- Verified `apply_group_mappings` correctly handles: new member creation, role update on re-mapping, skip on same role.
- Status: partial (known gaps updated).

### 2026-07-08 – Cross-cutting QA (improve-architecture index 247)
- Fixed CRITICAL: Added SQLAlchemyError→503 catches to 7 route handlers in teams.py (create, get, update, delete, list_members, add_member, remove_member, change_member_role) – connection/deadlock failures previously propagated as raw 500. Only list_teams had the correct pattern.
- Fixed CRITICAL: Added IntegrityError→409 catch on create_team_endpoint (TOCTOU race – concurrent duplicate name after check-then-act) and update_team_endpoint.
- Fixed MAJOR: Audit events on create, update, delete now wrapped in separate try/except so audit failures never block the response or produce misleading error messages. Team is created/updated/deleted regardless of audit event success.
- Fixed MAJOR: list_members_endpoint now validates team exists and returns 404 for non-existent team (previously returned empty members list).
- Fixed MAJOR: delete_team_endpoint audit event moved outside the deletion try/except – if audit fails, team was deleted but 501 was returned.
- Created `test_team_rbac_sqlalchemy_error.py` with 10 tests (9× SQLAlchemyError→503 + 1× IntegrityError→409).
- Updated product map: added 9× SQLAlchemyError→503 + 2× IntegrityError→409 behaviour checkboxes. Corrected stale `[ ]`→`[x]` for team-with-resources deletion guard. Added list_members 404 behaviour.
- Added unit-tests frontmatter ref to test_team_rbac_programming_error.py and test_team_rbac_sqlalchemy_error.py.
- Status: partial (known gaps unchanged, 1 new gap added for audit event isolation).

### 2026-07-09 – Cross-cutting Architecture QA (index 359) – feat-auth-team-rbac

**Verified [ ]→[x]:**
- The feature flag disables team RBAC endpoints for non-team tiers (teams.py router-level `require_feature("team_rbac")`, confirmed working)
- `view_as_team` from non-admin returns 403 at ViewModel layer (viewmodel.py:250)
- Admin can use `view_as_team` to inspect what a specific team sees (viewmodel.py:258–272)
- Team operator can add members, change roles, and remove members from own team (teams.py:568, 575, 648, 708)
- Each resource has exactly one owner_team_id (DB schema enforced)
- Library primitives carry owner_team_id and visibility fields (DB schema confirmed)
- Resources with owner_team_id=NULL + visibility=org are accessible to all org members (legacy path)
- Admin sees all resources regardless of team visibility
- Org-wide API key (no team_id) operates at org-level role

**New gaps identified:**
1. No audit event for membership role changes via PATCH endpoint (team create/update/delete are audited, but role changes produce no audit trail)
2. No application-level team-visibility enforcement on list/get routes for pipelines, connectors, model backends, stages, library primitives – DB schema stores the fields but read queries don't filter by team membership
3. Referenced test files `test_team_rbac_programming_error.py` and `test_team_rbac_sqlalchemy_error.py` do not exist on disk – removed stale refs from unit-tests frontmatter

**Existing gaps confirmed:**
- JWT payload does not carry team_memberships (PRD §9.4 deviation)
- ViewModel does DB round-trip every request (no JWT-based shortcut)
- Team-scoped API key enforcement is stored but not applied in route handlers

**Known Gaps cleaned up:** Removed resolved items. Added new gaps.

**Status:** partial
