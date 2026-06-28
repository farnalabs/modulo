---
id: feat-teams-team-isolation
prd: §9.3
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
depends-on: [task-nv1-team-entity]
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

- [ ] Org A user sees only Org A's resources — cross-org data is invisible across pipelines, agents, schemas, and connector instances
- [ ] Org admin sees all resources within their org regardless of team membership
- [ ] Team member sees resources with `visibility: team` owned by their team
- [ ] User sees resources with `visibility: org` irrespective of team membership
- [ ] `set_rls_org` applies the org context within an active transaction
- [ ] `set_rls_user_context` sets `user_id` and `org_role` for team-scoped RLS evaluation
- [ ] Pool checkout hook resets `app.organisation_id`, `app.user_id`, `app.org_role` to the empty-string sentinel
- [ ] ORM `do_orm_execute` listener injects `WHERE organisation_id = :oid` for non-Postgres backends
- [ ] All 21 org-scoped tables receive `rls_org_isolation` policy on migration 0002
- [ ] Five team-scoped tables (pipelines, stages, connector_instances, model_backends, library_primitives) receive `rls_team_isolation` policy on migration 0025

### Edge Cases

- [ ] User not in any team sees only org-visibility resources — no team-private leakage
- [ ] User in multiple teams sees each team's resources independently with their respective team roles
- [ ] Resource with `owner_team_id=NULL` and `visibility=org` (legacy/unowned) accessible to all org members
- [ ] Resource with `owner_team_id=NULL` and `visibility=team` is blocked by DB check constraint
- [ ] Empty org returns empty lists from all CRUD functions without error
- [ ] User removed from team loses access to that team's resources at next token refresh (JWT) or immediately (DB-live HITL check)
- [ ] `set_config(is_local=true)` reverts to the session-level empty string after COMMIT or ROLLBACK
- [ ] Second transaction on the same pooled connection starts without stale org context
- [ ] Org role does not override team visibility — an org-level `operator` outside the owning team cannot see team-private resources

### Error States

- [ ] Calling `set_rls_org` outside an active transaction raises `RuntimeError`
- [ ] Cross-org pipeline fetch by ID returns `None` (RLS filters the row) — not an error
- [ ] Cross-org pipeline run POST returns 404 (resource non-existence, not 403)
- [ ] Non-admin using `view_as_team` parameter returns 403
- [ ] Team deletion blocked (`team_has_resources` error) when owned resources exist
- [ ] Connector binding across teams returns `connector_team_mismatch` error
- [ ] Team member grant with role exceeding the granting user's role is blocked (privilege escalation prevention)
- [ ] HITL gate with `required_team_id` — non-member attempting claim returns 403

### Security

- [ ] RLS `rls_org_isolation` policy exists on every org-scoped table (migration 0002)
- [ ] `nullif(current_setting('app.organisation_id', true), '')::uuid` converts missing/empty context to NULL — no rows visible when org context is unset
- [ ] `set_config(is_local=true)` prevents org_id leakage across transactions (proven by integration tests)
- [ ] Pool checkout hook sets all three session vars to empty string — defense-in-depth against stale context
- [ ] FORCE ROW LEVEL SECURITY intentionally omitted — relies on non-superuser app connection role (infrastructure responsibility)
- [ ] ORM tenant filter (`_inject_tenant_filter`) covers SELECT, UPDATE, DELETE for non-Postgres backends
- [ ] `team_memberships` table itself is org-scoped (inherits `OrgScoped`) — memberships are isolated per tenant
- [ ] HITL gate `required_team_id` enforcement uses DB-live membership check — JWT claims not trusted for this path
- [ ] `rls_team_isolation` policy checks `current_setting('app.org_role') = 'admin'` so admins bypass team scoping
- [ ] Cross-org resource enumeration by ID is not possible — non-owned IDs return `None` / 404, not 403

### Concurrency

- [ ] Two concurrent transactions on different orgs do not interfere — `set_config` is per-backend-connection and `is_local=true` scopes to the transaction
- [ ] Advisory locks are org-scoped — different orgs can lock the same pipeline name concurrently
- [ ] Connection pool checkout with reset hook prevents context bleed across requests sharing a connection
- [ ] `register_rls_reset_hook` is safe to call once at engine init — uses `@event.listens_for` which supports multiple engines

### Backward Compatibility

- [ ] Existing 200+ CRUD functions and 30+ route handlers work unchanged with RLS applied
- [ ] Non-Postgres backends (MariaDB, SQLite) receive equivalent tenant filtering via ORM listener — zero code changes in CRUD
- [ ] Legacy resources (`owner_team_id=NULL`, `visibility=org`) remain fully accessible to all org members
- [ ] `set_rls_user_context` is additive — all existing `set_rls_org` callers continue to work
- [ ] BDD test patches for `set_rls_org` continue working alongside new user context function
- [ ] Existing API responses unchanged — RLS filtering is invisible to the client (fewer rows, same schema)

## Known Gaps

- `rls_enforcement.feature` at `backend/tests/bdd/features/security/rls_enforcement.feature` is a placeholder with no scenarios
- No BDD scenarios cover the `rls_team_isolation` policy (team-scoped visibility) — only org-level tenant isolation is covered
- No BDD scenario for connector_team_mismatch error path
- No BDD scenario for privilege escalation prevention in team membership grants
- No BDD scenario for `set_rls_user_context` error paths

