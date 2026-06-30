---
id: feat-teams-team-crud
prd: 9.3
delivery-tasks: [task-nv1-team-entity]
bdd:
  - backend/tests/features/teams/team_crud.feature
code:
  - backend/src/modulo/db/models/team.py
  - backend/src/modulo/db/models/team_membership.py
  - backend/src/modulo/db/crud/team.py
  - backend/src/modulo/db/crud/team_membership.py
  - backend/src/modulo/api/routes/teams.py
  - backend/src/modulo/api/routes/admin.py
unit-tests:
  - backend/tests/unit/api/test_teams.py
  - backend/tests/unit/db/crud/test_team.py
  - backend/tests/unit/db/crud/test_team_membership.py
  - backend/tests/integration/crud/test_team_isolation.py
depends-on: [feat-teams-org-entity]
status: covered
---

# Team CRUD

## Behaviours

### Create team
- [x] Admin creates team with name → 201, team returned with id
- [x] Admin creates team with name and description → 201, description persisted
- [x] Admin creates team with name exceeding 255 chars → 422
- [x] Admin creates team with empty name → 422
- [x] Admin creates team with description exceeding 2000 chars → 422
- [x] Admin creates team with duplicate name in same org → 409
- [x] Admin creates team with name used in a different org → 201 (name unique per org only)
- [x] Operator creates team → 403
- [x] Runner creates team → 403
- [x] Viewer creates team → 403
- [x] Unauthenticated request creates team → 401/403
- [x] account_id FK to accounts.id with RESTRICT — deleting a user who created teams is blocked
- [x] Response includes account_id field

### List teams
- [x] Authenticated user lists teams → 200, paginated results
- [x] Org with no teams returns empty items array, total=0
- [x] Page parameter (default 1) respected
- [x] Page_size parameter (default 20, max 100) respected
- [x] Unauthenticated request lists teams → 401/403
- [x] Results ordered by created_at ascending

### Get team
- [x] Authenticated user gets team by id → 200 with full response
- [x] Non-existent team id → 404
- [x] Team from another org (RLS isolation) → 404 (not revealed)
- [x] Unauthenticated request gets team → 401/403

### Update team
- [x] Admin updates team name → 200, new name persisted
- [x] Admin updates team description → 200, new description persisted
- [x] Admin updates both name and description → 200, both changes applied
- [x] Admin sends empty name → 422
- [x] Admin sends name exceeding 255 chars → 422
- [x] Admin sends description exceeding 2000 chars → 422
- [x] Admin updates to a name that already exists in the same org → 409 (duplicate name check now implemented)
- [x] Admin updates to a name that already exists in a different org → 200 (per-org uniqueness)
- [x] Admin updates non-existent team → 404
- [x] Operator updates team → 403
- [x] Unauthenticated request updates team → 401/403
- [x] Immutable fields (id, organisation_id, created_at, updated_at) silently ignored in update

### Delete team
- [x] Admin deletes team with no owned resources → 204
- [x] Admin deletes team that owns resources → 409 with pipeline count (pipelines checked; stages, connectors, model backends not yet checked)
- [x] Admin deletes non-existent team → 404
- [x] Operator deletes team → 403
- [x] Unauthenticated request deletes team → 401/403
- [x] Team deletion with no owned resources → team record removed
- [x] Cross-org isolation: deleting team in org A does not affect org B

### Membership — Add member
- [x] Admin adds user to team with valid role (viewer/runner/operator) → 201
- [x] Admin adds user with role exceeding target user's org role → 422
- [x] Team admin role not allowed for team membership (admin is org-only per 9.2)
- [x] Target user not found in org → 404
- [x] Team not found → 404
- [x] Duplicate membership (same team + same user) → DB constraint violation → 409
- [x] Invalid user_id format → 422
- [x] Invalid role string → 422
- [x] Operator adds member → 403
- [x] Unauthenticated request → 401/403

### Membership — List members
- [x] Authenticated user lists team members → 200, paginated
- [x] Team with no members returns empty items array
- [x] Pagination parameters respected

### Membership — Remove member
- [x] Admin removes member → 204
- [x] membership_id does not belong to the specified team → 404
- [x] Non-existent membership_id → 404
- [x] Operator removes member → 403
- [x] Unauthenticated request → 401/403
- [x] Removing last admin from team — allowed (no admin-preservation guard)

### Security & concurrency
- [x] RLS enforces org isolation on all team and membership queries
- [x] SET LOCAL app.organisation_id set before every query
- [x] Team name uniqueness enforced at DB level (UniqueConstraint)
- [x] Membership role constrained to valid values (CheckConstraint)
- [x] Membership uniqueness (team_id + user_id) enforced at DB level
- [x] cascade deletes: team deletion cascades to memberships (ondelete=CASCADE)
- [x] RESTRICT on account_id FK: prevents deleting user who created teams
- [x] Concurrent duplicate name creation handled (DB constraint catches)
- [x] Concurrent duplicate membership insertion handled (DB constraint catches)
- [x] Pagination avoids full-table scans (LIMIT/OFFSET with index on org_id)

### Backward compatibility / data migration
- [x] Team entity is alpha-stage (no existing data to migrate)
- [x] OrgScoped base class consistent with all other entities
- [x] notification_endpoints JSON column defaults to empty list
- [x] daily_spend_limit nullable (default None)

## Known Gaps

- Team deletion checks for pipelines (admin.py:824-832) but does NOT check for stages, connectors, model backends, or library primitives — partial implementation of PRD 9.3 deletion policy
- Membership add does not enforce privilege cap for non-admin grantors (PRD 9.3: a team operator can only grant roles up to their own team role — currently requires org admin)
- Notification endpoints not exposed through REST API (field exists in model, no route)
- Daily spend limit not exposed through REST API
- No audit events written for any team CRUD mutation
- `admin.py` team update route (PUT /api/v1/admin/teams/{team_id}) does NOT check for duplicate name when renaming — only the /api/v1/teams route has the check
- Cannot clear description via PATCH (None values are filtered out in update endpoint)
- Membership `update_member_role` CRUD function exists but is NOT exposed via any REST route or admin route
- Integration tests for team isolation are skipped (marked with `@pytest.mark.skip(reason="awaiting-implementation")`)
- No integration tests for the duplicate name update fix or the membership privilege cap
