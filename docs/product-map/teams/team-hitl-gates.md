---
id: feat-teams-team-hitl-gates
prd: 8.8, 9.3
delivery-tasks: [task-nv1-team-hitl-gates]
bdd: [backend/tests/bdd/features/teams/team_hitl_gate.feature]
unit-tests: [backend/tests/unit/hitl_manager/test_hitl_manager.py]
code:
  - backend/src/modulo/core/hitl_manager/__init__.py
  - backend/src/modulo/db/models/hitl_claim.py
  - backend/src/modulo/db/migrations/versions/0027_hitl_claim_team.py
  - backend/src/modulo/api/routes/pipelines.py
  - backend/src/modulo/api/mcp_server.py
  - backend/src/modulo/core/pipeline_engine/node_runner.py
  - backend/src/modulo/core/pipeline_engine/graph_cache.py
depends-on: [feat-teams-team-crud]
status: partial
---
# Team HITL Gates

A HITL gate may specify `required_team_id` to restrict claim/approve to members of that team with `runner` or `operator` team role. Enforcement uses a DB-live membership check (not JWT claims). `human_only` and `required_team_id` are additive — both must hold independently.

## Behaviours

### Gate definition
- [x] `HitlGateConfig.required_team_id` is optional (UUID | null)
- [x] `required_team_id` may be set independently of `human_only`
- [x] Setting both `human_only` + `required_team_id` enforces both conditions additively
- [x] `required_team_id` FK references `teams.id` with `ondelete=RESTRICT`
- [x] Migration 0027 adds `required_team_id` column to `hitl_claims` table

### Gate creation
- [x] `HITLManager.create_gate()` accepts and stores `required_team_id` on the `HitlClaim` row
- [x] Gate without `required_team_id` stores null (no team restriction)

### Claim — team enforcement
- [x] `HITLManager.claim()` performs a DB-live membership check when `gate.required_team_id` is set
- [x] Membership check queries `TeamMembership` where `team_id == required_team_id` AND `user_id == claimant_id` AND `organisation_id == org_id`
- [x] Team member with `runner` or `operator` team role can claim a team-scoped gate
- [x] Non-team member receives `NotTeamMemberError` (PermissionError)
- [x] Gate without `required_team_id` does not query team membership at claim time
- [x] Team membership check happens after gate-exists/not-decided/not-claimed pre-checks but before UPDATE
- [x] Pre-check uses SELECT (not a lock) — race window between pre-check and UPDATE is handled by the atomic UPDATE RETURNING pattern

### Claim — existing invariants preserved
- [x] Gate not found → `GateNotFoundError`
- [x] Gate already decided → `GateAlreadyDecidedError`
- [x] Gate already claimed → `AlreadyClaimedError`
- [x] Claim expiry and token generation work identically for team-scoped and non-team gates

### Approve / reject
- [x] `approve()` / `reject()` do not re-check team membership (enforced at claim time)
- [x] Token validation (JWT or opaque) works identically regardless of `required_team_id`
- [x] Decision recorded as `approved` or `rejected`; claim released

### MCP exposure
- [x] `list_pending_hitl` exposes `required_team_id` in the gate resource
- [x] Gate context resource exposes `required_team_id` and `required_team_name` to LLM clients and reviewers

### Expiry and overdue
- [x] `expire_stale()` resets claims regardless of `required_team_id` (column is not reset — only claim fields)
- [x] `list_overdue()` / `count_overdue()` work identically for team-scoped gates

### Notification routing
- [ ] `hitl_awaiting` for `required_team_id` gates dispatches to team notification endpoints (falls back to org-wide endpoints if team has none)

### Unit test coverage
- [x] `test_create_gate_with_required_team_id` — gate stores the team ref
- [x] `test_claim_team_member_can_claim` — team member successfully claims
- [x] `test_claim_non_team_member_raises` — non-member gets `NotTeamMemberError`
- [x] `test_claim_no_required_team_still_works` — gate without team restriction is unchanged

### Error Handling
- [ ] HITLManager.create_gate() is defined but NEVER called from production code — gate row is never created. Claim endpoint returns GateNotFoundError for any gate reached during a pipeline run. This is a CRITICAL gap.

## Known Gaps

- `HITLManager.create_gate()` is never called from production code - HitlClaim rows not created during pipeline execution
- `ViewAsTeam` enforcement for HITL gate visibility not yet tested
- No test for `human_only` + `required_team_id` additive enforcement at ViewModel layer
- No test for team notification fallback chain (team endpoints → org endpoints)
- No performance test for DB-live membership check on high-claim-contention gates
- `PendingHitlGate` in viewmodel still needs `required_team_name` propagation from `HitlClaim` model 