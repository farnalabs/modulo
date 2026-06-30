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

### Weighted selection

- [x] `pick_variant_weighted` selects a variant proportionally to each variant's `weight` key
- [x] Single-variant group short-circuits — returns the only variant directly (no random call)
- [x] Empty variants list returns None
- [x] All-zero weights fall back to `random.choice` (uniform)
- [x] Missing `weight` key defaults to 1.0

### Run creation

- [x] `POST /api/v1/variant-groups/{group_id}/run` selects a variant, merges `run_context_overrides` into `input_payload`, and creates a run with the variant's `snapshot_id`
- [x] `snapshot_id` accepted as both `str` and `uuid.UUID`
- [x] `degraded_evals=true` injects `_degraded_evals: True` into the merged payload
- [x] `trigger_type` defaults to `"manual"` for variant-triggered runs
- [x] Run count incremented on the variant group after each successful run
- [x] Response includes `run_id`, `variant_name`, and `merged_payload`
- [x] RLS org context set on the endpoint via `set_rls_org`

### Quota enforcement

- [x] `check_pipeline_run_quota` returns True when `active < max_concurrent_runs`
- [x] 429 returned when pipeline concurrent run quota exceeded
- [x] 429 returned when no variant selected or quota exceeded in `run_variant_weighted`

### Coverage gap detection

- [x] `GET /api/v1/variant-groups/{group_id}/coverage-gaps` returns variants whose `eval_definition_ids` don't cover all eval definitions for the pipeline
- [x] Eval definitions loaded from `EvalDefinition` table filtered by pipeline_id
- [x] Empty eval definitions list for pipeline yields no gaps
- [x] All evals present in variant yields no gap
- [x] 404 if variant group not found
- [x] RLS enforced

### Prompt diff comparison

- [x] `GET /api/v1/variant-groups/{group_id}/prompt-diffs` compares `prompt_pins_json` across variant snapshots
- [x] Returns agent-level diffs `{agent_id, base_hash, variant_hash}` when hashes differ
- [x] Handles `base_snapshot_ids` to explicitly mark base vs comparison variants
- [x] Missing snapshots are skipped (not a hard error)
- [x] No snapshots or no variants returns empty list
- [x] 404 if variant group not found
- [x] RLS enforced

## Known Gaps

- PRD 8.19 specifies batch firing N variants (all-or-nothing pre-flight) but current code fires one per call — no batch endpoint exists
- PRD 8.19 specifies partial completion with HITL but no HITL-aware execution handling exists
- PRD 8.19 specifies cancel/abandon variant endpoint — not implemented
- No frontend exists for variant group creation, comparison view, or coverage signal
- No all-or-nothing N-variant quota pre-flight
