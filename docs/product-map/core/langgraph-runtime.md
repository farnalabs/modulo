---
id: feat-core-langgraph-runtime
prd: 6.5
delivery-tasks: []
bdd:
  - backend/tests/bdd/features/pipelines/run_sequential.feature
  - backend/tests/bdd/features/pipelines/run_lifecycle.feature
  - backend/tests/bdd/features/pipelines/run_context.feature
  - backend/tests/bdd/features/pipelines/concurrency.feature
  - backend/tests/bdd/features/pipelines/error_recovery.feature
  - backend/tests/bdd/features/pipelines/conditional_transitions.feature
  - backend/tests/bdd/features/pipelines/node_types.feature
  - backend/tests/bdd/features/pipelines/validation.feature
  - backend/tests/bdd/features/pipelines/checkpoint_resume.feature
  - backend/tests/bdd/features/pipelines/run_variants.feature
code:
  - backend/src/modulo/core/pipeline_engine/
unit-tests:
  - backend/tests/unit/pipeline_engine/test_graph_cache.py
  - backend/tests/unit/pipeline_engine/test_graph_cache_hitl.py
  - backend/tests/unit/pipeline_engine/test_executor.py
  - backend/tests/unit/pipeline_engine/test_conditional_transitions.py
  - backend/tests/unit/pipeline_engine/test_conditional_transitions_audit_events.py
  - backend/tests/unit/pipeline_engine/test_context_setter_enforcement.py
  - backend/tests/unit/pipeline_engine/test_decorator.py
  - backend/tests/unit/pipeline_engine/test_event_broker.py
  - backend/tests/unit/pipeline_engine/test_input_truncation.py
  - backend/tests/unit/pipeline_engine/test_manual_node.py
  - backend/tests/unit/pipeline_engine/test_modulo_saver.py
  - backend/tests/unit/pipeline_engine/test_node_runner_hitl.py
  - backend/tests/unit/pipeline_engine/test_output_filter.py
  - backend/tests/unit/pipeline_engine/test_pipeline_composition.py
  - backend/tests/unit/pipeline_engine/test_recovery.py
  - backend/tests/unit/pipeline_engine/test_run_crud.py
  - backend/tests/unit/core/test_pipeline_engine.py
depends-on:
  - feat-connectors-hub
  - feat-model-backends-hub
status: partial
---

# LangGraph Runtime

LangGraph-based pipeline runtime — StateGraph compilation, execution, snapshot management, checkpointing. This is the runtime engine itself, not the pipeline features built on top of it.

## Behaviours

- [x] §6.5 StateGraph compilation — PipelineSnapshot compiles to a LangGraph `StateGraph` at run-start
- [x] §6.5 Graph validation before execution
- [x] §6.5 AsyncPostgresSaver/AsyncSqliteSaver checkpointing
- [x] §6.5 `interrupt()` for HITL gates
- [x] §6.5 `astream_events()` for real-time progress
- [x] §6.5 State type is `dict[str, Any]` — no dynamic TypedDicts
- [x] §6.5 Schema validation as Modulo-layer pre/post steps outside LangGraph type system
- [x] §6.5 Compiled graph caching keyed by `(pipeline_id, snapshot_id)` with LRU eviction
- [x] §6.5 Alembic `upgrade head` before `AsyncPostgresSaver.setup()` on startup
- [x] §6.5 Async drivers only in async path — `asyncpg` for Postgres, `aiosqlite` for SQLite

## Error Handling

- [x] Graph compilation errors caught and surface as run-failure with detail
- [x] Checkpoint persistence errors caught and logged
- [x] Node execution errors captured per-node in run state
- [x] `CancelledError` propagation via `@cancellable_node` decorator
- [x] `set_rls_org` called without `session.begin()` in `_do_db_cancellation_check` — relies on autobegin, will fail if disabled
- [ ] No explicit `IntegrityError` or `SQLAlchemyError` routing in engine-level catch blocks

## Edge Cases

- [x] Empty pipeline graph (zero nodes) — rejected at validation stage
- [x] Single-node pipeline — executes correctly
- [x] Pipeline with all node types (agent, manual, sandbox_agent, router, trigger)
- [x] LRU cache eviction on full cache — stale graphs gracefully recompiled
- [ ] Concurrent checkpoint writes — no explicit isolation testing
- [ ] State growth beyond memory limit — no enforced cap on `dict[str, Any]` state

## Security

- [x] RLS context set per-run — cross-org isolation enforced
- [x] `run_context` write guard prevents non-context-setter agents from writing
- [ ] No per-run credential isolation beyond ConnectorHub one-decrypt lifecycle
- [ ] Checkpoint data may contain state from previous runs before LRU eviction

## Known Gaps
