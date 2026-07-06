---
id: feat-teams-team-isolation
prd: 9.3
delivery-tasks: [task-nv1-team-isolation]
bdd:
  - backend/tests/bdd/features/auth/tenant_isolation.feature
  - backend/tests/bdd/features/security/rls_enforcement.feature
  - backend/tests/features/organisation/rls_isolation.feature
  - backend/tests/features/teams/cross_team_isolation.feature
  - backend/tests/features/teams/view_as_team.feature
  - backend/tests/features/teams/view_as_team_non_admin_rejected.feature
code:
  - backend/src/modulo/db/rls.py
  - backend/src/modulo/db/crud/base.py
  - backend/src/modulo/db/crud/team.py
  - backend/src/modulo/db/crud/team_membership.py
  - backend/src/modulo/db/models/base.py
  - backend/src/modulo/db/models/team.py
  - backend/src/modulo/db/models/team_membership.py
  - backend/src/modulo/db/migrations/versions/0002_rls_policies.py
  - backend/src/modulo/db/migrations/versions/0025_team_visibility_rls.py
unit-tests:
  - backend/tests/integration/test_rls_isolation.py
  - backend/tests/integration/test_cross_tenant_isolation.py
  - backend/tests/integration/crud/test_team_isolation.py
depends-on: [feat-teams-team-crud]
status: partial
---
# Team Isolation (RLS)

RLS (Row-Level Security) and tenant-scoping layer that prevents cross-organisation
and cross-team data leaks. Uses Postgres `set_config` / `SET LOCAL` for org context
and a pool-checkout reset hook for defense-in-depth. Non-Postgres backends get
equivalent filtering via an ORM `do_orm_execute` listener. Team-visibility RLS
(`rls_team_isolation`) additionally restricts team-scoped resources to team members.

## Behaviours

### Happy Paths

- [x] Org A user sees only Org A's resources — cross-org data is invisible across pipelines, agents, schemas, and connector instances
- [x] Org admin sees all resources within their org regardless of team membership
- [x] Team member sees resources with `visibility: team` owned by their team
- [x] User sees resources with `visibility: org` irrespective of team membership
- [x] `set_rls_org` applies the org context within an active transaction (used in all route handlers and MCP `_session()` context)
- [x] `set_rls_user_context` sets `user_id` and `org_role` for team-scoped RLS evaluation (called in MCP `_session()` context at mcp_server.py:94-96 and in admin routes; not yet universal across all API route handlers)
- [x] Pool checkout hook resets `app.organisation_id`, `app.user_id`, `app.org_role` to the empty-string sentinel
- [x] ORM `do_orm_execute` listener injects `WHERE organisation_id = :oid` for non-Postgres backends
- [x] All 21 org-scoped tables receive `rls_org_isolation` policy on migration 0002
- [x] Five team-scoped tables (pipelines, stages, connector_instances, model_backends, library_primitives) receive `rls_team_isolation` policy on migration 0025

### Edge Cases

- [x] User not in any team sees only org-visibility resources — no team-private leakage
- [x] User in multiple teams sees each team's resources independently with their respective team roles
- [x] Resource with `owner_team_id=NULL` and `visibility=org` (legacy/unowned) accessible to all org members (DB CHECK constraint allows this)
- [x] Resource with `owner_team_id=NULL` and `visibility=team` is blocked by DB check constraint (migration 0001 enforces)
- [x] Empty org returns empty lists from all CRUD functions without error (RLS returns zero rows)
- [x] User removed from team loses access to that team's resources at next token refresh (JWT) or immediately (DB-live HITL check — confirmed in hitl_manager)
- [x] `set_config(is_local=true)` reverts to the session-level empty string after COMMIT or ROLLBACK
- [x] Second transaction on the same pooled connection starts without stale org context
- [x] Org role does not override team visibility — an org-level `operator` outside the owning team cannot see team-private resources

### Error States

- [x] Calling `set_rls_org` outside an active transaction raises `RuntimeError`
- [x] Cross-org pipeline fetch by ID returns `None` (RLS filters the row) — not an error
- [x] Cross-org pipeline run POST returns 404 (resource non-existence, not 403)
- [x] Non-admin using `view_as_team` parameter returns 403
- [x] Team deletion blocked (`team_has_resources` error) when owned resources exist
- [ ] Connector binding across teams returns `connector_team_mismatch` error (BDD scenario exists but real enforcement not implemented)
- [x] Team member grant with role exceeding the granting user's role is blocked (privilege escalation prevention)
- [x] HITL gate with `required_team_id` — non-member attempting claim raises `NotTeamMemberError` (DB-live check in hitl_manager)

### Security

