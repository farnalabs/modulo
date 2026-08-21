---
id: feat-variants-variant-execution
prd: 8.19
delivery-tasks: [task-nv3-variant-run]
bdd:
  - backend/tests/bdd/features/pipelines/run_variants.feature
  - backend/tests/bdd/features/variants/variant_groups.feature
code:
  - backend/src/modulo/api/routes/variants.py
  - backend/src/modulo/db/crud/variant_group.py
unit-tests:
  - backend/tests/unit/api/test_variants.py
  - backend/tests/unit/db/crud/test_variant_group.py
  - backend/tests/bdd/steps/test_variant_groups.py
  - backend/tests/integration/crud/test_variant_group.py
depends-on: [feat-variants-variant-groups, feat-core-run-context]
status: partial
---

# Variant Execution

Weighted random variant selection, run_context_overrides merging, quota enforcement, and run creation for A/B test variant groups.

## Behaviours

### Run creation

- [x] `POST /api/v1/variant-groups/{group_id}/run` selects a variant, merges `run_context_overrides` into `input_payload`, and creates a run with the variant's `snapshot_id`
- [x] `snapshot_id` accepted as both `str` and `uuid.UUID`
- [x] `degraded_evals=true` injects `_degraded_evals: True` into the merged payload
- [x] `trigger_type` defaults to `"manual"` for variant-triggered runs
- [x] Run count incremented on the variant group after each successful run via `increment_run_count`
- [x] `increment_run_count` uses `with_for_update()` on the variant group row for safe concurrent increment
- [x] `run_variant_weighted` re-locks the variant group row with `with_for_update()` before quota check and run creation (prevents TOCTOU races)
- [x] Response includes `run_id`, `variant_name`, and `merged_payload`
- [x] RLS org context set on the endpoint via `set_rls_org`

### Batch run (`POST /api/v1/variant-groups/{group_id}/batch-run`)

- [x] Fires one run per variant (N variants → N runs), each sharing the same `input_payload` with the variant's `run_context_overrides` merged on top
- [x] Runs are created in variant insertion order (sequential)
- [x] Response `{runs: [{run_id, variant_name, merged_payload}], count}` where `count == len(variants)`
- [x] All-or-nothing pre-flight before any run is created: no variants, any variant missing `snapshot_id`, or quota breach rejects the whole group — no partial firing
- [x] `check_pipeline_run_quota_for_batch` requires `active + N <= max_concurrent_runs` (headroom for the entire batch, not just one run)
- [x] 429 `variant_group_quota_exceeded` returned when the batch would breach the pipeline concurrent-run quota (all runs rejected, none fired)
- [x] `_degraded_evals` flag injected into every batch run when the group has `degraded_evals` enabled
- [x] `run_count` incremented by N (batch size) in one locked update via `increment_run_count(delta=N)`
- [x] Same error contract as the single-run endpoint (404 group, 429 no-variants, 409/501/503/500)

### Quota enforcement

- [x] `check_pipeline_run_quota` returns True when `active < max_concurrent_runs`
- [x] `check_pipeline_run_quota_for_batch` returns True when `active + batch_size <= max_concurrent_runs`
- [x] 429 returned when pipeline concurrent run quota exceeded
- [x] 429 returned when no variant selected or quota exceeded in `run_variant_weighted`

### Error Handling

See [`variant-groups.md`](variant-groups.md#error-handling) for the canonical error handling patterns shared across all variant route handlers (ProgrammingError→501, SQLAlchemyError→503, IntegrityError→409, Exception→500, 404, 429, row locking).

## QA History

- 2026-07-06: qa-iterate — Fixed MAJOR: removed duplicated Weighted selection, Coverage gap detection, and Prompt diff comparison behaviours (canonical versions in variant-groups.md). Status: partial.
- 2026-08-13: improve-architecture — **RESOLVED 2 known gaps** — "PRD 8.19 specifies batch firing N variants (all-or-nothing pre-flight)" + "No all-or-nothing N-variant quota pre-flight". New `POST /api/v1/variant-groups/{group_id}/batch-run` endpoint (`run_variant_batch` in `db/crud/variant_group.py`) fires one run per variant in insertion order with an all-or-nothing pre-flight (`check_pipeline_run_quota_for_batch`, `active + N <= max_concurrent_runs`); any pre-flight failure rejects the whole group with 429 `variant_group_quota_exceeded` — no partial firing. `increment_run_count` gained a `delta` param (run_count += N). 11 new CRUD unit tests (`TestRunVariantBatch` ×7 + `TestCheckPipelineRunQuotaForBatch` ×3 + `increment_run_count` delta) + 6 new route unit tests (`TestRunVariantBatch`) + 2 BDD scenarios in `variant_groups.feature` promoted from `@awaiting-implementation` to wired ("Batch run fires one run per variant in insertion order", "Batch run is rejected when quota is exceeded") with the batch `when` step now driving the real CRUD function. 91 CRUD + 74 route unit tests + 3/3 `variant_groups.feature` BDD scenarios pass; ruff check + format clean; mypy --strict clean; bandit clean. Status: partial (HITL partial completion, cancel/abandon variant, frontend, website docs, skipped integration tests remain).

## Known Gaps

- PRD 8.19 specifies partial completion with HITL but no HITL-aware execution handling exists
- PRD 8.19 specifies cancel/abandon variant endpoint — not implemented
- No frontend exists for variant group creation or coverage signal UI (comparison view exists as VariantCompareView.vue)
- No website docs page for variant execution (variant-groups.md exists but no execution-specific page)
- Integration tests skipped (all `@pytest.mark.skip`)
