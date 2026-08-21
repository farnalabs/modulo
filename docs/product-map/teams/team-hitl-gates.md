---
id: feat-teams-team-hitl-gates
prd: 8.8, 9.3
delivery-tasks: [task-nv1-team-hitl-gates]
bdd: [backend/tests/bdd/features/teams/team_hitl_gate.feature]
unit-tests: [backend/tests/unit/hitl_manager/test_hitl_manager.py]
code:
  - backend/src/modulo/core/hitl_manager/__init__.py
  - backend/src/modulo/db/models/hitl_claim.py
  - backend/src/modulo/db/migrations/versions/0003_v2_pipeline_runtime.py
  - backend/src/modulo/api/routes/pipelines.py
  - backend/src/modulo/api/routes/hitl.py
  - backend/src/modulo/api/mcp_server.py
  - backend/src/modulo/core/pipeline_engine/node_runner.py
  - backend/src/modulo/core/pipeline_engine/graph_cache.py
depends-on: [feat-teams-team-crud]
status: covered
---
# Team HITL Gates

A HITL gate may specify `required_team_id` to restrict claim/approve to members of that team. Enforcement uses a DB-live membership check (not JWT claims). `human_only` and `required_team_id` are additive — both must hold independently.

**Note:** Role filtering (`runner`/`operator`-only) is enforced in `claim()` since 2026-08-15 — a `viewer` team membership cannot claim a team-scoped gate.

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
- [x] Team member with `runner` or `operator` team role can claim a team-scoped gate — the claim-time membership query carries `role IN ('runner', 'operator')`
- [x] Team member with only the `viewer` role cannot claim a team-scoped gate — the role predicate excludes `viewer` memberships from the membership check, so the claim raises `NotTeamMemberError` (403) before any claim UPDATE
- [x] The role predicate is applied to BOTH membership queries — the pre-check AND the post-claim TOCTOU re-verification, so a member demoted from `operator`/`runner` to `viewer` between check and UPDATE has the claim undone
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
- [x] Gate context resource (`modulo://runs/{run_id}/hitl/{gate_id}`) exposes `required_team_id` and `required_team_name`

### Expiry and overdue
- [x] `expire_stale()` resets claims regardless of `required_team_id` (column is not reset — only claim fields)
- [x] `list_overdue()` / `count_overdue()` work identically for team-scoped gates

### Notification routing
- [x] `hitl_awaiting` for `required_team_id` gates dispatches `team_id` in the event payload for routing to team notification endpoints
- [x] Actual dispatch to team notification endpoints (falls back to org-wide endpoints if team has none) — `Notifier.dispatch_event(org_id, event_type, payload, team_id=...)` routes to the team's endpoints first and falls back to org-wide (`team_id IS NULL`) endpoints when the team has none (`_get_subscribed_endpoints`); verified by `test_team_scoped_dispatch` / `test_get_subscribed_endpoints` (parametrised: team endpoints → org fallback → org-only) in `tests/unit/notifier/test_notifier.py`

### Unit test coverage
- [x] `test_create_gate_with_required_team_id` — gate stores the team ref
- [x] `test_claim_team_member_can_claim` — team member successfully claims
- [x] `test_claim_non_team_member_raises` — non-member gets `NotTeamMemberError`
- [x] `test_claim_no_required_team_still_works` — gate without team restriction is unchanged

### Error Handling
- [x] `HITLManager.create_gate()` is called from the `NodeInterrupt` handler in `executor.py` — gate row is persisted during pipeline execution before the `hitl_awaiting` event is published
- [x] `required_team_id` extraction from graph state in executor handles invalid UUID gracefully (logged, not raised) — `make_hitl_gate_fn` normalises the gate config's `required_team_id` via `_normalize_required_team_id` before it enters the interrupt payload, so the executor's `uuid.UUID()` conversion never sees an unparseable value; an invalid value is logged (`hitl_gate.invalid_required_team_id`) and treated as org-wide (None). Verified by `test_hitl_gate_invalid_required_team_id_is_logged_and_sanitized` / `test_hitl_gate_valid_required_team_id_passes_through` / `test_hitl_gate_required_team_id_absent_is_none` in `tests/unit/hitl_manager/test_hitl_manager.py`

## Known Gaps

- `ViewAsTeam` enforcement for HITL gate visibility not yet tested
- No test for `human_only` + `required_team_id` additive enforcement at ViewModel layer
- No performance test for DB-live membership check on high-claim-contention gates
- (Resolved) Team membership role enforcement: `claim()` checks TeamMembership existence only — any role (including `viewer`) could claim a team-scoped gate. Fixed 2026-08-15: both membership queries now carry `role IN ('runner', 'operator')` (`_TEAM_CLAIM_ROLES` in `core/hitl_manager/__init__.py`), so a `viewer` membership can never satisfy the claim. Propagates to REST (`hitl.claim_gate` → 403) and MCP (`review_hitl` → `not_team_member`) because both call `HITLManager.claim()`.
- BDD scenarios for team HITL gates are implemented as mock-based step definitions, not full integration tests with real DB
- (Resolved 2026-08-16) **Executor → notifier wiring for `hitl_awaiting`** — `core/pipeline_engine/executor.py` `_handle_graph_interrupt` now dispatches the `hitl_awaiting` webhook/in-app notification via the injected `Notifier` (with `team_id` for team-scoped gates, falling back to org-wide), closing the previously-only-WebSocket gap. Wired in the SAQ execute/resume path (`pipeline_execution.load_and_setup`) and the HITL decision routes (`api/routes/hitl.py`). PRD §8.8 HITL Flow step 2 ("Outbound notification webhook dispatched (HMAC-signed)") now triggers from the run lifecycle. See QA History below.

