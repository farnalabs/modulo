---
id: feat-core-pipeline-execution
prd: 8.4
delivery-tasks: [task-nv0-manual-node, task-nv0-snapshot-expansion]
bdd:
  - backend/tests/bdd/features/pipelines/run_lifecycle.feature
  - backend/tests/bdd/features/pipelines/node_types.feature
  - backend/tests/bdd/features/pipelines/checkpoint_resume.feature
  - backend/tests/bdd/features/pipelines/error_recovery.feature
code:
  - backend/src/modulo/core/pipeline_engine/
  - backend/src/modulo/core/graph_validator/
  - backend/src/modulo/db/crud/pipeline.py
  - backend/src/modulo/api/routes/runs.py
  - backend/src/modulo/api/routes/pipelines.py
  - backend/src/modulo/api/routes/stages.py
unit-tests:
  - backend/tests/unit/core/test_pipeline_engine.py
  - backend/tests/unit/api/test_pipeline_execution_programming_error.py
depends-on: [feat-core-agent-model, feat-core-schema-system, feat-core-trigger-system]
status: partial
---

# Pipeline Execution

StateGraph-based pipeline executor. Compiles pipeline config into a LangGraph graph, manages node dispatch, checkpointing, event emission, and HITL interrupt/resume.

## Behaviours

### Graph Compilation & Startup

- [x] Pipeline with valid config compiles to StateGraph
- [x] Invalid DAG (cycle, disconnected node) → validation error pre-run
- [x] Missing entry node → validation error
- [x] Node references nonexistent model_backend_id → error
- [x] Node references nonexistent connector_binding → error
- [x] Graph cache hit reuses compiled graph (keyed by (pipeline_id, snapshot_id) UUID)

### Node Execution

- [x] agent node runs and produces output artifact
- [x] manual node completes (no dispatch, just log)
- [x] Node output promoted to next node's input
- [x] Node execution timeout → TimeoutError raised, run marked failed (Python built-in TimeoutError, not custom node_timeout)
- [x] cancellation via @cancellable_node decorator → clean abort
- [ ] Node retry policy: max_retries, retry_on, backoff (not yet implemented)
- [x] All node types exhausted → run marked complete
- [x] Eval gate blocks → output NOT promoted, run marked "failed" with error_code="eval_suite_blocked"
- [x] Eval gate warns → output promoted, warning recorded (log warning, no error_code set)

### HITL Interrupt & Resume

- [x] HITL gate halts execution at node boundary
- [x] Claim → grant exclusive access with timeout
- [x] Approve → resume execution from the interrupted node
- [x] Reject → mark run rejected, fire feedback handler
- [x] Claim expires → auto-released, available for re-claim
- [x] human_only gate → blocks AI auto-approve

### Checkpoint & Resume

- [x] Run checkpoints after every node execution
- [x] Resume from last checkpoint after server restart
- [x] Snapshot captures pinned model_backend_id, not live entity
- [x] Model backend rotation during HITL delay → pinned version used on resume

### Event & Observability

- [x] Event emitted on node start/complete/fail
- [x] WebSocket fan-out for real-time UI updates
- [x] Audit event recorded for every run transition
- [x] OTel spans emitted via LangGraph callback handler
- [x] Graph validation error returns structured error (GraphValidationError), not 500
- [x] Runaway protection (max_duration, max_steps, token_budget) terminates runaway runs
- [x] Concurrent recovery prevention via SELECT FOR UPDATE + optimistic locking
- [x] Manual node output validated against output_schema_json before resume
- [x] Conditional gate evaluated via JMESPath condition expression against state
- [x] Eval-before-interrupt gate condition evaluated via operator+threshold against eval results
- [x] Input truncation for agent node max_input_length setting
- [x] NodeInterrupt (HITL) transitions to awaiting_human
- [x] EvalBlockedError transitions to eval_failed
- [x] OutputRejectedError transitions to output_rejected
- [x] RunawayRunError transitions to failed with error_code="runaway"
- [x] RunCancelledError transitions to cancelled

### Edge Cases

