---
id: feat-evals-eval-gates
prd: §8.17
delivery-tasks: [task-nv2-conditional-hitl, task-nv2-eval-gate-enforcement]
bdd:
  - backend/tests/features/evals/conditional_hitl.feature
  - backend/tests/features/evals/eval_block.feature
  - backend/tests/bdd/features/hitl/approval_gate.feature
code:
  - backend/src/modulo/core/pipeline_engine/node_runner.py
  - backend/src/modulo/core/pipeline_engine/executor.py
  - backend/src/modulo/core/hitl_manager/__init__.py
  - backend/src/modulo/core/hitl_manager/expiry_job.py
  - backend/src/modulo/core/eval_engine/__init__.py
  - backend/tests/unit/hitl_manager/test_hitl_manager.py
  - backend/tests/unit/core/hitl_manager/test_hitl_jwt.py
  - backend/tests/unit/core/test_eval_suite.py
  - backend/tests/unit/api/test_evals_endpoint.py
  - backend/tests/bdd/steps/test_hitl.py
  - backend/tests/bdd/steps/test_eval.py
unit-tests:
  - backend/tests/unit/hitl_manager/test_hitl_manager.py
  - backend/tests/unit/core/hitl_manager/test_hitl_jwt.py
  - backend/tests/unit/core/test_eval_suite.py
  - backend/tests/unit/api/test_evals_endpoint.py
depends-on: [feat-evals-eval-engine]
status: partial
---

# Eval Gates

Conditional HITL gating and eval-before-interrupt for pipeline nodes. A HITL gate can be made conditional via a JMESPath `condition` expression on the gate config. Additionally, node-scoped eval definitions are evaluated after the condition check but before the interrupt — block-level eval failures raise `EvalBlockedError`, preventing the interrupt entirely. Post-run, eval suites with `pass_threshold` are checked and can transition the run to `eval_failed`.

## Behaviours

### Happy Path

- [ ] HITL gate with no condition and no evals fires NodeInterrupt at expected gate node
- [ ] Gate creates a `hitl_claims` row on first visit (idempotent on re-creation)
- [ ] Claim sets claimant, token, and TTL expiry; returns updated gate
- [ ] Approve with valid token records "approved" decision and clears claim fields
- [ ] Reject with valid token records "rejected" decision and clears claim fields
- [ ] Expiry job resets stale claims back to unclaimed (claimed_by=NULL, token=NULL, expires_at=NULL)
- [ ] Expiry job resets affected run status from "claimed" back to "awaiting_human"
- [ ] Expiry job runs per-org with RLS scope (handles multi-tenant correctly)
- [ ] Eval with `failure_behaviour="warn"` logs warning and continues — gate still fires
- [ ] Eval with `failure_behaviour="block"` raises `EvalBlockedError` instead of interrupt
- [ ] Eval block failure transitions run to `eval_failed` terminal state with `error_code="eval_blocked"`
- [ ] Suite-level pass_threshold check after run completion — suite passes, run stays "complete"
- [ ] Suite-level pass_threshold check — suite fails, run transitions to "failed" with `error_code="eval_suite_blocked"`
- [ ] Multiple evals on one node: all pass — gate proceeds normally
- [ ] Cancel during eval evaluation stops run with status "cancelled"
- [ ] `EvalBlockedError` includes eval name and detail in exception message
- [ ] `EvalSuiteBlockedError` includes suite_id, score, and threshold in exception message
- [ ] Block failure publishes `run_failed` broker event
- [ ] `standalone_evaluate()` runs an ad-hoc eval without a persisted EvalDefinition (for Feedback System)
- [ ] Resume from interrupt: gate detects `_hitl_decision` in state and does not re-evaluate condition or evals
- [ ] Resume from interrupt: rejected decision routes via reject edge if configured
- [ ] `list_pending` returns all unclaimed, undecided gates for an org
- [ ] `list_overdue` returns gates whose `claimed_at` exceeds the overdue threshold
- [ ] `count_overdue` returns count of overdue gates

### Conditional Gating (§8.17)

- [ ] JMESPath `condition` on gate config evaluated against LangGraph state
- [ ] Condition evaluates to truthy value → gate proceeds to autonomy check (may fire interrupt)
- [ ] Condition evaluates to falsy value → gate skipped with `condition_skipped` artifact
- [ ] Condition `null` or absent → gate proceeds normally (non-conditional)
- [ ] Eval-definitions evaluated after condition check but before interrupt
- [ ] Eval-definitions on node, not on gate — scoped to upstream node output
- [ ] Eval-definitions evaluated only on first visit, not on resume (`_hitl_decision` check is first)

### Autonomy Integration

