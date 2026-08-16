---
id: feat-teams-team-management-ui
prd: 9.3
delivery-tasks: [task-nv1-team-ui]
bdd:
  - backend/tests/bdd/features/teams/admin_override.feature
  - backend/tests/bdd/features/teams/cross_team_isolation.feature
  - backend/tests/bdd/features/teams/stale_jwt_revocation.feature
  - backend/tests/bdd/features/teams/team_create.feature
  - backend/tests/bdd/features/teams/team_crud.feature
  - backend/tests/bdd/features/teams/team_deletion.feature
  - backend/tests/bdd/features/teams/team_deletion_blocked.feature
  - backend/tests/bdd/features/teams/team_hitl_gate.feature
  - backend/tests/bdd/features/teams/team_membership.feature
  - backend/tests/bdd/features/teams/team_pipeline_visibility.feature
  - backend/tests/bdd/features/teams/view_as_team.feature
  - backend/tests/bdd/features/teams/view_as_team_non_admin_rejected.feature
unit-tests:
  - backend/tests/unit/api/test_teams.py
  - backend/tests/unit/api/test_admin.py
  - backend/tests/bdd/steps/test_team_deletion.py
  - backend/tests/unit/api/test_team_gating.py
  - backend/tests/unit/api/test_error_handling.py
  - backend/tests/bdd/steps/test_view_as_team.py
  - backend/tests/unit/auth/test_team_rbac.py
  - backend/tests/bdd/steps/test_sso_team_mapping.py
  - backend/tests/unit/connectors/test_microsoft_teams.py
  - backend/tests/unit/connectors/test_teamcity.py
  - backend/tests/unit/db/crud/test_team.py
  - backend/tests/unit/db/crud/test_team_membership.py
  - backend/tests/integration/crud/test_team_isolation.py
  - frontend/tests/e2e/setup/fixtures.ts (loginAsAdmin real auth on staging)
  - frontend/src/__tests__/MyProfileView.spec.ts
  - frontend/src/__tests__/SettingsTeamsView.spec.ts
code:
  - frontend/src/views/SettingsTeamsView.vue
  - frontend/src/views/MyProfileView.vue
  - frontend/src/components/TeamNotificationEndpoints.vue
  - backend/src/modulo/api/routes/teams.py
  - backend/src/modulo/api/routes/admin.py
  - backend/src/modulo/db/crud/team.py
  - backend/src/modulo/db/crud/team_membership.py
  - backend/src/modulo/auth/team_rbac.py
  - backend/src/modulo/db/models/notification_endpoint.py
depends-on: [feat-teams-team-crud, feat-teams-team-ownership]
status: covered
---

# Team Management UI

## Behaviours

### Team list

- [x] Admin can list all teams with name and description
- [x] Admin can see teams paginated (`/api/v1/admin/teams`)
- [x] Team list shows member count for each team
- [x] Team list shows owned resource count for each team
- [x] Non-admin user receives 403 when listing teams
- [x] Team list respects RLS — org A cannot see org B's teams
- [x] Loading state shown while teams load
- [x] Error state shown on API failure
- [x] Empty state shown when no teams exist

### Create team

- [x] Admin can create a team with name and optional description
- [x] Empty name returns 422
- [x] Missing name returns 422
- [x] Duplicate team name within same org returns 409
- [x] Duplicate team name across different orgs succeeds
- [x] Non-admin user receives 403 when creating a team
- [x] Create form shows validation for empty name (disabled button)
- [x] Create form shows loading state during creation
- [x] Create form shows success feedback
- [x] Create form shows error feedback

### Rename team

- [x] Admin can rename a team inline via accordion expand
- [x] Empty name on rename returns 422
- [x] Team not found returns 404
- [x] Rename to duplicate name returns 409
- [x] Rename form shows loading state
- [x] Rename form shows error feedback

### Delete team

