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

### Comparison & Eval Behaviours

- [x] Comparison view UI: eval scores per node per variant side by side (VariantCompareView table, spec asserts badges)
- [x] Comparison view: token cost per run (summary footer totalCost + tokenTotal; spec asserts)
- [ ] Comparison view: HITL outcomes if gates reached (footer hardcodes approved/rejected/pending to 0)
- [x] Comparison view: per-node output diff (artifact comparison) (diff viewer, spec asserts side-by-side JSON)
- [ ] Eval coverage signal: "Variants diverged but evals did not differentiate" warning when scores identical but outputs differ
- [ ] Variant group with HITL: partial completion for completed variants, pending indicator for blocked variant
- [ ] Operators can cancel a variant run to mark it `abandoned`
- [ ] Group is complete when all variants reach terminal state
- [x] Pre-eval degraded mode: show token cost + output diffs only when no evals configured (implicit — eval=[] renders em dash, cost/diff still shown)
- [ ] Pre-eval degraded mode: banner prompting eval configuration when no evals exist
- [x] Prompt version comparison via run_context_overrides with `prompt_version` key (generic override merge + prompt-diffs endpoint; unit test added)
- [ ] Agents declare multiple prompt template versions and select by context key

## Error Handling

- [x] `except ProgrammingError → 501 Not Implemented` on all 8 route handlers (create, list, get, update, delete, run, coverage-gaps, prompt-diffs)
- [x] `except SQLAlchemyError → 503 Service Unavailable` on all 8 route handlers
- [x] `except IntegrityError → 409 Conflict` on create, update, delete, and run endpoints
- [x] `except Exception → 500 Internal Server Error` on all 8 route handlers (guards against Python-level errors)
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
- [x] Empty `prompt_pins_json` — no agent diffs returned (unit test added)

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

- No eval coverage signal Warning — the `get_coverage_gaps` function exists but the UX warning ("Variants diverged but evals did not differentiate") is not wired into the comparison UI
- No HITL partial completion flow — variant runs blocked on HITL have no special handling; the compare footer hardcodes approved/rejected/pending to 0, and abandon/cancel + group-complete tracking are not implemented
- No pre-eval degraded mode banner in UI — backend `degraded_evals` flag exists and cost+output-diff-only rendering works, but there is no banner prompting eval configuration
- No agent-side prompt template versioning — variants can carry `prompt_version` in `run_context_overrides` (merged into the payload) and the `prompt-diffs` API compares prompt hashes, but agents do not yet declare/select multiple prompt template versions by context key
- No retry/backoff or circuit breaker on run-creation / variant-selection failures — run creation failures surface as HTTP errors; there is no retry policy or circuit breaker

## QA History

- 2026-07-06: qa-iterate — Fixed CRITICAL: corrected "all 7 route handlers" → "all 8 route handlers" (the code has 8 route handlers, not 7). Fixed MAJOR: removed duplicated Weighted Selection, Eval Coverage Gaps, and Prompt Version Comparison sections (canonical versions in variant-groups.md). Status: partial.
- 2026-07-08: improve-architecture (index 261) — Fixed CRITICAL: added `except Exception → 500` catches to all 8 route handlers (previously missing generic exception guard — Python-level errors like KeyError, TypeError, ValueError propagated as raw 500). Fixed MAJOR: `run_variant_weighted` changed `variant["snapshot_id"]` to `variant.get("snapshot_id")` with None guard to prevent KeyError crash on missing snapshot_id. Fixed MAJOR: `get_prompt_diffs` dict comprehensions changed from bare `p["agent_id"]`/`p["prompt_version_hash"]` to `p.get(...)` with guard to prevent KeyError on malformed `prompt_pins_json`. Added Error Handling section (10 checkboxes), Edge Cases section (9 checkboxes), Resilience & Integration Robustness section (6 checkboxes) to product map. Added 9 new unit tests covering `except Exception → 500` for all 8 route handlers (9 test classes) and 1 CRUD-level test for missing snapshot_id. All 20 variant tests pass (10 new + 10 existing). Merged to main at v0.3.241. Status: partial.
- 2026-08-15: Coverage-completion pass (FAR-235). Verified every unchecked behaviour against code + tests. Checked off: comparison view UI (per-node eval badges), token cost per run (summary footer), per-node output diff, pre-eval cost+diff degraded rendering (implicit, eval=[] shows em dash), prompt version comparison via `run_context_overrides` (generic override merge + `prompt-diffs` endpoint), empty `prompt_pins_json` edge case. Added unit tests: empty `prompt_pins_json` → no agent diffs; batch run merging a `prompt_version` override (PRD 8.19). Added frontend spec coverage: per-node pass/fail/partial badges + eval scores side by side; summary footer token/cost totals. Genuine gaps left unchecked (Known Gaps): HITL outcomes/cancel/partial-completion, coverage-gap warning, pre-eval banner, agent-side prompt-template versioning, retry/circuit-breaker. Status: partial.
