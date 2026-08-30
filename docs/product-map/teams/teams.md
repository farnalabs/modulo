---
id: feat-teams
prd: N/A
adr: []
code:
  - backend/src/modulo/api/routes/teams.py
  - backend/src/modulo/auth/team_rbac.py
  - backend/src/modulo/core/team_visibility.py
  - backend/src/modulo/core/capability_scope.py
  - backend/src/modulo/api/routes/me.py
  - backend/src/modulo/db/models/team.py
unit-tests:
  - backend/tests/unit/api/test_teams.py
  - backend/tests/unit/auth/test_team_rbac.py
  - backend/tests/unit/auth/test_team_scope_dependencies.py
  - backend/tests/unit/core/test_team_visibility.py
  - backend/tests/unit/db/crud/test_team.py
  - backend/tests/unit/db/crud/test_team_membership.py
bdd:
  - backend/tests/bdd/features/teams/team_create.feature
  - backend/tests/bdd/features/teams/team_crud.feature
  - backend/tests/bdd/features/teams/team_membership.feature
  - backend/tests/bdd/features/teams/team_deletion.feature
  - backend/tests/bdd/features/teams/team_deletion_blocked.feature
  - backend/tests/bdd/features/teams/team_hitl_gate.feature
  - backend/tests/bdd/features/teams/cross_team_isolation.feature
  - backend/tests/bdd/features/teams/team_pipeline_visibility.feature
  - backend/tests/bdd/features/teams/view_as_team.feature
  - backend/tests/bdd/features/users/roles.feature
  - backend/tests/bdd/steps/test_team_crud.py
  - backend/tests/bdd/steps/test_team_membership.py
  - backend/tests/bdd/steps/test_team_deletion.py
  - backend/tests/bdd/steps/test_team_deletion_blocked.py
  - backend/tests/bdd/steps/test_team_hitl_gate.py
  - backend/tests/bdd/steps/test_cross_team_isolation.py
  - backend/tests/bdd/steps/test_team_pipeline_visibility.py
  - backend/tests/bdd/steps/test_view_as_team.py
  - backend/tests/bdd/steps/test_auth_rbac.py
depends-on:
  - feat-auth
  - feat-teams-org-entity
status: covered
---

# Users, Teams, and Role-Based Access

Teams scope org members (admin | operator | runner | viewer) to team-owned resources,
with RBAC enforced by `auth/team_rbac.py` and row/visibility scoping by
`core/team_visibility.py` plus DB-level owner-team columns. The feature powers the
`/settings/teams` and `/admin/users` surfaces plus the team memberships shown on the
org profile, and is the product-map home for user roles.

## Behaviours

- [x] Team CRUD: create with name/description (201), duplicate name 409, empty name 422,
      non-admin create 403, paginated list, get by id (404 when missing), rename (409 on
      duplicate), delete 204 for a non-admin-owned team (`team_crud.feature`)
- [x] Membership: an admin or the team operator adds/removes members with a role, a user
      cannot be granted a team role above their org role (422 "exceeds"), duplicate
      membership is 409, adding to a missing team is 404, and profile lists memberships
      with team id/name/role (`team_membership.feature`)
- [x] Deletion safety: deleting a team with active runs is blocked with 409 and the
      active-run count in the error; memberships cascade-clean on deletion; non-admin
      delete is 403 and a missing team is 404 (`team_deletion.feature`,
      `team_deletion_blocked.feature`)
- [x] Cross-team isolation: a team cannot see or enumerate another team's team-scoped
      pipelines (404 / omitted from list counts), cross-team connector binding is refused
      as `connector_team_mismatch`, org-wide resources stay shared, and there is no
      "N hidden" enumeration leak (`cross_team_isolation.feature`)
- [x] Team-scoped pipeline visibility and the view-as-team admin flows are enforced
      (`team_pipeline_visibility.feature`, `view_as_team.feature`)
- [x] RBAC roles (`admin | operator | runner | viewer`) gate team surfaces
      (`users/roles.feature`, `auth/team_rbac.py`, `test_team_rbac.py`)

## Known Gaps

- **HITL gate ownership (`team_hitl_gate.feature`) is cited under the hitl feature graph
  edge, not deeply here** — this entry cites the BDD coverage; gate-claim semantics live in
  `feat-hitl`.
- **`stale_jwt_revocation.feature` and `admin_override.feature`** exercise JWT/override
  surfaces under the teams BDD directory that are not cited by this entry's behaviours
  (they belong to the auth/JWT feature edges).

## QA History

- 2026-08-27: **improve-architecture (product-map walk)** — added this behaviour-tracker
  for the registered manifest feature `feat-teams`, which previously had no
  `docs/product-map/` entry. Behaviours verified against `api/routes/teams.py`,
  `auth/team_rbac.py`, `core/team_visibility.py` and the teams BDD/unit suites.
  Status: covered.