### 2026-08-16 — improve-architecture (product-map walk)

- **Fixed (PRD §8.8 step 2):** the executor→notifier `hitl_awaiting` gap is closed. (1) `PipelineExecutor` accepts an injectable `notifier:` seam; `_handle_graph_interrupt` resolves the pipeline name, publishes the broker event as before, then calls the new failure-isolated `_dispatch_hitl_awaiting` which invokes `Notifier.dispatch_event(EVENT_HITL_AWAITING, ..., team_id=...)` — routing team-scoped gates to team endpoints first (org-wide fallback) and creating the in-app notification record. (2) Wired at construction in `pipeline_execution.load_and_setup` (SAQ `execute_run`/`resume_run`) and via the new `_build_resume_executor` helper in all 5 HITL decision-route resumes (`approve`, `approve-with-modification`, `reject`, `deliver-manual`, `submit-manual`). (3) Notifier init and dispatch are failure-isolated (fail-open — a broken notifier never blocks gate creation or the run pause). (4) **Tests** — 6 new in `tests/unit/pipeline_engine/test_executor.py`: `test_dispatch_hitl_awaiting_routes_through_notifier`, `test_dispatch_hitl_awaiting_without_pipeline_name_omits_key`, `test_dispatch_hitl_awaiting_skips_without_notifier`, `test_dispatch_hitl_awaiting_failure_is_isolated`, `test_execute_with_notifier_dispatches_hitl_awaiting`. Verification: ruff check + format clean on `executor.py`, `pipeline_execution.py`, `hitl.py`, `test_executor.py`.

### 2026-08-15 — distribute (FAR-245 coverage walk)

- **Verified (notification routing):** `Notifier.dispatch_event(..., team_id=...)` routes `hitl_awaiting` to team endpoints first, falling back to org-wide (`team_id IS NULL`) endpoints when the team has none configured — `_get_subscribed_endpoints` queries `team_id == <team>` then falls back to `team_id IS NULL`. Existing coverage in `tests/unit/notifier/test_notifier.py` (`test_team_scoped_dispatch`, `test_get_subscribed_endpoints` parametrised with the team→org-fallback→org-only cases). The residual gap is the executor-side call site (see Known Gaps).
- **Implemented (graceful invalid-UUID handling):** `make_hitl_gate_fn` in `core/pipeline_engine/node_runner.py` now normalises `hitl_gate_config.required_team_id` via `_normalize_required_team_id` — a `uuid.UUID` passes through, a valid UUID string is canonicalised, and an unparseable value is logged (`hitl_gate.invalid_required_team_id`) and treated as org-wide (None) instead of letting the executor's `uuid.UUID(...)` raise and fail the run. Added 3 unit tests in `tests/unit/hitl_manager/test_hitl_manager.py` (invalid → logged + None, valid string passthrough, absent → None). Verified: full `test_hitl_manager.py` + `test_error_handling.py` + `test_notifier.py` + `test_node_runner_hitl.py` pass.

### 2026-08-15 — improve-architecture (product-map walk)

- **Fixed (SECURITY):** team-scoped HITL gate claims enforced `TeamMembership` existence only — any team role (including read-only `viewer`) could claim (and therefore approve/reject) a team gate. `HITLManager.claim()` now filters both team-membership queries by `_TEAM_CLAIM_ROLES = ("runner", "operator")` (`core/hitl_manager/__init__.py`), mirroring the org-level `hitl.claim` permission (runner-scoped): a `viewer` membership never matches, so the claim raises `NotTeamMemberError` → 403 on the REST route and `not_team_member` on the MCP tool. The role predicate is applied to the pre-check AND the post-claim TOCTOU re-verification, so a member demoted to `viewer` between the check and the claim UPDATE has the claim undone. The REST and MCP paths need no change — both call `HITLManager.claim()`.
- **Fixed (test wiring):** `tests/bdd/steps/test_team_hitl_gate.py` recorded responses on `ctx["_resp"]` while the shared `then` steps in `tests/bdd/conftest.py` read `request.node._resp` — all 4 `when` steps failed at runtime (`AttributeError: 'Function' object has no attribute '_resp'`), leaving the whole feature file broken on main. Each `when` step now records `request.node._resp` (matching `test_team_gates.py`/`test_hitl.py`), so the feature is executable again.
- Added 3 unit tests in `tests/unit/hitl_manager/test_hitl_manager.py`: `test_claim_team_membership_query_restricts_to_runner_or_operator_role` (both queries carry `team_memberships.role IN ('runner', 'operator')` via literal-binds SQL), `test_claim_team_viewer_role_denied` (viewer row filtered out → `NotTeamMemberError`, claim UPDATE never reached), `test_claim_team_role_lost_between_check_and_update_undoes_claim` (demotion between check and UPDATE → claim released).
- Added 1 BDD scenario (`Team viewer cannot claim team-required HITL gate` → 403) to `team_hitl_gate.feature` and updated the claim step to model the role filter (`runner`/`operator` only).
- Verification: 77/77 `test_hitl_manager.py` unit tests, 254 focused HITL/team/MCP unit tests, and 6/6 `team_hitl_gate.feature` BDD scenarios pass; ruff check + format clean; mypy --strict clean on `core/hitl_manager/__init__.py`.

### 2026-07-31 — improve-architecture (product-map walk)

- Fixed stale CODE ref: migration `0027_hitl_claim_team.py` renamed in v2 squash → `0003_v2_pipeline_runtime.py` (creates `hitl_claims` with `required_team_id`).
