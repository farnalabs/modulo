---
id: feat-teams-team-crud
prd: 9.3
delivery-tasks: [task-nv1-team-entity]
bdd:
  - backend/tests/bdd/features/teams/team_crud.feature
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
status: partial
---

# Team CRUD

## Behaviours

### Create team
- [x] Admin creates team with name â†’ 201, team returned with id
- [x] Admin creates team with name and description â†’ 201, description persisted
- [x] Admin creates team with name exceeding 255 chars â†’ 422
- [x] Admin creates team with empty name â†’ 422
- [x] Admin creates team with description exceeding 2000 chars â†’ 422
- [x] Admin creates team with duplicate name in same org â†’ 409
- [x] Admin creates team with name used in a different org â†’ 201 (name unique per org only)
- [x] Operator creates team â†’ 403
- [x] Runner creates team â†’ 403
- [x] Viewer creates team â†’ 403
- [x] Unauthenticated request creates team â†’ 401/403
- [x] account_id FK to accounts.id with RESTRICT â€” deleting a user who created teams is blocked
- [x] Response includes account_id field

### List teams
- [x] Authenticated user lists teams â†’ 200, paginated results
- [x] Org with no teams returns empty items array, total=0
- [x] Page parameter (default 1) respected
- [x] Page_size parameter (default 20, max 100) respected
- [x] Unauthenticated request lists teams â†’ 401/403
- [x] Non-admin lists teams via admin endpoint â†’ 403 (was incorrectly 400, fixed in 2026-07-09)
- [x] Results ordered by created_at ascending

### Get team
- [x] Authenticated user gets team by id â†’ 200 with full response
- [x] Non-existent team id â†’ 404
- [x] Team from another org (RLS isolation) â†’ 404 (not revealed)
- [x] Unauthenticated request gets team â†’ 401/403

### Update team
- [x] Admin updates team name â†’ 200, new name persisted
- [x] Admin updates team description â†’ 200, new description persisted
- [x] Admin updates both name and description â†’ 200, both changes applied
- [x] Admin sends empty name â†’ 422
- [x] Admin sends name exceeding 255 chars â†’ 422
- [x] Admin sends description exceeding 2000 chars â†’ 422
- [x] Admin updates to a name that already exists in the same org â†’ 409 (duplicate name check now implemented in admin route too)
- [x] Admin updates to a name that already exists in a different org â†’ 200 (per-org uniqueness)
- [x] Admin updates non-existent team â†’ 404
- [x] Admin can clear description by sending explicit null or empty string
- [x] Operator updates team â†’ 403
- [x] Unauthenticated request updates team â†’ 401/403
- [x] Immutable fields (id, organisation_id, created_at, updated_at) silently ignored in update

### Delete team
- [x] Admin deletes team with no owned resources â†’ 204
- [x] Admin deletes team that owns resources â†’ 409 with resource counts (pipelines, stages, connectors, model backends, library primitives)
- [x] Admin deletes non-existent team â†’ 404
- [x] Operator deletes team â†’ 403
- [x] Unauthenticated request deletes team â†’ 401/403
- [x] Team deletion with no owned resources â†’ team record removed
- [x] Cross-org isolation: deleting team in org A does not affect org B

### Membership â€” Add member
- [x] Admin adds user to team with valid role (viewer/runner/operator) â†’ 201
- [x] Admin adds user with role exceeding target user's org role â†’ 422
- [x] Team admin role not allowed for team membership (admin is org-only per 9.2)
- [x] Target user not found in org â†’ 404
- [x] Team not found â†’ 404
- [x] Duplicate membership (same team + same user) â†’ DB constraint violation â†’ 409
- [x] Invalid user_id format â†’ 422
- [x] Invalid role string â†’ 422
- [x] Operator adds member â†’ 403
- [x] Unauthenticated request â†’ 401/403

### Membership â€” List members
- [x] Authenticated user lists team members â†’ 200, paginated
- [x] Team with no members returns empty items array
- [x] Pagination parameters respected

