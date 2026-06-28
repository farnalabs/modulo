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
unit-tests: [backend/tests/unit/core/test_pipeline_engine.py]
depends-on: [feat-core-agent-model, feat-core-schema-system, feat-core-trigger-system]
status: partial
---

# Pipeline Execution

StateGraph-based pipeline executor. Compiles pipeline config into a LangGraph graph,
manages node dispatch, checkpointing, event emission, and HITL interrupt/resume.

## Behaviours

### Graph Compilation & Startup
- [x] Pipeline with valid config compiles to StateGraph
- [x] Invalid DAG (cycle, disconnected node) → validation error pre-run
- [ ] Missing entry node → validation error
- [ ] Node references nonexistent model_backend_id → error
- [x] Node references nonexistent connector_binding → error
- [ ] Graph cache hit reuses compiled graph (validated by snapshot_id hash)

### Node Execution
- [x] agent node runs and produces output artifact
- [x] manual node completes (no dispatch, just log)
- [x] Node output promoted to next node's input
- [ ] Node execution timeout → node_timeout error, run marked failed
- [x] cancellation via @cancellable_node decorator → clean abort
- [x] Node retry policy: max_retries, retry_on, backoff
- [ ] All node types exhausted → run marked complete
- [ ] Eval gate blocks → output NOT promoted, run marked eval_failed
- [ ] Eval gate warns → output promoted, warning recorded

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

### Edge Cases
- [ ] Empty pipeline (no nodes) → what happens? (should be validation error on save, not at run-time)
- [ ] Node returns None output → handled gracefully or crashes?
- [ ] Post-HITL model backend unreachable → retry vs fail vs HITL re-engage?
- [ ] Two simultaneous runs of same pipeline → isolated state, no cross-contamination
- [ ] Checkpoint restore with schema migration applied → old snapshots still load (version compatibility)
- [ ] WebSocket reconnect mid-run → event replay catches client up
- [ ] `cancelled` state mechanics: in-flight node finishes? or is interrupted mid-execution?
- [ ] Lock wait timeout: run queued behind another on same pipeline/agent → timeboxed or indefinite?

### Error Handling
- [ ] Model backend returns non-JSON → parsed gracefully, error in run detail
- [ ] Connector hub decrypt fails → run marked failed, credential not logged
- [ ] StateGraph compile error → validation error, not 500
- [ ] DB connection lost mid-run → what happens to the in-memory graph state?
- [ ] OTel exporter unavailable → non-fatal, run continues (currently: critical raise_error fix applied but verify)

## Known Gaps
- No per-node execution timeout in executor (the PRD specifies it but `asyncio.wait_for` pattern not wired)
- Lock wait timeout not implemented (run queues but no timeout mechanism)
- Empty pipeline not validated at save time
- Cancellation mid-HITL: cancelled claim returns to available, but run status is ambiguous
