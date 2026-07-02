---
id: feat-pipelines-cicd-pipeline
prd: 8.4
delivery-tasks: [task-nv12-cicd-pipeline]
bdd:
  - backend/tests/bdd/features/pipelines/run_lifecycle.feature
  - backend/tests/bdd/features/pipelines/crud.feature
  - backend/tests/bdd/features/pipelines/pipeline_config_validation.feature
  - backend/tests/bdd/features/pipelines/checkpoint_resume.feature
  - backend/tests/bdd/features/pipelines/node_types.feature
  - backend/tests/bdd/features/pipelines/error_recovery.feature
  - backend/tests/bdd/features/pipelines/scheduling.feature
  - backend/tests/bdd/features/pipelines/webhook_trigger.feature
  - backend/tests/bdd/features/pipelines/run_variants.feature
code:
  - backend/src/modulo/core/pipeline_engine/executor.py
  - backend/src/modulo/core/pipeline_engine/node_runner.py
  - backend/src/modulo/core/pipeline_engine/graph_cache.py
  - backend/src/modulo/core/pipeline_engine/decorator.py
  - backend/src/modulo/core/pipeline_engine/event_broker.py
  - backend/src/modulo/core/pipeline_engine/modulo_saver.py
unit-tests:
  - backend/tests/unit/api/test_pipelines_endpoint.py
  - backend/tests/unit/api/test_pipeline_copy_errors.py
depends-on: []
status: partial
---

# CI/CD Pipeline

Executes a pipeline run end-to-end: seed state from snapshot, compile StateGraph,
enforce concurrency limits, stream events, handle HITL interrupts, eval thresholds,
and checkpoint resume.

## Behaviours

### Pipeline CRUD

- [x] Create a pipeline with name and valid config — 201 with id and slug
- [x] List all pipelines in org — returns full collection
- [x] Get pipeline by id — returns pipeline name and config
- [x] Update pipeline config via PATCH — 200 on success
- [x] Delete pipeline — 204, pipeline no longer exists

### Pipeline Config Validation

- [x] Missing required field (e.g. `nodes`) rejected with 422 and field-level error
- [x] Unknown node type rejected with 422
- [x] Cycle in node graph rejected with 422 and "cycle" in error message
- [x] Valid minimal pipeline (single LLM node) accepted with 201
- [x] Deprecated schema version pinned — soft warning (run proceeds)
- [x] Schema reference missing at pinned version — hard block
- [x] ConnectorBinding missing required operation — hard block with `connector_capability_mismatch`
- [x] ModelBackend missing or stale (>5 min since health check) — hard block with `model_backend_unavailable`

### Run Lifecycle

- [x] Trigger a run via POST /api/pipelines/{id}/runs — 202 with status "pending"
- [x] Run transitions "pending" → "running" when engine picks it up
- [x] All nodes complete without error — status "completed" with final_state
- [x] Node raises unhandled exception — status "failed" with error_detail
- [x] Run context merged correctly (defaults + per-run overrides)
- [x] Pre-run input validation against entry agent's input_schema — field-level errors returned immediately

### Concurrent Run Capacity

- [x] Run waits when pipeline max_concurrent_runs is reached — polls until slot available
- [x] Slot wait exceeds lock_wait_seconds — status "failed" with error_code "lock_timeout"
- [x] Capacity check serialised via SELECT FOR UPDATE on pipeline row (TOCTOU prevention)

### Cancellation

- [x] Run cancellation requested before node starts — status "cancelled", node not executed
- [x] DB-backed cancellation check via set_cancellation_check hook (authoritative source)
- [x] State-based fast-path cancellation check (run_context.cancelled)

### Checkpoint and Resume

- [x] State checkpointed after each node via AsyncPostgresSaver
- [ ] Resume from checkpoint after failure — restarts from failed node, prior nodes not re-executed (resume API endpoint does not exist yet)
- [x] Checkpoints written to PostgreSQL checkpoints table
- [ ] Resume after HITL interrupt — injects _hitl_decision via aupdate_state (resume API endpoint does not exist yet)
- [ ] Snapshot re-validated on resume before execution continues (resume API endpoint does not exist yet)