### Membership â€” Remove member
- [x] Admin removes member â†’ 204
- [x] membership_id does not belong to the specified team â†’ 404
- [x] Non-existent membership_id â†’ 404
- [x] Operator removes member â†’ 403
- [x] Unauthenticated request â†’ 401/403
- [x] Removing last admin from team â€” allowed (no admin-preservation guard)

### Membership â€” Change role
- [x] Admin changes member role via PATCH â†’ 200, updated membership returned
- [x] Membership not found â†’ 404
- [x] Team not found â†’ 404
- [x] Invalid role string â†’ 422
- [x] Operator changes role â†’ 403 (when not team operator)
- [x] Unauthenticated request â†’ 401/403
- [x] Team operator can change roles up to their own role
- [x] Team operator cannot grant a role above their own

### Audit events
- [x] team_created audit event written on create (both /api/v1/teams and /api/v1/admin/teams)
- [x] team_updated audit event written on update (both routes)
- [x] team_deleted audit event written on delete (both routes)
- [x] Audit event includes org_id, actor_user_id, resource_type, resource_id, payload_json

### Deletion resource checks
- [x] Team deletion blocked when team owns pipelines
- [x] Team deletion blocked when team owns stages
- [x] Team deletion blocked when team owns connectors
- [x] Team deletion blocked when team owns model backends
- [x] Team deletion blocked when team owns library primitives
- [x] Team deletion proceeds when no resources owned â†’ 204

### Security & concurrency
- [x] RLS enforces org isolation on all team and membership queries
- [x] SET LOCAL app.organisation_id set before every query
- [x] set_rls_user_context called alongside set_rls_org on all admin team routes
- [x] Team name uniqueness enforced at DB level (UniqueConstraint)
- [x] Membership role constrained to valid values (CheckConstraint)
- [x] Membership uniqueness (team_id + account_id) enforced at DB level
- [x] cascade deletes: team deletion cascades to memberships (ondelete=CASCADE)
- [x] RESTRICT on account_id FK: prevents deleting user who created teams
- [x] Concurrent duplicate name creation handled (DB constraint catches)
- [x] Concurrent duplicate membership insertion handled (DB constraint catches)
- [x] Pagination avoids full-table scans (LIMIT/OFFSET with index on org_id)

### Error Handling (API routes)

- [x] ProgrammingError â†’ 501 on all 9 team/membership routes in `teams.py`
- [x] SQLAlchemyError â†’ 503 on all 9 team/membership routes in `teams.py`
- [x] Exception â†’ 500 (with structured logging) on all 9 team/membership routes in `teams.py`
- [x] IntegrityError â†’ 409 on create team route in `teams.py`
- [x] Admin create team (`admin.py`) now catches ProgrammingErrorâ†’501, SQLAlchemyErrorâ†’503, IntegrityErrorâ†’409, Exceptionâ†’500
- [x] Admin update team (`admin.py`) now catches ProgrammingErrorâ†’501, SQLAlchemyErrorâ†’503, Exceptionâ†’500
- [x] Admin delete team (`admin.py`) now catches ProgrammingErrorâ†’501, SQLAlchemyErrorâ†’503, Exceptionâ†’500
- [x] Admin list teams (`admin.py`) now catches ProgrammingErrorâ†’501, SQLAlchemyErrorâ†’503, Exceptionâ†’500
- [x] Admin create team duplicate name pre-check with `get_team_by_name` + integrity guard
- [x] Audit event failures caught (non-fatal) on all team mutation routes â€” team operation completes regardless of audit success

### Backward compatibility / data migration
- [x] Team entity is alpha-stage (no existing data to migrate)
- [x] OrgScoped base class consistent with all other entities
- [x] notification_endpoints JSON column defaults to empty list
- [x] daily_spend_limit nullable (default None)

## Known Gaps