- [ ] `manual_approval` autonomy level: gate fires interrupt for human review
- [ ] `notify_on_complete` autonomy level: gate auto-approves without interrupt, records artifact
- [ ] `fully_autonomous` autonomy level: gate silently skipped
- [ ] `human_only` flag on gate config overrides autonomy — always interrupts
- [ ] Autonomy level read from `run_context._pipeline_default_autonomy` at runtime
- [ ] `should_skip_hitl_gate` returns true for `fully_autonomous`
- [ ] `should_notify_on_complete` returns true for `notify_on_complete`

### HITL Claim Lifecycle

- [ ] Create gate with required_team_id — stored on HitlClaim row (used to scope claimants)
- [ ] Claim requires JWT or opaque token (secret_key at construction determines which)
- [ ] Claim pre-checks: gate exists, not already decided, not already claimed, team membership if team-scoped
- [ ] Claim race condition: concurrent claims on same gate — exactly one wins, others get `AlreadyClaimedError`
- [ ] Claim with custom `expiry_minutes` sets `expires_at` accordingly
- [ ] Claim with no `secret_key` generates opaque random token (alpha backwards compat)
- [ ] Claim with `secret_key` generates short-lived JWT (15-min default TTL) scoped to run_id + gate_id + claimant_id
- [ ] Approve validates JWT signature + scope, then checks DB token match + expiry
- [ ] Expired JWT → `ClaimTokenExpiredError`
- [ ] Invalid JWT (bad signature, scope mismatch) → `ClaimTokenInvalidError` (no opaque fallback for bad JWT)
- [ ] Non-JWT token on JWT-configured manager → opaque comparison fallback
- [ ] Approve/reject on non-existent gate → `GateNotFoundError`
- [ ] Approve/reject on already-decided gate → `GateAlreadyDecidedError`
- [ ] Approve/reject with wrong token → `ClaimTokenInvalidError`
- [ ] Approve/reject with expired token → `ClaimTokenExpiredError`
- [ ] Expire stale: resets claims with `expires_at < NOW()` and `decision IS NULL`
- [ ] Expire stale: returns list of `{run_id, gate_id}` for notification dispatch
- [ ] Gate with `expires_at=NULL` (defensive guard) → treated as expired on decide attempt

### Eval Suite Post-Run Check

- [ ] Executor loads eval definitions with `suite_id` and `pass_threshold` after run completes
- [ ] Suite threshold check aggregates eval results by suite_id
- [ ] Suite aggregate score above threshold → run stays "complete"
- [ ] Suite aggregate score at threshold → run stays "complete"
- [ ] Suite aggregate score below threshold → run transitions to "failed" with `error_code="eval_suite_blocked"`
- [ ] No suite definitions with threshold → no post-run check
- [ ] Empty suite (no results) → passes (aggregate_score=1.0)
- [ ] Multiple suites with thresholds — first failing suite terminates check

### Error States

- [ ] Gate not found on claim → `GateNotFoundError`
- [ ] Gate already claimed → `AlreadyClaimedError` (not idempotent)
- [ ] Gate already decided → `GateAlreadyDecidedError`
- [ ] Non-team-member tries to claim team-scoped gate → `NotTeamMemberError`
- [ ] Viewers/non-approvers cannot approve/reject → 403 response
- [ ] Expired claim token → `ClaimTokenExpiredError` (not silently accepted)
- [ ] `EvalSuiteBlockedError` is defined but not raised anywhere (UNTESTED)
- [ ] Expiry job tick failure logged and recovered on next tick
- [ ] Cancelled expiry job stops cleanly
- [ ] DB session failure in expiry job does not crash the background loop

### Edge Cases

- [ ] Condition expression runtime error → JMESPath raises, percolates as node error
- [ ] Eval definitions list is empty → no eval check performed (gate proceeds)
- [ ] Eval definition with no node_id (pipeline-level) → not loaded by executor for eval-before-interrupt
- [ ] Condition value is `False` literal → treated as falsy, gate skipped
- [ ] Condition value is `0` (int) → treated as falsy
- [ ] Condition value is empty string → treated as falsy
- [ ] Condition value is empty list/dict → treated as falsy
- [ ] Claim token: SQL UPDATE with WHERE clause provides atomicity (no TOCTOU)
- [ ] Approve/reject: JWT validation before SQL UPDATE to fail fast on bad token
- [ ] _decide(): DB WHERE clause checks `expires_at > now()` — DB is authoritative TTL source
- [ ] _decide(): scalar_one_or_none() returning None triggers diagnostic SELECT for precise error
- [ ] _looks_like_jwt heuristic: counts dots — avoids misidentifying opaque tokens as JWTs
- [ ] create_gate idempotent: duplicate run_id+gate_id returns existing row (no error)
- [ ] post-run suite check uses `distinct(EvalDefinition.suite_id)` to avoid redundant queries
- [ ] Post-run suite check uses `pass_threshold` from any definition in suite (first found if multiple)

### Concurrency

