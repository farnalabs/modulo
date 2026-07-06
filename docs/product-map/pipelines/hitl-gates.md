---
id: feat-pipelines-hitl-gates
prd: 8.8
delivery-tasks: [task-nv1-hitl-claim-api, task-nv1-hitl-approve-reject, task-nv1-hitl-expiry-job, task-nv2-conditional-hitl, task-nv2-eval-gate-enforcement, task-nv2-eval-definition, task-prd-hitl-conditional-gates]
bdd:
  - backend/tests/bdd/features/hitl/approval_gate.feature
  - backend/tests/bdd/features/hitl/deliver_manual.feature
  - backend/tests/bdd/features/hitl/feedback_handler.feature
  - backend/tests/bdd/features/hitl/manual_node.feature
  - backend/tests/bdd/features/hitl/modify_then_approve.feature
  - backend/tests/bdd/features/eval/conditional_hitl.feature
code:
  - backend/src/modulo/core/hitl_manager/__init__.py
  - backend/src/modulo/core/hitl_manager/expiry_job.py
  - backend/src/modulo/core/hitl_manager/overdue_warning.py
  - backend/src/modulo/core/pipeline_engine/node_runner.py
  - backend/src/modulo/core/pipeline_engine/graph_cache.py
  - backend/src/modulo/core/pipeline_engine/executor.py
  - backend/src/modulo/core/eval_engine/__init__.py
  - backend/src/modulo/api/routes/pipelines.py
  - backend/src/modulo/db/models/hitl_claim.py
  - backend/src/modulo/core/graph_validator/__init__.py
  - frontend/src/views/PipelineEditorView.vue
unit-tests:
  - backend/tests/unit/pipeline_engine/test_node_runner_hitl.py
  - backend/tests/unit/hitl_manager/test_hitl_manager.py
  - backend/tests/unit/core/hitl_manager/test_hitl_jwt.py
  - backend/tests/unit/pipeline_engine/test_graph_cache_hitl.py
  - backend/tests/unit/pipeline_engine/test_conditional_transitions.py
  - backend/tests/unit/graph_validator/test_graph_validator.py
depends-on: [feat-pipelines-core, feat-evals-eval-engine]
status: partial
---

# HITL Gates

HITL (Human-In-The-Loop) gates pause pipeline execution at an edge and wait for
a human to review the upstream node's output before deciding whether to approve,
reject, or modify it. A gate is a property of an edge (not a node) and compiles
to an intermediate LangGraph gate node at runtime.

## Behaviours

### Gate Lifecycle

- [x] Edge with `hitl_gate_config` compiles to an intermediate gate node between source and target
- [x] Gate node first call raises `NodeInterrupt` with gate payload
  (gate_id, autonomy_level, human_only, overdue_threshold)
- [x] Run transitions to `awaiting_human` when interrupt is caught by executor
- [x] Human claims the gate (atomic claim via `UPDATE ... WHERE claimed_by IS NULL RETURNING`)
- [x] Human approves the gate with optional notes — run resumes with status `approved`
- [x] Human rejects the gate with required reason — run routes via reject edge or normal forward
- [x] Human delivers output manually — manual_output written to state
- [x] Claim has TTL expiry — `expire_stale` background job resets stale claims
- [x] Claim uses short-lived JWT (15-min default) with per-run/gate/claimant scope
- [x] Opaque random token fallback for alpha backwards compatibility

### Autonomy Integration

- [x] `manual_approval` level: gate fires interrupt for human review
- [x] `notify_on_complete` level: gate auto-approves, records notification artifact
- [x] `fully_autonomous` level: gate silently skipped
- [x] `human_only` flag on gate config overrides autonomy — always interrupts
- [x] Autonomy level read from `run_context._pipeline_default_autonomy` at runtime
- [x] Run context `autonomy_recommendation` overrides pipeline default

### Team-Scoped Gates

- [x] `required_team_id` on gate config restricts claim to team members
- [x] DB-live membership check (not JWT claims)
- [x] `human_only` and `required_team_id` are additive — both must be satisfied for claim
- [x] Non-member claim attempt returns `NotTeamMemberError`

### Conditional Gating (JMESPath — v0.9)

- [x] `condition` field on `hitl_gate_config` supports JMESPath expression evaluated against state
- [x] Condition evaluates to falsy value → gate skipped with `condition_skipped` artifact, no interrupt
- [x] Condition evaluates to truthy value → gate proceeds to autonomy checks (may interrupt)
- [x] Condition `null` or absent → gate proceeds normally
- [x] Falsy semantics: None, False, 0, empty string, empty list/dict all treated as falsy

### Conditional Gating (Eval-Reference — §8.17 v1)

- [ ] `eval_condition` field on `hitl_gate_config` references an eval by name with threshold and operator
- [ ] Eval-before-interrupt runs node-scoped eval definitions, captures results
- [ ] Eval-reference condition checks eval score against threshold (operator lt/gt/lte/gte)
- [ ] Condition true (score below threshold with lt) → NodeInterrupt raised
- [ ] Condition false (score at/above threshold) → execution continues without interrupt
- [ ] Eval results are logged and persisted for eval-before-interrupt

### Eval-Before-Interrupt