- [x] RLS `rls_org_isolation` policy exists on every org-scoped table (migration 0002)
- [x] `nullif(current_setting('app.organisation_id', true), '')::uuid` converts missing/empty context to NULL — no rows visible when org context is unset
- [x] `set_config(is_local=true)` prevents org_id leakage across transactions (proven by integration tests)
- [x] Pool checkout hook sets all three session vars to empty string — defense-in-depth against stale context
- [x] FORCE ROW LEVEL SECURITY intentionally omitted — relies on non-superuser app connection role (infrastructure responsibility)
- [x] ORM tenant filter (`_inject_tenant_filter`) covers SELECT, UPDATE, DELETE for non-Postgres backends
- [x] `team_memberships` table itself is org-scoped (inherits `OrgScoped`) — memberships are isolated per tenant
- [x] HITL gate `required_team_id` enforcement uses DB-live membership check — JWT claims not trusted for this path (confirmed in hitl_manager)
- [x] `rls_team_isolation` policy checks `current_setting('app.org_role') = 'admin'` so admins bypass team scoping
- [x] Cross-org resource enumeration by ID is not possible — non-owned IDs return `None` / 404, not 403

### Concurrency

- [x] Two concurrent transactions on different orgs do not interfere — `set_config` is per-backend-connection and `is_local=true` scopes to the transaction
- [x] Advisory locks are org-scoped — different orgs can lock the same pipeline name concurrently
- [x] Connection pool checkout with reset hook prevents context bleed across requests sharing a connection
- [x] `register_rls_reset_hook` is safe to call once at engine init — uses `@event.listens_for` which supports multiple engines

### Backward Compatibility

- [x] Existing 200+ CRUD functions and 30+ route handlers work unchanged with RLS applied
- [x] Non-Postgres backends (MariaDB, SQLite) receive equivalent tenant filtering via ORM listener — zero code changes in CRUD (confirmed in test_rls_isolation.py)
- [x] Legacy resources (`owner_team_id=NULL`, `visibility=org`) remain fully accessible to all org members
- [x] `set_rls_user_context` is additive — all existing `set_rls_org` callers continue to work
- [x] BDD test patches for `set_rls_org` continue working alongside new user context function (confirmed in rls_enforcement.feature)
- [x] Existing API responses unchanged — RLS filtering is invisible to the client (fewer rows, same schema)

### Migration Column Rename

- [x] `rls_team_isolation` policy correctly references `team_memberships.account_id` (post-migration 0060 fix)
- [x] `test_team_memberships_isolated_by_org_rls` verifies team membership isolation across teams within the same org

### API Response Inconsistencies

- [x] `MembershipResponse.user_id` returns the value of `account_id` column (cosmetic — field name mismatches column name)

## Known Gaps

- **connector_team_mismatch not implemented in backend code** — BDD scenario exists at `tests/features/teams/cross_team_isolation.feature` but is mocked (MagicMock). Real enforcement at pipeline-save time (checking connector.owner_team_id vs pipeline.owner_team_id) does not exist yet.

## QA History

### 2026-07-08 — Cross-cutting QA (improve-architecture index 255)

**Findings fixed:**
- CRITICAL — `test_cross_tenant_isolation.py` `_seed_user()` inserted into non-existent `users` table (migration 0074 renamed to `accounts`+`org_memberships`). Rewrote to insert into `accounts` + `org_memberships` with correct columns. 7 integration tests (org data isolation, system admin access, cross-org admin, system admin explicit org param) were silently broken — they'd crash at fixture setup with `UndefinedTable` before any assertion ran.
- CRITICAL — `test_teams_isolated_between_orgs` in `test_team_isolation.py` was `@pytest.mark.skip` with reason "RLS isolation needs investigation". Fixed `_create_user` helper to use `accounts`+`org_memberships` (was using old `accounts` table with `organisation_id` column that doesn't exist). Converted all `session.flush()` patterns to `session.begin()` for proper transaction scoping. Un-skipped the test.
- MAJOR — `test_team_isolation.py` used bare `session.flush()` without `session.begin()` on 5 test functions (team_name_unique_per_org, memberships_isolated_between_orgs, membership_unique_per_team_user, crud_round_trip, membership_round_trip). `set_config(is_local=true)` only works inside an active transaction — `flush()` does not begin a transaction. Converted all to `async with session.begin():`.
- MAJOR — Added `test_get_other_org_pipeline_by_id_returns_404` and `test_get_own_org_pipeline_by_id_succeeds` to `test_cross_tenant_isolation.py` — cross-org single-resource fetch test covering the RLS 404-for-None pattern (previously only list-scoped isolation was tested).
- MAJOR — Added `test_team_memberships_isolated_by_org_rls` to `test_rls_isolation.py` — verifies team membership isolation across teams within the same org: creates accounts in different teams, confirms each account can only see their own team's members, and admin sees all.

**Product map updates:**
- Marked Migration Column Rename `[ ]` → `[x]` for RLS policy correctness test.
- Added `test_team_isolation.py` to `unit-tests:` frontmatter.
- Removed 2 resolved Known Gaps (cross-org single-resource fetch, Migration 0060 not tested).
- Removed stale Known Gap about `list_members_endpoint` using `m.user_id` — the endpoint correctly uses `m.account_id` mapped to `user_id` response field name.

**Status:** partial (1 known gap remains — connector_team_mismatch not implemented in backend code).