- [ ] Empty pipeline (no nodes) → `graph_cache.py` raises `ValueError` which becomes HTTP 500 in `executor.py`'s catch-all `except Exception` handler. Should be structured validation error pre-run (already in Known Gaps).
- [x] Node returns `None` output → handled gracefully, run continues to next node (BDD scenario in run_lifecycle.feature:35-39; unit test for _seed_state covers empty input)
- [ ] Post-HITL model backend unreachable → no explicit handling. The model backend is pinned in snapshot, but if the backend becomes unreachable between approval and resume, no retry/fail/fallback logic exists.
- [x] Two simultaneous runs of same pipeline → isolated state via SELECT FOR UPDATE + per-run thread_id, no cross-contamination (verified in executor.py `_wait_for_capacity_or_fail`)
- [ ] Checkpoint restore with schema migration applied → old snapshots still load (version compatibility). `ModuloPostgresSaver` has no schema-version field on checkpoints.
- [ ] WebSocket reconnect mid-run → `replay_since()` implemented in `event_broker.py:97` but has no BDD or unit test coverage.
- [x] `cancelled` state mechanics: cancellation is checked BEFORE node execution (in `cancellable_node` decorator via state flag and DB-backed check). In-flight node finishes normally; cancellation takes effect at the next node boundary. Verified in `decorator.py:86-94` and `executor.py:484-488`.
- [x] Lock wait timeout: timeboxed via `lock_wait_seconds` on pipeline config. `_wait_for_capacity_or_fail` polls with SELECT FOR UPDATE until deadline, then marks run as `failed` with error_code `lock_timeout`.
- [ ] Oversized pipeline graph (>500 nodes or >1000 edges) → rejected at Pydantic validation layer in `PipelineGraphUpdate.reject_database_conflicts`, returns 422 with descriptive message before any DB work.
- [x] Node timeout less than model backend latency → `TimeoutError` raised, run marked `failed` with `error_code="node_timeout"`. Verified in `_stream_graph` catch block and BDD scenario (error_recovery.feature:62-66).
- [ ] Concurrent `resume()` calls for the same run → `aupdate_state` followed by `astream_events` — no locking around the resume path. Two concurrent resumes could race.
- [ ] Graph cache key collision → key is `(pipeline_id, snapshot_id) UUID tuple` — astronomically unlikely but no bounds check on `_MAX_SIZE` (256 entry LRU). When full, eviction drops the oldest entry silently; no validation that eviction was intentional.
- [ ] Manual node resume with invalid output schema → `_validate_against_schema` raises `ValueError`. This becomes `failed` with `error_code="ValueError"` — confusing because it's a validation failure, not a system error. Should produce a domain-specific error code.

### Error Handling

- [ ] Model backend returns non-JSON → no handling in current stub node (model dispatch not yet plumbed fully). `_stream_graph` catches generic `Exception` which would catch a JSON decode error but the error code would be `JSONDecodeError`, not domain-specific.
- [ ] Connector hub decrypt fails → no explicit handling in pipeline_engine code. Connector hub exceptions would propagate to executor's catch-all `except Exception`.
- [x] StateGraph compile error → `ValueError` from `build_graph_from_json` (empty nodes, cycle, missing entry). Caught by `executor.py` catch-all, producing `error_code="ValueError"`. Not structured validation error. Pre-run validation via `GraphValidator` catches these before execution (checked in `validate_for_run`).
- [ ] DB connection lost mid-run → no explicit handling. `ModuloPostgresSaver` would raise connection error. In-memory graph state is lost if checkpointer is unreachable (already in Known Gaps).
- [x] OTel exporter unavailable → non-fatal, run continues (LangGraph defaults to raise_error=False for callbacks; verified)
- [x] `ProgrammingError` on pipeline snapshot routes → 501 Not Implemented (snapshot endpoints, graph replace, node conversion in pipelines.py)
- [x] `ProgrammingError` on run CRUD routes → all 14 routes in `runs.py` now have `ProgrammingError→501` handling (verified against code at `backend/src/modulo/api/routes/runs.py`).
- [x] `ProgrammingError` on pipeline CRUD routes → all 16 routes in `pipelines.py` now have `ProgrammingError→501` handling (verified against code at `backend/src/modulo/api/routes/pipelines.py`).
- [x] `ProgrammingError` on stage CRUD routes → all 5 routes in `stages.py` have the catch.
- [ ] Empty pipeline (no nodes) produces raw HTTP 500 instead of structured 422 → `graph_cache.py` raises `ValueError` which becomes 500 in `execute()`. Pre-run validation (`GraphValidator._check_topology`) catches this and returns `TOPOLOGY_NO_NODES` error, but `graph_cache` exception still fires if validation is somehow bypassed.
- [ ] Node retry policy referenced in pipeline config schema but NOT implemented in pipeline engine → no retry loop exists in `node_runner.py` or `executor.py`.