### HITL Gate

- [x] Edge with hitl_gate_config inserts intermediate gate node in compiled graph
- [x] Gate with autonomy "manual_approval" — interrupts for human review via NodeInterrupt
- [x] Gate with autonomy "notify_on_complete" — auto-approves, records artifact, no interrupt
- [x] Gate with autonomy "fully_autonomous" — silently skipped
- [x] Gate with human_only=true — overrides autonomy, always interrupts
- [x] Gate on resume — reads _hitl_decision, records approved/rejected result
- [x] Rejected HITL decision routed to reject_target via kick-back conditional edge
- [x] Conditional gate with JMESPath condition — skipped when condition falsy
- [x] Eval-before-interrupt — eval definitions evaluated before interrupt; block evals raise EvalBlockedError

### Manual Node

- [x] Manual node pauses run and waits for human output via HITL UI
- [x] On resume, manual output validated against output_schema_id (required field check)
- [x] Manual output recorded as artifact in state
- [x] Manual node has no agent_id, connector_binding, or model_backend_id

### Node Types

- [x] Standard (agent) node — runs agent/connector body
- [x] Manual node — placeholder for human SDLC steps, interrupts immediately
- [x] HITL gate node — intermediate node per edge with gate config

### Per-Node Execution Controls

- [x] Per-node timeout via asyncio.wait_for — TimeoutError propagated to run state
- [x] Context-setter guard — only nodes with role="context_setter" may write run_context
- [x] Context-setter violation raises ContextSetterViolationError
- [x] Run-context write log with last-write-wins semantics (_run_context_write_log)

### Graph Compilation and Caching

- [x] StateGraph compiled from snapshot graph_json via build_graph_from_json
- [x] In-memory LRU cache (256 entries) keyed by (pipeline_id, snapshot_id)
- [x] Per-key locking prevents double compilation for concurrent access
- [x] Conditional edge routing via JMESPath-based router
- [x] Reject edge routing: _hitl_decision.action == "rejected" routes to reject_target

### Event Streaming

- [x] Real-time events consumed from astream_events() and published per-run via RunEventBroker
- [x] Events: node_started, node_completed, node_failed, hitl_awaiting, run_completed, run_failed, run_cancelled
- [x] WebSocket fan-out via subscriber queues (weak references, auto-cleanup on disconnect)
- [x] 100-event ring buffer for reconnection replay (replay_since)
- [x] Broker closed on run terminal state — None sentinel to all subscribers
- [x] BrokerRegistry singleton — one broker per active run

### Token and Cost Tracking

- [x] Token usage collected per-node from on_chat_model_end / on_llm_end events
- [x] Total token count and cost computed on run completion
- [x] Cost based on input_rate ($0.00001/token) and output_rate ($0.00003/token)
- [x] Token/cost persisted to Run row via update_run_status

### Eval Suite Thresholds

- [x] Completed run checks eval suites with pass_threshold
- [x] Suite below threshold — status downgraded to "failed" with error_code "eval_suite_blocked"
- [x] Multiple suites evaluated independently per completed run

### Multi-Tenant Checkpoints (ModuloPostgresSaver)

- [x] All checkpoint tables include organisation_id column
- [x] Queries filtered by organisation_id — tenant isolation
- [x] Checkpoint JSON encrypted at rest via Fernet
- [x] Migration SQL creates org-scoped tables and indexes

### WebSocket Patch Events

- [x] Events carry typed patch payloads for Pinia store patching
- [x] node_started payload: run_id, node_id, started_at
- [x] node_completed payload: run_id, node_id, output_summary, completed_at
- [x] node_failed payload: run_id, node_id, error_code, error_message, failed_at
- [x] hitl_awaiting payload: run_id, gate_id, node_id, human_only, required_team_id
- [x] hitl_claimed payload: run_id, gate_id, claimed_by_name, claimed_at
- [x] hitl_reviewed payload: run_id, gate_id, action
- [x] run_completed payload: run_id, terminal_status, completed_at

### Error Paths

