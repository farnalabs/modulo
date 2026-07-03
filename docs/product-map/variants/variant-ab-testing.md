---
id: feat-variants-variant-ab-testing
prd: 8.19
delivery-tasks: [task-nv3-ab-test-models]
bdd:
  - backend/tests/bdd/features/pipelines/run_variants.feature
  - backend/tests/bdd/features/variants/variant_groups.feature
code:
  - backend/src/modulo/api/routes/variants.py
  - backend/src/modulo/db/crud/variant_group.py
  - backend/src/modulo/db/models/variant_group.py
unit-tests:
  - backend/tests/unit/api/test_variants.py
  - backend/tests/unit/db/crud/test_variant_group.py
  - backend/tests/integration/crud/test_variant_group.py
depends-on: [feat-evals-eval-engine, feat-variants-variant-execution]
status: partial
---

# Variant A/B Testing

## Behaviours

### Variant Group CRUD

- [x] Create variant group with name, description, variants, selection_strategy, max_concurrent_runs, degraded_evals
- [x] Create variant group with empty variants list
- [x] Get variant group by ID returns full group with all fields
- [x] Get variant group returns 404 for unknown group ID
- [x] List variant groups with pagination (page, page_size, sorted by created_at desc)
- [x] List variant groups filtered by pipeline_id
- [x] Update variant group — any subset of name, description, variants, selection_strategy, max_concurrent_runs, degraded_evals
- [x] Update variant group returns 404 for unknown group ID
- [x] Delete variant group
- [x] Delete variant group returns 404 for unknown group ID
- [x] Variant group scoped to organisation (RLS enforced)

### Variant Definition

- [x] VariantDef requires snapshot_id, name; optional weight (default 1.0), run_context_overrides (default {}), eval_definition_ids (default [])
- [x] Weight ge 0 (zero-weight variants accepted)
- [x] Variants stored as JSON column on VariantGroup

### Weighted Selection

- [x] Empty variants returns None
- [x] Single variant returned directly (short-circuit, no random)
- [x] Weighted random selection respects proportional weights
- [x] All-zero weights falls back to uniform random choice
- [x] Missing weight key defaults to 1.0
- [x] Weighted selection can reach any variant given sufficient trials (distribution test)
- [x] Weighted selection dominates over low-weight variants (100:1 ratio heavily favours higher weight)

### Running Variants

- [x] Run endpoint selects weighted variant, merges run_context_overrides into input_payload, creates a run
- [x] Run returns run_id, variant_name, merged_payload
- [x] Run raises 429 when pipeline concurrent run quota exceeded
- [x] Run raises 429 when no variant selected (empty variant list)
- [x] Run raises 404 for unknown group ID
- [x] Degraded evals mode adds `_degraded_evals: True` to merged payload
- [x] Each run increments variant group run_count

### Quota & Limits

- [x] Pre-flight check before firing: group rejected if active runs >= max_concurrent_runs (no partial firing)
- [x] `check_pipeline_run_quota` returns True when within limit
- [x] `check_pipeline_run_quota` returns False when at or over limit
- [x] N variants creates N runs, each counted individually against pipeline limits

### Eval Coverage Gaps

- [x] Detect which variants lack eval definitions (missing eval_definition_ids)
- [x] Return list of gaps with variant reference and missing eval IDs
- [x] Return empty list when all pipeline evals are referenced by all variants
- [x] Coverage gap endpoint accessible via API

### Prompt Version Comparison

- [x] Compare prompt_pins_json across variant snapshots
- [x] Identify agent-level prompt version hash differences between base and comparison variants
- [x] Skip pairs where a snapshot is missing (graceful)
- [x] Prompt diff endpoint accessible via API

### PRD Behaviours (not yet implemented)

- [ ] Comparison view UI: eval scores per node per variant side by side
- [ ] Comparison view: token cost per run
- [ ] Comparison view: HITL outcomes if gates reached
- [ ] Comparison view: per-node output diff (artifact comparison)
- [ ] Eval coverage signal: "Variants diverged but evals did not differentiate" warning when scores identical but outputs differ
- [ ] Variant group with HITL: partial completion for completed variants, pending indicator for blocked variant
- [ ] Operators can cancel a variant run to mark it `abandoned`
- [ ] Group is complete when all variants reach terminal state
- [ ] Pre-eval degraded mode: show token cost + output diffs only, with banner prompting eval configuration
- [ ] Prompt version comparison via run_context_overrides with `prompt_version` key
- [ ] Agents declare multiple prompt template versions and select by context key

## Known Gaps

- No comparison view UI — backend for variant CRUD and weighted running exists, but the comparison surface (side-by-side eval scores, token cost, output diff, HITL outcomes) is not yet built
- No eval coverage signal Warning — the `get_coverage_gaps` function exists but the UX warning ("Variants diverged but evals did not differentiate") is not wired
- No HITL partial completion flow — variant runs blocked on HITL have no special handling; abandon/cancel not implemented
- No pre-eval degraded mode banner in UI — backend `degraded_evals` flag exists but no UI prompt
