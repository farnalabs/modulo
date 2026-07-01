---
id: feat-teams-team-management-ui
prd: 9.3
delivery-tasks: [task-nv1-team-ui]
bdd: []
unit-tests: []
code:
  - frontend/src/views/SettingsTeamsView.vue
  - backend/src/modulo/api/routes/teams.py
  - backend/src/modulo/api/routes/admin.py
  - backend/src/modulo/db/crud/team.py
  - backend/src/modulo/db/crud/team_membership.py
  - backend/src/modulo/auth/team_rbac.py
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
- [x] Rename form shows loading state
- [x] Rename form shows error feedback
- [ ] Rename collisions (existing name) not tested in unit tests

### Delete team
- [x] Admin can delete an empty team (no owned resources)
- [x] Team deletion blocked with 409 if pipelines reference the team
- [x] Team not found returns 404
- [x] Non-admin receives 403
- [x] Delete confirmation dialog shown before proceeding
- [x] Delete shows loading state
- [x] Delete shows error feedback
- [ ] Bulk "Reassign all resources to org-wide" action before deletion (PRD requirement — not implemented)
- [ ] Team deletion writes `team_deleted` to AuditEvent

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
- [ ] Email-based invitation flow per PRD v1 spec (direct add implemented instead)

### Change member role
- [x] Admin can change a member's role via inline select dropdown
- [x] Role change uses POST /members (upsert pattern)
- [x] Error state shown on role change failure
- [x] Members reloaded on role change failure to ensure consistency
- [ ] Dedicated PATCH /members/{id} endpoint for role change
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
- [ ] Team-scoped API key enforcement (5.2)
- [ ] Team badge on pipeline/stage cards in Stage board with hover tooltip
- [ ] "My Teams" section in user profile panel

### Notification endpoints
- [ ] Team notification endpoint config in UI (PRD requirement)
- [ ] Notification endpoints stored on team entity

### Team-scoped visibility (cross-feature)
- [ ] Stage board honours team visibility (no resource enumeration for non-members)
- [ ] Admin "View as: All / Team: X" toggle
- [ ] view_as_team server-enforced — non-admin receives 403
- [ ] Team-scoped connectors enforced at pipeline-save time
- [ ] HITL gates with required_team_id enforced at claim time
- [ ] SSO group-to-team mapping via claims (JIT provisioning)

### Concurrency and edge cases
- [ ] Concurrent team rename by two admins not protected (no optimistic locking)
- [ ] Member add during team deletion not tested
- [ ] Race condition between role change and concurrent add/remove
- [ ] Self-removal from team not tested

## Known Gaps
- `delivery-tasks` correctly uses task IDs; no orphaned task-ID references in `depends-on` (verified: depends-on uses feat-* IDs)
- No BDD feature files exist for team management UI
- PRD 9.3 specifies email-based invitation flow (v1) — code uses direct add
- PRD 9.3 specifies owned resource count — not shown in UI
- PRD 9.3 specifies bulk "Reassign all resources to org-wide" action — not implemented
- PRD 9.3 specifies notification endpoint config — not in UI
- PRD 9.3 specifies team badge on pipeline/stage cards — not verified in scope
- PRD 9.3 specifies "My Teams" in user profile panel — not verified in scope
- `changeMemberRole` uses POST /members (upsert) instead of a dedicated PATCH endpoint
- `org_settings.feature` is a placeholder — no BDD coverage
- No guard against removing last admin/operator from a team
- No audit event assertion for team deletion 