- [x] RunNotFoundError — returns "failed" with error_code="KeyError" and error_detail
- [x] GraphValidationError — blocks run start, transitions run to "failed" with error_code="GraphValidationError"
- [x] NodeInterrupt — transitions run to "awaiting_human" with gate payload
- [x] Runaway run (exceeds max steps) — terminates with "runaway_max_steps"
- [x] Runaway run (exceeds max duration) — terminates with "runaway_max_duration"
- [x] Node timeout via asyncio.wait_for — transitions to "failed" with error_code="node_timeout"
- [x] Capacity timeout (lock_wait_seconds exceeded) — "failed" with error_code="lock_timeout"
- [x] Eval suite threshold not met — "failed" with error_code="eval_suite_blocked"
- [x] ContextSetterViolationError — blocked node, error propagated to run state
- [x] Missing connector capability — hard block with `connector_capability_mismatch`
- [x] Stale model backend (>5 min health check) — hard block with `model_backend_unavailable`

## Known Gaps

### @awaiting-implementation BDD scenarios

- webhook_trigger.feature — 5/5 scenarios tagged @awaiting-implementation (entire webhook trigger feature not yet wired to real endpoints)
- scheduling.feature — 1/5 scenarios tagged @awaiting-implementation (cron trigger fire scenario)
- run_variants.feature — 1/5 scenarios tagged @awaiting-implementation (coverage gaps scenario)

### Resume API not yet implemented

- Resume from checkpoint, resume after HITL interrupt, and snapshot re-validation on resume all depend on a POST /api/runs/{run_id}/resume endpoint that does not exist yet. BDD steps explicitly note the test produces 404/501 until implemented.

### Agent Theming and Agent Theme ViewModel

- GET /api/v1/viewmodel/current endpoint specified in PRD 8.4 but test coverage not confirmed
- ?mode=agent theme mode not exercised by any BDD scenario

### CopyToAdaptWizard

- Multi-step modal component specified in PRD 8.4 but not yet covered by pipeline-related BDD tests

### Canvas State Across Drill-Down

- Viewport persistence per drill-down level via Vue Router state — no test coverage

### Stage Board

- Stage board kanban with search/filter/sort — not covered by pipeline feature tests

## QA History

### Index 61 (2026-07-02): Cross-cutting QA
- Marked 60+ implemented behaviours [ ]→[x] across all sections (CRUD, Validation, Run Lifecycle, Concurrency, Cancellation, Checkpoint/Resume, HITL Gate, Manual Node, Node Types, Execution Controls, Graph Compilation, Event Streaming, Token/Cost, Eval Suites, Multi-Tenant, WebSocket Patch, Error Paths)
- Added Error Paths section with 11 behaviour checkboxes
- Removed 5 stale Known Gaps (node_types.feature, error_recovery.feature, scheduling.feature, webhook_trigger.feature, run_variants.feature — all have real BDD content, not placeholders)
- Added 3 new Known Gaps documenting @awaiting-implementation tags (webhook_trigger 5/5, scheduling 1/5, run_variants 1/5)
- Added "Resume API not yet implemented" Known Gap (checkpoint/resume behaviours remain [ ])
- Fixed CRITICAL silent data corruption in modulo_saver.py _decrypt_checkpoint (re-raise on decrypt failure instead of returning garbage)
- Fixed HIGH _compile_locks memory leak in graph_cache.py (evict locks with cache entries)
- Fixed HIGH unhandled JMESPathError in node_runner.py (wrapped jmespath.compile in try/except)
- Fixed HIGH BrokerRegistry memory leak in event_broker.py (added stale broker cleanup with TTL)
- Fixed MEDIUM silent exception swallowing in executor.py _stream_graph (added _log.exception)
- Fixed MEDIUM missing Fernet key warning in modulo_saver.py (warn on plaintext storage)
- Added unit-tests frontmatter with 2 unit test file refs
- Status: partial (8 known gaps remain — 3 @awaiting-implementation BDD features, resume API unimplemented, agent theming, CopyToAdaptWizard, canvas state, stage board)
- 424/424 unit tests pass