- [x] Node-scoped eval definitions evaluated after condition check but before interrupt
- [x] Eval with `failure_behaviour="warn"` logs warning — gate still proceeds to interrupt
- [x] Eval with `failure_behaviour="block"` raises `EvalBlockedError` — prevents interrupt
- [x] Multiple evals on one node: all pass → gate proceeds normally
- [x] First block failure raises `EvalBlockedError`, remaining evals not evaluated

### Resume Semantics

- [x] Gate detects `_hitl_decision` in state on resume — returns immediately
- [x] Resume check is first: condition and evals NOT re-evaluated
- [x] Rejected decision routes via reject edge or normal forward
- [x] Gate artifact on resume records `"interrupted"` status with result

### Overdue Tracking

- [x] `get_overdue_claims()` finds undecided claims exceeding warning (4h) and escalation (24h) thresholds
- [x] Overdue results include remaining time for human review

### Frontend

- [x] HITL Review view: list pending gates, claim, approve, reject — SettingsHitlReviewView.vue
- [ ] Pipeline editor edge properties panel with HITL gate config toggle
- [ ] Pipeline editor: gate condition expression (JMESPath) input field
- [ ] Pipeline editor: eval-reference condition selector (eval, threshold, operator)
- [ ] Pipeline editor: team selector for required_team_id
- [ ] Pipeline editor: human_only toggle

### Error States

- [x] Gate not found on claim → GateNotFoundError
- [x] Gate already claimed → AlreadyClaimedError (not idempotent)
- [x] Gate already decided → GateAlreadyDecidedError
- [x] Non-team-member claims team-scoped gate → NotTeamMemberError
- [x] Expired claim token → ClaimTokenExpiredError
- [x] Invalid claim token → ClaimTokenInvalidError

### Edge Cases

- [x] JMESPath condition runtime error → percolates as node error
- [x] No eval definitions → no eval check, gate proceeds
- [x] Empty eval definitions list → no eval check
- [x] Condition value `False` → falsy, gate skipped
- [x] Condition value `0` → falsy, gate skipped
- [x] Condition value empty string → falsy, gate skipped
- [x] create_gate idempotent: duplicate run_id+gate_id returns existing row

### Concurrency

- [x] Claim uses atomic `UPDATE ... WHERE claimed_by IS NULL AND decision IS NULL RETURNING`
- [x] Race between pre-check SELECT and UPDATE → second claimant gets AlreadyClaimedError
- [x] HITLManager is stateless — safe for concurrent use with separate sessions
- [x] Expiry job uses per-org transactions with RLS SET LOCAL

### Security

- [x] RLS scopes all hitl_claims queries by organisation_id
- [x] JWT claim_token scoped to run_id + gate_id + client_id — replay restricted
- [x] Bad signature / scope mismatch raises ClaimTokenInvalidError without opaque fallback
- [x] Expiry job sets RLS per session

## Error Handling

- [x] Gate lifecycle routes claim/approve/reject return typed errors (GateNotFoundError, AlreadyClaimedError, GateAlreadyDecidedError)
- [x] Claim token validation errors return ClaimTokenExpiredError / ClaimTokenInvalidError
- [x] Non-team-member claim returns NotTeamMemberError
- [x] Missing DB table (ProgrammingError) on HITL claim/approve/reject routes returns 501 Not Implemented
- [x] Claim/approve/reject/deliver-manual/submit-manual routes: SQLAlchemyError (connection/deadlock) returns 503 SERVICE_UNAVAILABLE
- [x] Non-team-member claim via HTTP returns 403 Forbidden
- [x] Expired claim token on approve/reject/deliver-manual/submit-manual returns 410 Gone
- [ ] Auth 401/403 documented and tested for HITL claim/approve/reject endpoints

## Resilience & Integration Robustness

- [x] Pipeline name lookups inside session.begin() transaction for RLS consistency
- [x] All 7 route handlers have except ProgrammingError → 501
- [x] All 7 route handlers have except SQLAlchemyError → 503
- [ ] claim_gate notifies on NotTeamMemberError (403) consistently logged
- [ ] PipelineExecutor.resume() exceptions not caught in route handlers (propagates as 500)
- [ ] No retry/backoff on HITL DB operations

## QA History

- 2026-07-06: feat-pipelines-hitl-gates cross-cutting QA (arch-230): Fixed CRITICAL — claim_gate route missing NotTeamMemberError→403 catch (non-member team-scoped claim returned 500 instead of 403). Fixed CRITICAL — all 7 route handlers added SQLAlchemyError→503 catches (connection/deadlock failures propagated as 500). Fixed MAJOR — submit_manual_output returned 403 for expired tokens instead of 410 (inconsistent with all other HITL decision routes). Fixed MAJOR — pipeline name lookups moved inside session.begin() in list_run_pending_gates and list_org_pending_gates. Updated product map: marked 2 [ ]→[x], added Resilience section (6 checkboxes: 4 [x] + 2 [ ]). Added 14 SQLAlchemyError→503 + NotTeamMemberError→403 tests.

## Known Gaps

- [ ] Eval-reference condition format (§8.17) not yet implemented — code uses JMESPath only
- [ ] Eval results from eval-before-interrupt not logged/persisted
- [ ] PRD specifies `condition` field as `{eval_id, threshold, operator}` but code
  implements JMESPath `condition` and `eval_condition` as separate fields
- [ ] No end-to-end integration test for full eval-before-interrupt → suite check → eval_failed chain
- [ ] Frontend pipeline editor lacks edge properties panel entirely
