---
id: feat-teams
prd: N/A
adr: []
code:
  - backend/src/modulo/api/routes/teams.py
  - backend/src/modulo/auth/team_rbac.py
  - backend/src/modulo/core/team_visibility.py
  - backend/src/modulo/core/capability_scope.py
  - backend/src/modulo/api/routes/viewmodel.py
  - backend/src/modulo/db/crud/team_scope.py
  - backend/src/modulo/db/models/team.py
  - backend/src/modulo/db/migrations/versions/0287_team_rls_lifecycle_evals.py
unit-tests:
  - backend/tests/unit/api/test_teams.py
  - backend/tests/unit/api/test_teams_routes_coverage.py
  - backend/tests/unit/auth/test_team_rbac.py
  - backend/tests/unit/auth/test_team_scope_dependencies.py
  - backend/tests/unit/core/test_team_visibility.py
  - backend/tests/unit/db/crud/test_team.py
  - backend/tests/unit/db/crud/test_team_membership.py
  - backend/tests/unit/db/crud/test_team_scope.py
  - backend/tests/unit/mcp/test_team_scope_enforcement.py
  - backend/tests/integration/test_rls_isolation.py
  - backend/tests/unit/db/test_migration_team_visibility_rls.py
  - backend/tests/architecture/test_team_scope_wiring.py
bdd:
  - backend/tests/bdd/features/teams/team_create.feature
  - backend/tests/bdd/features/teams/team_crud.feature
  - backend/tests/bdd/features/teams/team_membership.feature
  - backend/tests/bdd/features/teams/team_deletion.feature
  - backend/tests/bdd/features/teams/team_deletion_blocked.feature
  - backend/tests/bdd/features/teams/team_hitl_review.feature
  - backend/tests/bdd/features/teams/cross_team_isolation.feature
  - backend/tests/bdd/features/teams/team_pipeline_visibility.feature
  - backend/tests/bdd/features/teams/view_as_team.feature
  - backend/tests/bdd/features/users/roles.feature
  - backend/tests/bdd/steps/test_team_create.py
  - backend/tests/bdd/steps/test_team_crud.py
  - backend/tests/bdd/steps/test_team_membership.py
  - backend/tests/bdd/steps/test_team_deletion.py
  - backend/tests/bdd/steps/test_team_deletion_blocked.py
  - backend/tests/bdd/steps/test_team_hitl_review.py
  - backend/tests/bdd/steps/test_cross_team_isolation.py
  - backend/tests/bdd/steps/test_team_pipeline_visibility.py
  - backend/tests/bdd/steps/test_view_as_team.py
  - backend/tests/bdd/steps/test_alpha_users.py
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
      duplicate), delete 204 for a team with no owned resources (`team_crud.feature`)
- [x] Membership: an admin or the team operator adds/removes members with a role, a user
      cannot be granted a team role above their org role (422 "exceeds"), duplicate
      membership is 409, adding to a missing team is 404, and the profile lists memberships
      with team id and role (`team_membership.feature`)
- [x] Deletion safety: deleting a team that still owns resources (pipelines, connectors,
      model backends, library primitives) is blocked with 409 (`team_has_resources`) and
      the per-resource counts in the error
      (`tests/unit/api/test_teams_routes_coverage.py`, driving the real route); deletion is
      a soft delete (`deleted_at`), so memberships are not cascade-deleted; delete with no
      owned resources is 204, non-admin delete is 403 and a missing team is 404
      (`team_crud.feature`, `team_deletion.feature`, `team_deletion_blocked.feature`)
- [x] Cross-team isolation: a team cannot see or enumerate another team's team-scoped
      pipelines (404 / omitted from list counts), cross-team connector binding is refused
      as `connector_team_mismatch`, org-wide resources stay shared, and there is no
      "N hidden" enumeration leak (`cross_team_isolation.feature`). The sharing rule is
      explicit (FAR-1618): teams are a VISIBILITY GROUPING, not a credential trust
      boundary. `visibility: org` means shared across the organisation — it binds to
      ANY pipeline, including one owned by a team (an org-wide connector, model backend
      or environment profile never produces a mismatch). `visibility: team` means
      owner-team-only — it binds only to a pipeline owned by the same team, and a
      different team's pipeline (or an org pipeline) is refused at every write path
      that can create the binding. Both directions are the same rule for connectors,
      model backends and environment profiles, but each resource type reports its own
      machine-readable code: `connector_team_mismatch`, `model_backend_team_mismatch`,
      `environment_profile_team_mismatch` / `environment_profile_binding_team_mismatch`
      (`core/team_visibility.py`)
- [x] Team-scoped pipeline visibility and the view-as-team admin flows are enforced
      (`team_pipeline_visibility.feature`, `view_as_team.feature`)
- [x] RBAC roles (`admin | operator | runner | viewer`) gate team surfaces
      (`users/roles.feature`, `auth/team_rbac.py`, `test_team_rbac.py`)
