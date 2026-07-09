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

## Error Handling

- [ ] Capacity timeout (lock_wait_seconds exceeded) transitions run to failed with error_code lock_timeout
- [ ] Missing DB table returns 501 Not Implemented on run trigger
- [ ] Auth 401/403 enforced on run trigger endpoints
- [ ] 422 validation for invalid concurrency parameters

## Edge Cases

- [ ] max_concurrent_runs=1 behaves as sequential execution
- [ ] Concurrent runs at limit with one completing — next waiting run starts
- [ ] Run cancellation while waiting for lock releases lock and transitions to cancelled
- [ ] lock_wait_timeout_seconds at minimum (30) and maximum (3600) bounds
- [ ] Multiple pipelines each at their own concurrency limit — independent capacity pools

## Known Gaps

- No per-trigger concurrency limit implemented (only per-pipeline)
- No advisory lock implementation for connector instance + target resource
- No `waiting_for_lock` sub-state in run status model
- No BDD or unit test coverage for concurrency behaviours
- No integration test for concurrent run serialisation

## QA History

- 2026-07-09: Second-pass product map QA (feat-pipelines-run-concurrency): Verified pipeline model implements max_concurrent_runs (default=5, CHECK >0), lock_wait_timeout_seconds (default=300, CHECK 30-3600), node_timeout_seconds. Executor enforces max_concurrent_runs via SELECT FOR UPDATE on pipeline row. All 7 PRD behaviours remain [ ] as the concurrency scope is forward-looking (per-trigger limits, advisory locks, waiting_for_lock sub-state not yet implemented). Added Error Handling, Edge Cases, Known Gaps, and QA History sections.
