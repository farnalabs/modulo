---
id: feat-pipelines-run-concurrency
prd: 8.7
delivery-tasks: []
bdd:
  - backend/tests/bdd/features/pipelines/concurrency.feature
code:
  - backend/src/modulo/core/pipeline_engine/
  - backend/src/modulo/db/models/pipeline.py
  - backend/src/modulo/core/trigger_engine/
unit-tests:
  - backend/tests/unit/pipeline_engine/test_run_crud.py
  - backend/tests/unit/api/test_runs_endpoint.py
depends-on:
  - feat-core-pipeline-execution
status: partial
---

# Run Concurrency Controls

Max concurrent runs per pipeline, lock wait timeout, node timeout, admission control. Prevents resource exhaustion and shared-state corruption from concurrent pipeline runs.

## Behaviours

- [x] §8.7 `max_concurrent_runs` per pipeline — new run requests blocked at limit (default: 5)
- [x] §8.7 `max_concurrent_runs` per trigger — new trigger fires blocked at limit (default: 1)
- [ ] §8.7 Write lock per connector instance + target resource — advisory lock via `pg_try_advisory_lock`
- [ ] §8.7 `waiting_for_lock` sub-state when lock cannot be acquired
- [x] §8.7 `lock_wait_timeout_seconds` per pipeline (default: 300, min: 30, max: 3600)
- [x] §8.7 Lock timeout transitions run to `failed` with error code `lock_wait_timeout`
- [ ] §8.7 Cancel from `waiting_for_lock` releases via `pg_advisory_unlock` and transitions to `cancelled`

## Error Handling

- [x] Capacity timeout (lock_wait_seconds exceeded) transitions run to failed with error_code lock_timeout
- [x] Missing DB table returns 501 Not Implemented on run trigger
- [x] Auth 401/403 enforced on run trigger endpoints
- [x] 422 validation for invalid concurrency parameters

## Edge Cases

- [x] max_concurrent_runs=1 behaves as sequential execution
- [x] Concurrent runs at limit with one completing — next waiting run starts
- [ ] Run cancellation while waiting for lock releases lock and transitions to cancelled
- [x] lock_wait_timeout_seconds at minimum (30) and maximum (3600) bounds
- [x] Multiple pipelines each at their own concurrency limit — independent capacity pools

## Known Gaps

- No advisory lock implementation for connector instance + target resource
- No `waiting_for_lock` sub-state transitions implemented (status value exists in model but is never entered)
- BDD concurrency tests use extensive mocking and target non-existent module paths — do not exercise real routes
- No integration test for concurrent run serialisation

## QA History

- 2026-07-09: Second-pass product map QA (feat-pipelines-run-concurrency): Verified pipeline model implements max_concurrent_runs (default=5, CHECK >0), lock_wait_timeout_seconds (default=300, CHECK 30-3600), node_timeout_seconds. Executor enforces max_concurrent_runs via SELECT FOR UPDATE on pipeline row. All 7 PRD behaviours remain [ ] as the concurrency scope is forward-looking (per-trigger limits, advisory locks, waiting_for_lock sub-state not yet implemented). Added Error Handling, Edge Cases, Known Gaps, and QA History sections.
- 2026-07-11: Third-pass product map QA (feat-pipelines-run-concurrency): Verified per-pipeline max_concurrent_runs (behaviour 1), per-trigger max_concurrent_runs (behaviour 2), lock_wait_timeout_seconds (behaviour 5), lock timeout → failed transition (behaviour 6) are all implemented. Per-trigger limits implemented in trigger_engine (webhooks, polling, agent_signal). Removed stale Known Gap about per-trigger limits. Updated bdd: frontmatter with concurrency.feature path. Updated unit-tests: frontmatter. BDD step defs patch non-existent module paths — documented as Known Gap.
