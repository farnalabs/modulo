---
id: feat-evals-eval-definitions
prd: 8.17
delivery-tasks: [task-nv2-eval-definition]
bdd:
  - backend/tests/bdd/features/eval/eval_run.feature
  - backend/tests/features/evals/eval_regex.feature
  - backend/tests/features/evals/eval_llm_judge.feature
  - backend/tests/features/evals/eval_block.feature
  - backend/tests/features/evals/conditional_hitl.feature
code:
  - backend/src/modulo/db/models/eval_definition.py
  - backend/src/modulo/db/models/eval_result.py
  - backend/src/modulo/api/routes/evals.py
  - backend/src/modulo/core/eval_engine/__init__.py
depends-on: [feat-pipelines-core]
status: partial
---
# Eval Definitions

Eval definitions describe automated quality checks that run as a post-node step within the LangGraph StateGraph (8.17). Each definition specifies an eval type, config, pass threshold, and failure behaviour.

## Behaviours

### Eval Definition CRUD
- [x] Admin can create an eval definition with pipeline_id, name, eval_type, and optional node_id, config_json, failure_behaviour, pass_threshold, suite_id
- [x] Admin can create an eval definition with only required fields (pipeline_id, name, eval_type)
- [x] Non-admin user (runner) receives 403 when creating eval definition
- [x] Unauthenticated user receives 401/403 when creating eval definition
- [x] Invalid eval_type returns 422
- [x] Admin can list all eval definitions for their org, paginated
- [x] Admin can list eval definitions filtered by pipeline_id
- [x] Empty list returns 200 with total=0, items=[]
- [x] Admin can get a single eval definition by ID
- [x] Non-existent eval definition ID returns 404
- [x] Admin can update an eval definition (name, pass_threshold, suite_id, etc.)
- [x] Non-admin receives 403 when updating eval definition
- [x] Update on non-existent ID returns 404
- [x] Admin can delete an eval definition
- [x] Non-admin receives 403 when deleting eval definition
- [x] Delete on non-existent ID returns 404
- [ ] Eval definition "from-run" endpoint creates a definition pre-populated from run output with type-specific config stubs
- [ ] Eval coverage endpoint returns per-node coverage map for a pipeline ### Eval Definition Fields
- [x] Eval definition has fields: id, organisation_id, pipeline_id, name, eval_type, config_json, failure_behaviour, created_by
- [x] Optional fields: node_id, pass_threshold, suite_id
- [x] eval_type must be one of: llm_judge, regex, json_schema, custom_function (DB CHECK constraint)
- [x] failure_behaviour must be one of: warn, block (DB CHECK constraint)
- [x] pass_threshold is a nullable float (0.0–1.0)
- [x] suite_id is a nullable string for grouping evals into suites
- [x] eval_definitions cascade-delete when parent pipeline is deleted ### Eval Engine — Regex
- [x] Regex pattern matched on output field returns passed=true with score 1.0
- [x] Regex pattern not matched on output field returns passed=false with score 0.0
- [x] Regex eval on a nested/missing field coerces value to string
- [x] Missing "pattern" in config returns passed=false with score 0.0
- [x] Missing "field" in config returns passed=false with score 0.0
- [x] Regex match can be found anywhere in the field value (re.search, not full match) ### Eval Engine — LLM Judge
- [x] LLM judge returns score and passed based on rubric callable
- [x] Score above pass_threshold marks eval as passed
- [x] Score below pass_threshold marks eval as failed
- [x] LLM judge uses a dedicated model_backend_id (independent of agent's backend)
- [x] Custom rubric prompt is sent to the judge model
- [x] Rubric prompt treats agent output as untrusted
- [x] Missing llm_judge callable returns passed=false with score 0.0 ### Eval Engine — JSON Schema
- [x] JSON Schema validation on output field passes when data matches schema
- [x] JSON Schema validation fails with descriptive message when data does not match
- [ ] JSON Schema eval with no schema defined returns appropriate failure ### Eval Engine — Custom Function
- [x] Custom function from registry is called with output and config
- [x] Missing function in registry returns passed=false
- [x] Custom function exception returns passed=false with error detail
- [ ] Custom function can return passed, score, and detail ### Failure Behaviour
- [x] failure_behaviour="block" raises EvalBlockedError when eval fails
- [x] EvalBlockedError is caught by pipeline executor and run transitions to "eval_failed" with error_code "eval_blocked"
- [x] Block failure is recorded in AuditEvent with type "eval_blocked"
- [x] Multiple evals on one node — first block failure halts evaluation of remaining evals
- [x] failure_behaviour="warn" logs a warning and pipeline execution continues
- [x] Warn failure does not transition run to "eval_failed" ### Suite-Level Aggregation
- [x] evaluate_suite aggregates individual eval results into aggregate score
- [x] Aggregate score below pass_threshold marks suite as failed
- [x] Suite pass threshold met allows run to complete successfully
- [x] Suite with pass_threshold=None never blocks but still returns aggregate
- [x] Suite with no eval results returns aggregate_score=1.0 ### Eval Run Lifecycle
- [x] Triggering an eval run returns 202 with status "pending"
- [x] Eval run with all cases scored produces an aggregate score
- [x] Eval run scoring below suite pass_threshold results in status "failed"
- [x] Completed eval run results are visible per-case with aggregate score ### Conditional HITL Gating (v1)
- [x] Eval score below HITL gate threshold triggers NodeInterrupt, run transitions to "awaiting_human"
- [x] Eval score above HITL gate threshold skips the gate, execution continues without interrupt
- [x] Gate artifact contains "condition_skipped" when eval condition is false
- [x] JMESPath condition on run_context can skip or trigger the gate independently of evals
- [x] Eval block failure takes priority over HITL interrupt — run goes to "eval_failed", no interrupt
- [x] Gate resume from interrupt does not re-evaluate condition or re-run evals
- [x] HITL gate condition references eval by id with threshold and operator (lt)
- [x] Reject routing from conditional HITL gate routes to reject_target ### Auth & Org Scoping
- [x] All eval definition operations enforce org-level RLS
- [x] All eval result queries enforce org-level RLS
- [x] Admin role required for create, update, delete
- [x] Any authenticated user can list and read eval definitions within their org ### Edge Cases
- [ ] Eval definition with empty config_json defaults to empty dict
- [ ] Eval definition node_id can be null (pipeline-level eval, not node-specific)
- [ ] Deleted eval definition cascades to delete associated eval_results
- [ ] Eval "from-run" with missing run output creates an eval with empty config
- [ ] Coverage endpoint returns coverage_pct=0.0 when no nodes exist in pipeline ## Known Gaps
- Eval_suite_crud.feature is a placeholder — no CRUD BDD scenarios implemented yet
- Eval_scorer.feature is a placeholder — no multi-scorer dispatch tests
- Feedback_system.feature is a placeholder — no feedback record BDD tests
- No dedicated CRUD module in backend/src/modulo/db/crud/ — CRUD is inline in api/routes/evals.py
- eval_block.feature scenario "Block behaviour raises EvalBlockedError" has a semantic mismatch: regex pattern matches the output content, so the code sets passed=true and does NOT raise EvalBlockedError, but the BDD asserts EvalBlockedError is raised. The PRD defines "Pass = match found" for regex evals, so the BDD scenario name/assertions may need updating.
- No unit tests for EvalEngine.evaluate_json_schema or EvalEngine.evaluate_custom function paths
- No performance/stress tests for eval engine under many definitions per node 