- Membership add does not enforce privilege cap for non-admin grantors (PRD 9.3: a team operator can only grant roles up to their own team role â€” currently requires org admin)
- ~~Notification endpoints not exposed through REST API (field exists in model, no route)~~ â€” RESOLVED 2026-08-17: team-scoped notification endpoints are fully manageable via the `/api/v1/notifications` CRUD (`team_id` on create/update/response) and the admin `/api/v1/admin/notifications` CRUD (create/update now accept `team_id` with RLS-scoped existence validation â†’ 422 for unknown teams; responses echo it)
- ~~Daily spend limit not exposed through REST API~~ â€” RESOLVED 2026-08-17 (verified): team `daily_spend_limit` is readable/writable via `GET/PUT /api/v1/admin/costs/limits/teams/{team_id}` in `api/routes/costs.py`
- No integration tests for the membership privilege cap
- RLS isolation test (`test_teams_isolated_between_orgs`) still skipped â€” uses SET_CONFIG directly instead of the app-level RLS helpers

## QA History

### 2026-07-09 â€” Cross-cutting QA (improve-architecture index 281) â€” feat-teams-team-crud

**Lens:** Correctness, bugs, maintainability, SOLID/DRY, error handling, edge cases/resilience

**Fixed (CRITICAL):** Admin team routes (`admin_create_team`, `admin_update_team`, `admin_delete_team`) in `admin.py` had no try/except error handling â€” `ProgrammingError`, `SQLAlchemyError`, `IntegrityError`, and Python-level errors (`TypeError`, `KeyError`, `ValueError`) propagated as raw 500 to CatchAllMiddleware. Added comprehensive guards (ProgrammingErrorâ†’501, SQLAlchemyErrorâ†’503, IntegrityErrorâ†’409, HTTPExceptionâ†’re-raise, Exceptionâ†’500) to all 3 routes.

**Fixed (CRITICAL):** `admin_create_team` in `admin.py` missing duplicate name pre-check â€” creating a team with a duplicate name hit an uncaught IntegrityError (raw 500). Added `get_team_by_name` pre-check (409 Conflict) plus `except IntegrityError â†’ 409` safety net.

**Fixed (MAJOR):** `admin_list_teams` in `admin.py` returned 400 for non-admin users instead of 403. Changed to `HTTP_403_FORBIDDEN`.

**Fixed (MAJOR):** `admin_list_teams` in `admin.py` had no `except SQLAlchemyError â†’ 503` â€” connection/deadlock failures returned 500. Added the catch with structured logging.

**Fixed (MAJOR):** `admin_update_team` and `admin_create_team` audit event blocks had no error handling â€” audit failures propagated after the team was already mutated. Added `except ProgrammingError`/`except SQLAlchemyError` catching.

**All tests pass:** 30 API endpoint + 22 CRUD unit tests = 52/52 pass. No regressions.

### 2026-07-09 â€” Cross-cutting QA (index 353) â€” feat-teams-team-crud

**Lens:** Correctness, maintainability, error handling, edge cases

**Fixed (MAJOR):** Added `set_rls_user_context` calls to all 4 admin team routes in `admin.py` and their audit event blocks â€” `admin_create_team`, `admin_list_teams`, `admin_update_team`, `admin_delete_team`. Previously only `set_rls_org` was set, missing user identity context for RLS policies and tenant-filter listener.

**Fixed (MINOR):** Flattened nested try/except in `admin_create_team` â€” inner `try/except ProgrammingError` was redundant with outer `except ProgrammingError`; consolidated and added `logger.exception` to the outer handler.

**All tests pass:** 30 API endpoint + 22 CRUD unit tests + 4 admin RLS context tests = 56/56 pass. No regressions.

### 2026-08-17 â€” improve-architecture (feat-core-notifications cross-entry)

**RESOLVED 2 Known Gaps:**
- **Notification endpoints not exposed through REST API** â€” the admin `/api/v1/admin/notifications` CRUD now accepts `team_id` on create/update (RLS-scoped existence validation â†’ 422 `Unknown team id` for missing/soft-deleted/other-org teams, closing the FK-`IntegrityError`â†’503 rough edge), echoes `team_id` on every response, and the non-admin `/api/v1/notifications` CRUD already surfaced the field. 9 new endpoint tests in `test_admin_notifications_webhooks.py`.
- **Daily spend limit not exposed through REST API** â€” verified resolved: `GET/PUT /api/v1/admin/costs/limits/teams/{team_id}` (`api/routes/costs.py:373`) reads/writes `Team.daily_spend_limit`.