### Frontend i18n Issues

- [ ] RunDetailView.vue → 19 hardcoded English strings, line 107 node status missing `capitalize` class
- [ ] PipelineListView.vue → 25 hardcoded English strings
- [ ] PipelineEditorView.vue → 80+ hardcoded English strings (worst offender, only 1 `$t()` call in 874 lines)
- [ ] PipelineTemplateGallery.vue → 10 hardcoded English strings, locale keys exist but ignored in favour of inline text
- [ ] CompositeEditorView.vue → 15 hardcoded English strings, zero i18n usage, no locale section exists

## Known Gaps
- Node timeout raises `TimeoutError` (Python built-in), not a domain-specific `node_timeout` error code — confusing in API responses and logs.
- Cancellation mid-HITL: cancelled claim returns to available, but run status is ambiguous.
- Node retry policy (max_retries, retry_on, backoff) is specified in the pipeline config schema but is not implemented in the pipeline engine — no retry logic exists at the node execution or graph level.
- DB connection lost mid-run: no explicit handling, in-memory graph state is lost if checkpointer is unreachable.
- Checkpoint restore with schema migration: no version-compatibility check for old snapshots after schema changes.
- **Missing BDD for conditional gate**: The JMESPath-based conditional gate feature (in `graph_cache.py` + `node_runner.py`) has no BDD scenario.
- **Missing BDD for eval-before-interrupt**: The eval-before-interrupt feature in `node_runner.py` has no BDD scenario.
- **Missing BDD for node timeout**: The `@cancellable_node` timeout wrapper has no BDD scenario.
- **Empty pipeline (no nodes) produces raw 500**: `graph_cache.py` raises `ValueError` which becomes HTTP 500 instead of a structured validation error.
- **ProgrammingError→501 catches — FIXED**: All 14 run routes in `runs.py` and all 16 pipeline routes in `pipelines.py` now uniformly catch `ProgrammingError` on DB-accessing route handlers. No longer a gap.
- **Concurrent `resume()` for same run has no locking**: `executor.py:resume()` calls `aupdate_state` + `astream_events` without locking around the resume path — two concurrent resumes could race.
- **Manual node schema validation error produces `error_code="ValueError"`**: `_validate_against_schema` raises `ValueError` which becomes a confusing non-domain-specific error code in the run result.
- **No per-node output schema validation for agent nodes**: Only manual nodes validate output against `output_schema_json`. Agent node outputs are not schema-validated before promotion.
- **WebSocket reconnect replay not tested**: `replay_since()` exists but has no BDD or unit test coverage.
- **Post-HITL model backend unreachable has no fallback**: If the pinned model backend becomes unreachable between approval and resume, no retry/fail/fallback logic exists.

## QA History

| Date | Scope | Findings | Status |
|---|---|---|---|
| 2026-07-04 | Cross-cutting QA (6 lenses) | Behaviour completeness, edge case audit, error path audit, cross-module contract check, gap freshness, resilience auditing | [x] 30 behaviours verified [x] 33 error/edge case checkboxes added [x] 21 ProgrammingError catch sites added [x] 2 unit test files created [x] Known Gaps refreshed |
| 2026-07-05 | Cross-cutting QA — code verification pass | Verify stale product map claims against actual code; check ProgrammingError catches, executor error handling, frontend i18n | [x] All 14 run routes confirmed with ProgrammingError catches (product map was stale — claimed 13/14 missing) [x] All 16 pipeline routes confirmed with ProgrammingError catches (product map was stale — claimed 8/16 missing) [x] Executor error handling verified: NodeInterrupt→awaiting_human, EvalBlockedError→eval_failed, OutputRejectedError→output_rejected, RunCancelledError→cancelled, RunawayRunError→failed/runaway, TimeoutError→failed/node_timeout [x] 5 frontend run views audited for i18n [ ] Frontend i18n gaps documented (5 views, ~149 hardcoded strings) |
