---
id: feat-variants-variant-groups
prd: 8.19
delivery-tasks: [task-nv3-variant-group]
bdd:
  - backend/tests/bdd/features/pipelines/run_variants.feature
  - backend/tests/bdd/features/variants/variant_groups.feature
code:
  - backend/src/modulo/api/routes/variants.py
  - backend/src/modulo/db/models/variant_group.py
  - backend/src/modulo/db/crud/variant_group.py
  - backend/src/modulo/db/migrations/versions/0012_variant_groups.py
unit-tests:
  - backend/tests/unit/api/test_variants.py
  - backend/tests/unit/db/crud/test_variant_group.py
  - backend/tests/unit/api/test_variant_groups_bdd.py
  - backend/tests/integration/crud/test_variant_group.py
depends-on: [feat-core-run-context, feat-evals-eval-engine]
status: partial
---

# Variant Groups

A/B test variant management — named sets of runs against the same pipeline that differ only in `run_context_overrides`.

## Behaviours

### CRUD operations

- [x] Create a variant group with name, description, pipeline_id, and 2+ variant definitions (each with snapshot_id, name, weight, run_context_overrides, eval_definition_ids)
- [x] Create a variant group with optional `selection_strategy` (default "weighted"), `max_concurrent_runs` (default 5), `degraded_evals` (default false)
- [x] List variant groups with pagination (page, page_size, max 100), ordered by created_at desc
- [x] List variant groups filtered by pipeline_id
- [x] Get a variant group by ID
- [x] Update a variant group — change name, description, variants, selection_strategy, max_concurrent_runs, degraded_evals
- [x] Delete a variant group by ID
- [x] RLS org context set on every CRUD endpoint via `set_rls_org`

### Variant selection

- [x] Weighted random selection picks a variant proportionally to each variant's weight
- [x] Single-variant group short-circuits — returns the only variant directly (no random call)
- [x] Empty variants list returns None
- [x] All-zero weights fall back to uniform random selection
- [x] Missing weight key defaults to 1.0
- [x] Selection strategy constrained to `'weighted'` or `'single'` (DB CHECK constraint)

### Running variants

- [x] `POST /{group_id}/run` selects a variant via weighted random, merges `run_context_overrides` into `input_payload`, creates a run with the variant's `snapshot_id`, and returns `{run_id, variant_name, merged_payload}`
- [x] `snapshot_id` accepted as both `str` and `uuid.UUID` in variant definitions
- [x] `degraded_evals=true` injects `_degraded_evals: True` into merged payload
- [x] Run count incremented on the variant group after each successful run
- [x] `trigger_type` defaults to `"manual"` for variant-triggered runs
- [x] 404 if variant group not found when running
- [x] 429 if pipeline concurrent run quota exceeded (`check_pipeline_run_quota`)
- [x] 429 if no variant selected or quota exceeded in `run_variant_weighted`

### Coverage gap detection

- [x] `GET /{group_id}/coverage-gaps` returns variants whose `eval_definition_ids` don't cover all eval definitions for the pipeline
- [x] Eval definitions loaded from `EvalDefinition` table filtered by pipeline_id
- [x] Empty eval definitions list for pipeline → no gaps reported
- [x] All evals present in variant's `eval_definition_ids` → no gap reported
- [x] 404 if variant group not found
- [x] RLS enforced

### Prompt diff comparison

- [x] `GET /{group_id}/prompt-diffs` compares `prompt_pins_json` across variant snapshots
- [x] Returns agent-level diffs: `{agent_id, base_hash, variant_hash}` when hashes differ
- [x] Handles `base_snapshot_ids` parameter for explicitly marking base vs comparison variants
- [x] Missing snapshots are skipped (not a hard error)
- [x] No snapshots or no variants → returns empty list
- [x] 404 if variant group not found
- [x] RLS enforced

### Error handling

- [x] 404 on GET/PUT/DELETE for unknown group_id
- [x] 204 No Content on successful DELETE
- [x] 404 on run_variant when group not found
- [x] 429 on run_variant when quota exceeded
- [x] ForeignKey `RESTRICT` on pipeline deletion (pipeline with variant groups cannot be deleted)
- [x] Check constraint enforces `selection_strategy IN ('weighted', 'single')`

### Pipeline limits

- [x] Concurrent run limit enforced per pipeline per variant group via `check_pipeline_run_quota` (`active < max_concurrent_runs`)

### Error handling (programming error)

- [x] POST create_variant_group → 501 ProgrammingError
- [x] GET list_variant_groups → 501 ProgrammingError
- [x] GET get_variant_group → 501 ProgrammingError
- [x] PUT update_variant_group → 501 ProgrammingError
- [x] DELETE delete_variant_group → 501 ProgrammingError
- [x] POST run_variant → 501 ProgrammingError
- [x] GET coverage_gaps → 501 ProgrammingError
- [x] GET prompt_diffs → 501 ProgrammingError

## Missing implementations (gaps relative to PRD 8.19)

- [ ] Batch run: PRD specifies "fires one run per variant" — current code fires only one run per API call, not N variants
- [ ] Comparison view: no frontend, no endpoint for side-by-side eval scores / token cost / HITL outcomes / per-node output diff
- [ ] Eval coverage signal warning: `coverage-gaps` endpoint exists but no UI surfaces the "Variants diverged but evals did not differentiate" warning
- [ ] HITL partial completion: no handling for one variant reaching `awaiting_human` while others complete
- [ ] Cancel/abandon variant: no endpoint or status for marking a variant run as abandoned and excluding from aggregates
- [ ] All-or-nothing pre-flight quota: PRD says check all N variants before firing any — current code checks per-run only
- [ ] Prompt versioning library guide: `get_prompt_diffs` exists but documented library pattern for prompt versioning does not

## Test coverage gaps

- [ ] No unit tests for `run_variant_weighted`
- [ ] No unit tests for `get_prompt_diffs`

## Known Gaps

- No frontend exists for variant group creation, comparison view, or coverage signal
- No all-or-nothing N-variant quota pre-flight
- No HITL partial completion handling
