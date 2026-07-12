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
  - backend/tests/unit/api/test_variant_groups_bdd.py
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

### Quota enforcement

- [x] `check_pipeline_run_quota` returns True when `active < max_concurrent_runs`
- [x] 429 returned when pipeline concurrent run quota exceeded
- [x] 429 returned when no variant selected or quota exceeded in `run_variant_weighted`

### Error Handling

See [`variant-groups.md`](variant-groups.md#error-handling) for the canonical error handling patterns shared across all variant route handlers (ProgrammingError→501, SQLAlchemyError→503, IntegrityError→409, Exception→500, 404, 429, row locking).

## QA History

- 2026-07-06: qa-iterate — Fixed MAJOR: removed duplicated Weighted selection, Coverage gap detection, and Prompt diff comparison behaviours (canonical versions in variant-groups.md). Status: partial.

## Known Gaps

- PRD 8.19 specifies batch firing N variants (all-or-nothing pre-flight) but current code fires one per call — no batch endpoint exists
- PRD 8.19 specifies partial completion with HITL but no HITL-aware execution handling exists
- PRD 8.19 specifies cancel/abandon variant endpoint — not implemented
- No frontend exists for variant group creation or coverage signal UI (comparison view exists as VariantCompareView.vue)
- No all-or-nothing N-variant quota pre-flight
- No website docs page for variant execution (variant-groups.md exists but no execution-specific page)
- Integration tests skipped (all `@pytest.mark.skip`)
