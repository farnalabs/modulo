---
id: feat-teams-team-management-ui
prd: 9.3
delivery-tasks: [task-nv1-team-ui]
bdd:
  - backend/tests/bdd/features/teams/admin_override.feature
  - backend/tests/bdd/features/teams/cross_team_isolation.feature
  - backend/tests/bdd/features/teams/ownership_picker.feature
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
  - backend/tests/unit/api/test_team_deletion_bdd.py
  - backend/tests/unit/api/test_team_gating.py
  - backend/tests/unit/api/test_view_as_team_bdd.py
  - backend/tests/unit/auth/test_team_rbac.py
  - backend/tests/unit/auth/test_sso_team_mapping_bdd.py
  - backend/tests/unit/connectors/test_microsoft_teams.py
  - backend/tests/unit/connectors/test_teamcity.py
  - backend/tests/unit/db/crud/test_team.py
  - backend/tests/unit/db/crud/test_team_membership.py
  - backend/tests/integration/crud/test_team_isolation.py
  - backend/tests/staging_e2e/test_teams.py
code:
  - frontend/src/views/SettingsTeamsView.vue
  - frontend/src/components/TeamNotificationEndpoints.vue
  - backend/src/modulo/api/routes/teams.py
  - backend/src/modulo/api/routes/admin.py
  - backend/src/modulo/db/crud/team.py
  - backend/src/modulo/db/crud/team_membership.py
  - backend/src/modulo/auth/team_rbac.py
  - backend/src/modulo/db/models/notification_endpoint.py
depends-on: [feat-teams-team-crud, feat-teams-team-ownership]
status: partial
---

# Team Management UI

Discovered from 1 completed delivery tasks.

## Behaviours

### Team list

- [x] Admin can list all teams with name and description
- [x] Admin can see teams paginated (`/api/v1/admin/teams`)
- [x] Team list shows member count for each team
- [ ] Team list shows owned resource count for each team
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
- [ ] Bulk "Reassign all resources to org-wide" action before deletion (PRD requirement — not implemented)

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
- [ ] No guard against removing the last admin/operator from a team

### Remove member

- [x] Admin can remove a member from a team
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
- [ ] Team badge on pipeline/stage cards in Stage board with hover tooltip
- [ ] "My Teams" section in user profile panel

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
| Stage board honours team visibility | feat-teams-team-isolation | partial |
| Admin "View as: All / Team: X" toggle | feat-teams-team-isolation | partial |
| `view_as_team` server-enforced — non-admin receives 403 | feat-teams-team-isolation | partial |
| Team-scoped connectors enforced at pipeline-save time | feat-teams-team-ownership | partial |
| HITL gates with `required_team_id` enforced at claim time | feat-teams-team-hitl-gates | partial |
| SSO group-to-team mapping via claims (JIT provisioning) | feat-teams-sso-team-mapping | partial |

### Concurrency and edge cases

- [ ] Concurrent team rename by two admins not protected (no optimistic locking)
- [ ] Member add during team deletion not tested
- [ ] Race condition between role change and concurrent add/remove
- [ ] Self-removal from team not tested
- [ ] Concurrent notification endpoint CRUD with team deletion not tested

### Error Handling

- [x] All team endpoints catch `sqlalchemy.exc.ProgrammingError` and return 501 Not Implemented
- [x] Team not found returns 404 on all CRUD endpoints
- [x] Non-admin receives 403 on all team mutation endpoints
- [x] Team operator privilege cap enforced — cannot grant roles above their own
- [x] Resource conflict (team still owns resources) returns 409 with per-resource-type breakdown

## Known Gaps

- PRD 9.3 specifies email-based invitation flow (v1) — code uses direct add
- PRD 9.3 specifies owned resource count in team list — not shown in UI or backend response
- PRD 9.3 specifies bulk "Reassign all resources to org-wide" action — not implemented
- PRD 9.3 specifies team badge on pipeline/stage cards — not implemented
- PRD 9.3 specifies "My Teams" in user profile panel — not implemented
- `changeMemberRole` frontend uses POST /members (upsert) instead of the dedicated PATCH endpoint; backend has both
- No guard against removing the last admin/operator from a team (both backend and frontend)
- No audit event assertion in tests for `team_deleted`
- No concurrency tests — no coverage for concurrent rename, member add during deletion, role change race, or self-removal
- No optimistic locking on team rename
- `view_as_team` non-admin rejection tested in BDD but enforcement not verified in frontend
- `notification_endpoints` table has `team_id` FK but no RLS policy for team isolation at the notification level