- [ ] Claim uses atomic `UPDATE ... WHERE claimed_by IS NULL AND decision IS NULL RETURNING`
- [ ] Race between pre-check SELECT and UPDATE → second claimant gets `AlreadyClaimedError` from RETURNING
- [ ] HITLManager is stateless — safe for concurrent use with separate sessions
- [ ] Expiry job uses per-org transactions with RLS `SET LOCAL` — no cross-org data leaks
- [ ] Expiry job iterates orgs sequentially, not in parallel — safe but potentially slow with many orgs
- [ ] Expiry job batch-resets run status via `UPDATE ... WHERE status = "claimed"` — avoids lost updates
- [ ] Pipeline executor loads eval definitions before claiming capacity slot — stale definitions possible if definition added during capacity wait
- [ ] _check_eval_suites reads uncommitted results from a fresh session after the streaming session closes

### Security

- [ ] Admin role required for eval definition CRUD (403 for runner/operator roles)
- [ ] Unauthenticated requests to eval API endpoints return 401
- [ ] RLS scopes all hitl_claims queries by organisation_id
- [ ] RLS scopes all eval_results and eval_definitions queries by organisation_id
- [ ] JWT claim_token scoped to specific run_id + gate_id + client_id — replay restricted
- [ ] Opaque claim_token is cryptographically random (secrets.token_urlsafe, 32 bytes)
- [ ] Expired JWT is rejected — no silent acceptance of stale tokens
- [ ] Bad signature / scope mismatch raises `ClaimTokenInvalidError` without opaque fallback
- [ ] Non-JWT token on secret_key-configured manager falls through to opaque comparison (backwards compat)
- [ ] Expiry job sets RLS per session — each org's claims scoped correctly
- [ ] Eval-before-interrupt: evaluate output against state before human sees it (no data leak on block)

### Backward Compatibility

- [ ] Opaque claim tokens accepted alongside JWTs during migration period
- [ ] create_gate returns existing row if run_id+gate_id already exists — no breaking change for idempotent callers
- [ ] HitlClaim model columns stable across migrations (expires_at, claim_token, etc.)
- [ ] Claim without secret_key still generates valid token (alpha mode)
- [ ] `list_pending` and `list_overdue` API shape unchanged
- [ ] `_looks_like_jwt` heuristic avoids breaking opaque token consumers
- [ ] EvalEngine.evaluate() signature stable — `llm_judge_callable` remains optional kwarg
- [ ] `standalone_evaluate()` provides non-persisted path for backwards compatibility with Feedback System

## Known Gaps

- [ ] PRD §8.17 specifies HITL gate `condition` as `{eval_id, threshold, operator}` referencing an eval definition. The code implements condition as a JMESPath expression against state (node_runner.py:128-163), with eval-definitions as a separate array. These are different mechanisms. The BDD `conditional_hitl.feature` includes scenarios for both — the implementation's eval-before-interrupt evaluates ALL eval_definitions against state with no per-eval threshold or operator. The PRD-specified eval-reference format (eval_id + threshold + operator) is not implemented.
- [ ] `EvalSuiteBlockedError` class defined in `eval_engine/__init__.py` but never raised or caught anywhere. The post-run suite check in executor.py sets `error_code = "eval_suite_blocked"` manually instead.
- [ ] `node_runner.py:168` — `engine.evaluate(state, eval_def)` return value is discarded. Eval results are not logged or persisted for eval-before-interrupt. Only exceptions are surfaced. Warn-level eval failures produce no output.
- [ ] `eval_block.feature` scenario "Block failure is recorded in AuditEvent" references AuditEvent recording, but no AuditEvent integration exists in the eval engine or executor. AuditEvent writing for block failures is not implemented.
- [ ] `eval_block.feature` scenario "Multiple evals on one node all must pass" expects "remaining evals are not evaluated" on first block failure — the code iterates all eval_definitions and only raises after the loop, but calls `engine.evaluate()` which raises `EvalBlockedError` on block failure, so remaining evals are actually skipped (by exception propagation). This happens to be correct but is implicit.
- [ ] `evaluate_suite()` returns `passed=True` for empty suites (aggregate_score=1.0) — this means a suite with zero definitions and a threshold of 0.0 would pass even though no evals ran. The post-run check only loads suites that have definitions with thresholds, so this edge case is unreachable in practice.
- [ ] No BDD scenarios for non-approver claim, team-scoped gate claim denial, or gate listing with permissions.
- [ ] Claim expiry job resets run status from "claimed" to "awaiting_human" (`expiry_job.py:116-121`) — but the initial run status set by the executor is "awaiting_human", not "claimed". The status "claimed" is set by the HITL claim API, not the executor. The status reset path relies on this API setting, which is not tested at BDD level.
- [ ] Post-run suite check in executor.py queries eval_results from a fresh session — it may not see results from the streaming run if the session isn't yet committed. The run status update and suite check run in the same transaction block after the streaming session is closed.
- [ ] No integration test for the full eval-before-interrupt → suite check → eval_failed chain end-to-end.
