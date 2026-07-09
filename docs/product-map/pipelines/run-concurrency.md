---
id: feat-pipelines-run-concurrency
prd: 8.7
delivery-tasks: []
bdd: []
code:
  - backend/src/modulo/core/pipeline_engine/
  - backend/src/modulo/db/models/pipeline.py
unit-tests: []
depends-on:
  - feat-core-pipeline-execution
status: partial
---

# Run Concurrency Controls

Max concurrent runs per pipeline, lock wait timeout, node timeout, admission control. Prevents resource exhaustion and shared-state corruption from concurrent pipeline runs.

## Behaviours

- [ ] §8.7 `max_concurrent_runs` per pipeline — new run requests blocked at limit (default: 5)
- [ ] §8.7 `max_concurrent_runs` per trigger — new trigger fires blocked at limit (default: 1)
- [ ] §8.7 Write lock per connector instance + target resource — advisory lock via `pg_try_advisory_lock`
- [ ] §8.7 `waiting_for_lock` sub-state when lock cannot be acquired
- [ ] §8.7 `lock_wait_timeout_seconds` per pipeline (default: 300, min: 30, max: 3600)
- [ ] §8.7 Lock timeout transitions run to `failed` with error code `lock_wait_timeout`
- [ ] §8.7 Cancel from `waiting_for_lock` releases via `pg_advisory_unlock` and transitions to `cancelled`
