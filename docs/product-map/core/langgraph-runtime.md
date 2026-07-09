---
id: feat-core-langgraph-runtime
prd: 6.5
delivery-tasks: []
bdd: []
code:
  - backend/src/modulo/core/pipeline_engine/
unit-tests: []
depends-on: []
status: partial
---

# LangGraph Runtime

LangGraph-based pipeline runtime — StateGraph compilation, execution, snapshot management, checkpointing. This is the runtime engine itself, not the pipeline features built on top of it.

## Behaviours

- [ ] §6.5 StateGraph compilation — PipelineSnapshot compiles to a LangGraph `StateGraph` at run-start
- [ ] §6.5 Graph validation before execution
- [ ] §6.5 AsyncPostgresSaver/AsyncSqliteSaver checkpointing
- [ ] §6.5 `interrupt()` for HITL gates
- [ ] §6.5 `astream_events()` for real-time progress
- [ ] §6.5 State type is `dict[str, Any]` — no dynamic TypedDicts
- [ ] §6.5 Schema validation as Modulo-layer pre/post steps outside LangGraph type system
- [ ] §6.5 Compiled graph caching keyed by `(pipeline_id, snapshot_id)` with LRU eviction
- [ ] §6.5 Alembic `upgrade head` before `AsyncPostgresSaver.setup()` on startup
- [ ] §6.5 Async drivers only in async path — `asyncpg` for Postgres, `aiosqlite` for SQLite
