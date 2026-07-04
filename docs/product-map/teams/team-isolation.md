---
id: feat-teams-team-isolation
prd: 9.3
delivery-tasks: [task-nv1-team-isolation]
bdd:
  - backend/tests/bdd/features/auth/tenant_isolation.feature
  - backend/tests/bdd/features/security/rls_enforcement.feature
  - backend/tests/features/organisation/rls_isolation.feature
  - backend/tests/integration/test_cross_tenant_isolation.py
  - backend/tests/integration/test_rls_isolation.py
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
- [ ] Resource with `owner_team_id=NULL` and `visibility=org` (legacy/unowned) accessible to all org members
- [ ] Resource with `owner_team_id=NULL` and `visibility=team` is blocked by DB check constraint
- [ ] Empty org returns empty lists from all CRUD functions without error
- [ ] User removed from team loses access to that team's resources at next token refresh (JWT) or immediately (DB-live HITL check)
- [x] `set_config(is_local=true)` reverts to the session-level empty string after COMMIT or ROLLBACK
- [x] Second transaction on the same pooled connection starts without stale org context
- [x] Org role does not override team visibility — an org-level `operator` outside the owning team cannot see team-private resources

### Error States

- [x] Calling `set_rls_org` outside an active transaction raises `RuntimeError`
- [x] Cross-org pipeline fetch by ID returns `None` (RLS filters the row) — not an error
- [x] Cross-org pipeline run POST returns 404 (resource non-existence, not 403)
- [x] Non-admin using `view_as_team` parameter returns 403
- [x] Team deletion blocked (`team_has_resources` error) when owned resources exist
- [ ] Connector binding across teams returns `connector_team_mismatch` error
- [x] Team member grant with role exceeding the granting user's role is blocked (privilege escalation prevention)
- [ ] HITL gate with `required_team_id` — non-member attempting claim returns 403

### Security

- [x] RLS `rls_org_isolation` policy exists on every org-scoped table (migration 0002)
- [x] `nullif(current_setting('app.organisation_id', true), '')::uuid` converts missing/empty context to NULL — no rows visible when org context is unset
- [x] `set_config(is_local=true)` prevents org_id leakage across transactions (proven by integration tests)
- [x] Pool checkout hook sets all three session vars to empty string — defense-in-depth against stale context
- [x] FORCE ROW LEVEL SECURITY intentionally omitted — relies on non-superuser app connection role (infrastructure responsibility)
- [x] ORM tenant filter (`_inject_tenant_filter`) covers SELECT, UPDATE, DELETE for non-Postgres backends
- [x] `team_memberships` table itself is org-scoped (inherits `OrgScoped`) — memberships are isolated per tenant
- [ ] HITL gate `required_team_id` enforcement uses DB-live membership check — JWT claims not trusted for this path
- [x] `rls_team_isolation` policy checks `current_setting('app.org_role') = 'admin'` so admins bypass team scoping
- [x] Cross-org resource enumeration by ID is not possible — non-owned IDs return `None` / 404, not 403

### Concurrency

- [x] Two concurrent transactions on different orgs do not interfere — `set_config` is per-backend-connection and `is_local=true` scopes to the transaction
- [x] Advisory locks are org-scoped — different orgs can lock the same pipeline name concurrently
- [x] Connection pool checkout with reset hook prevents context bleed across requests sharing a connection
- [x] `register_rls_reset_hook` is safe to call once at engine init — uses `@event.listens_for` which supports multiple engines

### Backward Compatibility

- [x] Existing 200+ CRUD functions and 30+ route handlers work unchanged with RLS applied
- [ ] Non-Postgres backends (MariaDB, SQLite) receive equivalent tenant filtering via ORM listener — zero code changes in CRUD
- [x] Legacy resources (`owner_team_id=NULL`, `visibility=org`) remain fully accessible to all org members
- [x] `set_rls_user_context` is additive — all existing `set_rls_org` callers continue to work
- [ ] BDD test patches for `set_rls_org` continue working alongside new user context function
- [x] Existing API responses unchanged — RLS filtering is invisible to the client (fewer rows, same schema)

### Migration Column Rename

- [x] `rls_team_isolation` policy correctly references `team_memberships.account_id` (post-migration 0060 fix)
- [ ] No automated test verifies the RLS policy works after the column rename

### API Response Inconsistencies

- [x] `MembershipResponse.user_id` returns the value of `account_id` column (cosmetic — field name mismatches column name)

## Known Gaps

- No BDD scenario for connector_team_mismatch error path
- No BDD scenario for privilege escalation prevention in team membership grants
- No BDD scenario for `set_rls_user_context` error paths
- No test for cross-org single-resource fetch by ID (should return None/404)
- No test for `view_as_team` parameter enforcement
- Migration 0060 only fixes the RLS policy — no test proves the policy still works after the column rename
- `connector_team_mismatch` error (PRD §9.3) is not implemented — connector bindings at pipeline-save time do not enforce team scoping
- `MembershipResponse.user_id` field name in teams.py doesn't match the DB column `account_id` (cosmetic)