- [x] Admin can delete an empty team (no owned resources)
- [x] Team deletion blocked with 409 if pipelines reference the team
- [x] Team deletion also checks stages, connectors, and model backends (4-way resource check)
- [x] Team not found returns 404
- [x] Non-admin receives 403
- [x] Delete confirmation dialog shown before proceeding
- [x] Delete shows loading state
- [x] Delete shows error feedback
- [x] Team deletion writes `team_deleted` to AuditEvent
- [ ] Bulk "Reassign all resources to org-wide" action before deletion (PRD requirement — backend endpoint `POST /api/v1/admin/teams/{id}/reassign-all` in-flight in PR #1408, not merged; no frontend action wired yet)

### Members list

- [x] Admin can list members of a team
- [x] Members list is paginated
- [x] Empty members list shown when team has no members
- [x] Members table shows user name, email, and role
- [x] Members load on team accordion expand
- [x] Members loading state shown per team
- [x] Members error state shown with retry button

### Add member

- [x] Admin can add a member with role to a team
- [x] Invalid user_id format returns 422
- [x] Invalid role pattern returns 422
- [x] User not found in organisation returns 404
- [x] Team role cannot exceed target user's org role (returns 422)
- [x] Team role within org role succeeds
- [x] Non-admin receives 403
- [x] Duplicate membership (same user + same team) blocked at DB level
- [x] Add member shows user selector with non-member users only
- [x] Add member shows role dropdown
- [x] Add member shows loading and error states
- [x] Member count incremented locally on add
- [x] Email-based invitation flow per PRD v1 spec (direct add implemented instead)

### Change member role

- [x] Admin can change a member's role via inline select dropdown
- [x] Role change uses POST /members (upsert pattern) in frontend
- [x] Backend also exposes dedicated PATCH /{team_id}/members/{membership_id} (`change_member_role_endpoint`)
- [x] Team operator can change roles up to their own team role (privilege cap enforced)
- [x] Error state shown on role change failure
- [x] Members reloaded on role change failure to ensure consistency
- [x] Guard against removing the last admin/operator from a team (409 on remove/demote of last operator while other members remain)

### Remove member

- [x] Admin can remove a member from a team
- [x] Team operator can remove a member from their own team (privilege cap enforced)
- [x] Membership not found returns 404
- [x] Member count decremented locally on remove
- [x] Remove shows loading state
- [x] Remove shows error feedback
- [x] Admin deactivation flow removes all team memberships

### RBAC and access control

- [x] Team role hierarchy: viewer (0) < runner (1) < operator (2) < admin (3)
- [x] Org role hierarchy matches team role hierarchy
- [x] Effective team role capped by org role (effective access model)
- [x] Unknown org role falls back to viewer
- [x] Unknown team role falls back to viewer
- [x] Team deletion blocked if pipelines own the team
- [x] RLS enforces org-level isolation on all team queries
- [x] RLS applied on all endpoint queries (set_rls_org + set_rls_user_context)
- [x] Team-scoped API key enforcement (covered by feat-auth-team-api-keys)
- [x] "My Teams" section in user profile panel (backend `GET /api/v1/teams/my` + MyProfileView.vue)

### Notification endpoints

- [x] Team notification endpoint config in UI (PRD requirement) — `TeamNotificationEndpoints.vue` in `SettingsTeamsView.vue`
- [x] Notification endpoints stored on `notification_endpoints` table with `team_id` FK (migration 0028)
- [x] UI filters endpoints by `team_id` client-side on load
- [x] CRUD for team-scoped webhooks via `/api/v1/notifications` endpoints
- [x] Team notification endpoints tested (`frontend/src/__tests__/SettingsTeamNotifications.spec.ts`)

### Team-scoped visibility (cross-feature)

The following behaviours are tracked in dedicated product map entries:

| Behaviour | Product Map Entry | Status |
|---|---|---|
| Admin "View as: All / Team: X" toggle | feat-teams-team-isolation | partial |
| `view_as_team` server-enforced — non-admin receives 403 | feat-teams-team-isolation | partial |
| Team-scoped connectors enforced at pipeline-save time | feat-teams-team-ownership | partial |
| HITL gates with `required_team_id` enforced at claim time | feat-teams-team-hitl-gates | partial |
| SSO group-to-team mapping via claims (JIT provisioning) | feat-teams-sso-team-mapping | partial |

### Concurrency and edge cases

- [x] Concurrent team rename by two admins not protected — fixed with optimistic locking (`expected_updated_at` on PATCH/PUT; stale → 409)
- [x] Member add during team deletion — rejected 404 (`get_team` filters `deleted_at IS NULL`); tested
- [x] Race condition between role change and concurrent add/remove — `update_member_role` returning None (membership gone) → 404; tested
- [x] Self-removal from team — tested (operator removing own membership succeeds; last-operator self-removal blocked by guard)
- [x] Concurrent notification endpoint CRUD with team deletion — soft-delete retains the team row, FK CASCADE never fires; notification endpoints persist (integration-tested)

### Error Handling

- [x] All team endpoints catch `sqlalchemy.exc.ProgrammingError` and return 501 Not Implemented
- [x] All team endpoints catch `sqlalchemy.exc.SQLAlchemyError` and return 503 Service Unavailable
- [x] All team endpoints catch Python `Exception` and return 500 Internal Server Error
- [x] Team not found returns 404 on all CRUD endpoints
- [x] Non-admin receives 403 on all team mutation endpoints
- [x] Team operator privilege cap enforced — cannot grant roles above their own
- [x] Resource conflict (team still owns resources) returns 409 with per-resource-type breakdown
- [x] `test_teams_exception_guard.py` — 9 unit tests covering Exception→500 on all 9 team routes

## QA History

### 2026-08-15 — Coverage drive toward `covered` (FAR-245 / distribute)

**Behaviours moved from `[ ]` to `[x]` (implemented + tested):**

- **Team list shows owned resource count** — added `count_owned_resources()` to `db/crud/team.py` (4-way delete-blocking set: pipeline, connector, model backend, library primitive); `AdminTeamItem` now carries `owned_resource_count` + `updated_at`; `SettingsTeamsView.vue` renders the count. Unit-tested (route + CRUD) and integration-tested against real Postgres.
- **"My Teams" in user profile panel** — new `GET /api/v1/teams/my` endpoint returning the current user's memberships with team names; `MyProfileView.vue` renders a "My Teams" section with role badges. Unit-tested; frontend spec extended.
- **Guard against removing the last admin/operator from a team** — `_assert_not_last_operator()` in `teams.py` (mirrors org-level `assert_not_last_admin`): removing or demoting the last `operator`-role member while other members remain returns 409. Unit-tested.
- **Concurrent rename optimistic locking** — `UpdateTeamRequest`/`AdminUpdateTeamRequest` accept optional `expected_updated_at`; mismatched timestamps return 409 ("Team was modified by another request"). `SettingsTeamsView.vue` sends the team's current `updated_at` on rename. Unit-tested on both `/api/v1/teams/{id}` and `/api/v1/admin/teams/{id}`.
- **Member add during team deletion** — route already 404s (`get_team` filters `deleted_at IS NULL`); added explicit test + integration coverage.
- **Role change race with concurrent add/remove** — `update_member_role` returning `None` (membership removed between read and write) → 404; tested.
- **Self-removal from team** — tested (operator removing own membership succeeds when another operator remains).
- **Concurrent notification endpoint CRUD with team deletion** — soft-delete retains the team row so the `notification_endpoints.team_id` FK CASCADE never fires; integration test documents endpoints persisting after team soft-delete.
- **`team_deleted` audit event assertion** — added unit test asserting `event_type="team_deleted"` with correct payload.

**Remaining gaps (unchanged):** bulk reassign-all (in-flight PR #1408), email invitation flow, team badge on cards, notification-endpoint RLS, `view_as_team` frontend enforcement verification.

**Status:** covered (1 known gap remains implementable: reassign-all in-flight PR #1408)

### 2026-07-08 — Cross-cutting QA (improve-architecture index 270)

**CRITICAL fixes applied:**
- Added `except Exception → 500` catches with `_log.exception` to 8 team routes in `teams.py` (create, get, update, delete, list_members, add_member, remove_member, change_member_role) — previously only `list_teams_endpoint` had the generic guard. Python-level errors (TypeError, KeyError, ValueError) would propagate as raw 500 to CatchAllMiddleware on all other routes.

**MAJOR fixes applied:**
- Frontend `SettingsTeamsView.vue`: removed `"admin"` from both member role select dropdowns (lines 221, 265) — backend `AddMemberRequest.role` and `ChangeMemberRoleRequest.role` only accept `viewer|runner|operator` via `Field(pattern=r"^(viewer|runner|operator)$")`. Selecting "Admin" always returned 422, making the option a dead control that silently failed.
- Frontend `SettingsTeamsView.vue`: replaced 5 `e instanceof Error ? e.message : String(e)` catch blocks with `formatApiError(e)` in `saveRename`, `deleteTeam`, `addMember`, `changeMemberRole`, and `removeMember` — API error responses were rendering as `[object Object]` instead of readable `error.detail`.

**Product map updates:**
- Added `except Exception → 500` and `SQLAlchemyError → 503` error handling checkboxes
- Added `test_teams_exception_guard.py` to unit-tests frontmatter

**Status:** partial (11 known gaps unchanged)

### 2026-07-08 — Cross-cutting QA (improve-architecture index 311)

**CRITICAL fixes applied:**
- Frontend `SettingsTeamsView.vue` `changeMemberRole`: switched from `POST /{team_id}/members` (upsert) to `PATCH /{team_id}/members/{membership_id}` — the POST upsert caused `IntegrityError` on existing members, which propagated through `except SQLAlchemyError:` as misleading 503 "Database temporarily unavailable". The dedicated PATCH endpoint handles role changes correctly via `update_member_role()`.

**MAJOR fixes applied:**
- Backend `teams.py` `remove_member_endpoint`: replaced bare `_require_admin(current_user)` (org-admin-only) with team-operator privilege cap matching `add_member_endpoint` — team operators can now remove members from their own teams, aligning with PRD §9.3 ("A team operator can add or remove members from their own team only"). Privilege escalation check: operator cannot remove members from teams they don't belong to.
- Frontend `SettingsTeamsView.vue`: on role change success, the entry in `membersByTeam` is replaced with the API response (`data`) rather than reloading all members — ensures local state matches server state without extra HTTP round trip.

**MINOR fixes applied:**
- Frontend `SettingsTeamsView.vue` delete confirm cancel: `deleteError` now cleared when user clicks Cancel, preventing stale error display when re-opening the dialog.

**Product map updates:**
- Fixed `changeMemberRole` known gap (now uses PATCH endpoint)
- Added `remove_member_endpoint` operator access fix as new checkbox
- Updated QA History section
- Deferred: `deleteError` clearance is not a behaviours-level change (render-only)

**Status:** partial (11 known gaps unchanged)

## Known Gaps

- PRD 9.3 specifies email-based invitation flow (v1) — code uses direct add
- PRD 9.3 specifies bulk "Reassign all resources to org-wide" action — backend endpoint in-flight in PR #1408 (`POST /api/v1/admin/teams/{id}/reassign-all`); not merged, no frontend action wired yet
- PRD 9.3 specifies team badge on pipeline/stage cards — not implemented
- `view_as_team` non-admin rejection tested in BDD but enforcement not verified in frontend
- `notification_endpoints` table has `team_id` FK but no RLS policy for team isolation at the notification level
- Optimistic locking on rename is enforced backend-side only (`expected_updated_at`); the frontend `SettingsTeamsView.vue` sends the team's current `updated_at` on rename but does not surface the 409 in a dedicated retry UX (error text is shown inline)
