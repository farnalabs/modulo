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

## Error Handling

- [x] `except ProgrammingError → 501 Not Implemented` on all 7 route handlers (create, list, get, update, delete, run, coverage-gaps, prompt-diffs)
- [x] `except SQLAlchemyError → 503 Service Unavailable` on all 7 route handlers
- [x] `except IntegrityError → 409 Conflict` on create, update, delete, and run endpoints
- [x] `except Exception → 500 Internal Server Error` on all 7 route handlers (guards against Python-level errors)
- [x] Group not found → 404 on get, update, delete, run, coverage-gaps, prompt-diffs
- [x] Pipeline quota exceeded → 429 on run
- [x] Empty variants list → 429 on run
- [x] Missing `snapshot_id` in variant → variant run not created (graceful None return)
- [x] Invalid `snapshot_id` UUID → ValueError caught by `except Exception` → 500

## Edge Cases

- [x] Empty variants list — group exists but has no variants, run attempt returns 429
- [x] Single variant — pick_variant_weighted short-circuits (no random)
- [x] All-zero weights — falls back to uniform random selection
- [x] Missing weight key — defaults to 1.0
- [x] Variant group deleted mid-flight — row lock returns None, run attempt returns 429
- [x] Concurrent run quota — checked both at route level and inside locked transaction
- [x] `run_context_overrides` may be non-dict — `isinstance(overrides, dict)` guard
- [x] Missing `snapshot_id` in variant dict — `.get()` returns None, run aborted gracefully
- [ ] Empty `prompt_pins_json` — no agent diffs returned

## Resilience & Integration Robustness

- [x] `SELECT ... FOR UPDATE` row lock prevents concurrent quota races
- [x] Quota double-checked (route handler + locked transaction) prevents TOCTOU
- [x] Variant selection uses `random.random()` (not cryptographic — acceptable for A/B test weighting)
- [x] `run_context_overrides` merged with input_payload in defensive copy
- [x] `get_prompt_diffs` skips missing snapshots gracefully (no crash on stale references)
- [x] `get_prompt_diffs` dict comprehensions guard against malformed `prompt_pins_json`
- [ ] No retry/backoff on run creation failure
- [ ] No circuit breaker on repeated variant selection failures

## Known Gaps

- No comparison view UI — backend for variant CRUD and weighted running exists, but the comparison surface (side-by-side eval scores, token cost, output diff, HITL outcomes) is not yet built
- No eval coverage signal Warning — the `get_coverage_gaps` function exists but the UX warning ("Variants diverged but evals did not differentiate") is not wired
- No HITL partial completion flow — variant runs blocked on HITL have no special handling; abandon/cancel not implemented
- No pre-eval degraded mode banner in UI — backend `degraded_evals` flag exists but no UI prompt

## QA History

- 2026-07-08: improve-architecture (index 261) — Fixed CRITICAL: added `except Exception → 500` catches to all 7 route handlers (previously missing generic exception guard — Python-level errors like KeyError, TypeError, ValueError propagated as raw 500). Fixed MAJOR: `run_variant_weighted` changed `variant["snapshot_id"]` to `variant.get("snapshot_id")` with None guard to prevent KeyError crash on missing snapshot_id. Fixed MAJOR: `get_prompt_diffs` dict comprehensions changed from bare `p["agent_id"]`/`p["prompt_version_hash"]` to `p.get(...)` with guard to prevent KeyError on malformed `prompt_pins_json`. Added Error Handling section (10 checkboxes), Edge Cases section (9 checkboxes), Resilience & Integration Robustness section (6 checkboxes) to product map. Added 9 new unit tests covering `except Exception → 500` for all 7 route handlers (9 test classes) and 1 CRUD-level test for missing snapshot_id. All 20 variant tests pass (10 new + 10 existing). Merged to main at v0.3.242. Status: partial.