- [x] DB team RLS covers every team-scoped table (FAR-1514):
      `lifecycle_maps`, `eval_datasets` and `eval_suites` joined the five 0124
      tables — migration `0287_team_rls_lifecycle_evals` drops their org-only
      `rls_org_isolation` policy and creates the shared `rls_team_isolation`
      policy carrying the full visibility matrix (org AND — `visibility='org'`
      / NULL / no owner team / `owner_team_id IN my team_memberships` /
      org admin / `app.execution_context='true'`). Postgres ORs permissive
      policies, so a lone org policy beside a team policy made the team policy
      dead weight (the 0124 cross-team leak); the org check stays an AND gate,
      so the execution-context escape hatch only widens the team clause WITHIN
      the org. Background machinery (executor, cron, SAQ suite-run dispatch,
      housekeeping, seed) sets `app.execution_context` and keeps reading
      team-private rows; the FAR-1515 team-scope seams (`team_blind_org_scope`,
      `pipeline_team_scope_team_blind`) widen on the request path for the duration of
      a scoped gate check then restore the caller's context, so user-facing list/read
      paths stay team-filtered.
      The four core-table resolvers (connectors / model_backends /
      environment_profiles / library_primitives) stay INTENTIONALLY unwired from
      route dependencies — their DB RLS alone 404s a non-member before handler
      code runs, so extra request-time gates would be redundant transactions.
      Policy-DDL only, existence-guarded (idempotent), Postgres-only
      (`tests/integration/test_rls_isolation.py`,
      `tests/unit/db/test_migration_team_visibility_rls.py`,
      `tests/architecture/test_team_scope_wiring.py`)

## Known Gaps

- **HITL review ownership (`team_hitl_review.feature`) is cited under the hitl feature graph
  edge, not deeply here** — this entry cites the BDD coverage; gate-claim semantics live in
  `feat-hitl`.
- **`stale_jwt_revocation.feature` and `admin_override.feature`** exercise JWT/override
  surfaces under the teams BDD directory that are not cited by this entry's behaviours
  (they belong to the auth/JWT feature edges).
- **`team_deletion.feature` and `team_deletion_blocked.feature` scenarios are mocked** —
  both patch `delete_team` with client-constructed 409s, so neither exercises the shipped
  resource guard (the real 409 is raised by the route's owned-resource count loop); the
  active-run-blocking scenarios in `team_deletion.feature` describe a guard that does not
  ship (the route blocks on owned resources — see the Deletion safety behaviour above).
  The shipped block is covered by the unit test
  `test_delete_team_with_owned_resources_returns_409`
  (`tests/unit/api/test_teams_routes_coverage.py`), which drives the real route to a 409
  `team_has_resources` carrying the per-resource count.

## QA History
- 2026-10-08: **Improve Architecture product-map walk** – closed the untracked
  FAR-1514 sub-surface (DB team RLS for `lifecycle_maps`, `eval_datasets` and
  `eval_suites`, merged in PR #1378): the last neither-layer tables gained the
  `rls_team_isolation` policy while the manifest `feat-teams` registry and this
  tracker still described only the application-layer isolation. Added the
  checked behaviour line plus the migration / integration / architecture-test
  citations.
- 2026-09-25: **Improve Architecture product-map walk** — reconciled the
  manifest `feat-teams` registry entry with this tracker (both now
  `status: covered`): the "team-level resource scoping partially wired" unchecked
  item from #972 is closed against the shipped cross-team isolation /
  team-pipeline-visibility / view-as-team surfaces verified below
  (`core/team_visibility.py`, `auth/team_rbac.py`, the three BDD feature files).
  Org-membership/entity lifecycle stays owned by `feat-teams-org-entity`.
- 2026-09-12: **product-map review pass** — registered the shared
  plan-entitlement gate surface (`components/FeatureGate.vue` + `LockIcon.vue` static
  testids `feature-gate*` / `lock-icon`) in the manifest `elements:` inventory for `/admin/users`, `/settings/teams`
  and wired the two components into the reverse testid-coverage guard
  (`test_mapped_route_elements_cover_owning_view_testids`), so the entitlement-card
  surface on those pages stays visible to Assistant's docs indexer / `/api/v1/manifest` and
  can no longer drift unguarded.

- 2026-09-11: **product-map review pass** — extended the reverse
  testid-coverage guard (`test_mapped_route_elements_cover_owning_view_testids`) to
  `/settings/teams`: the whole-page view(s) `SettingsTeamsView.vue` render static `data-testid`s that the
  product map `elements:` inventory already documents, but the surface was not yet
  guarded against drift. The route now maps to its owning view so a newly shipped
  testid can no longer silently stay invisible to Assistant's docs indexer /
  `/api/v1/manifest`.

- 2026-08-27: **product-map review pass** — added this behaviour-tracker
  for the registered manifest feature `feat-teams`, which previously had no
  `docs/product-map/` entry. Behaviours verified against `api/routes/teams.py`,
  `auth/team_rbac.py`, `core/team_visibility.py` and the teams BDD/unit suites.
  Status: covered.
