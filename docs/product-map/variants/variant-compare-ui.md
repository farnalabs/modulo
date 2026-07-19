---
id: feat-variants-variant-compare-ui
prd: 8.19
delivery-tasks: [task-nv3-variant-compare-ui]
bdd:
  - backend/tests/bdd/features/pipelines/run_variants.feature
code:
  - frontend/src/views/VariantCompareView.vue
  - frontend/src/router/index.ts
  - backend/src/modulo/api/routes/variants.py
  - backend/src/modulo/db/crud/variant_group.py
  - backend/src/modulo/db/models/variant_group.py
  - Website/modulo-website/src/docs/variants/variant-compare-ui.md
unit-tests:
  - backend/tests/unit/api/test_variants.py
  - backend/tests/unit/db/crud/test_variant_group.py
  - backend/tests/integration/crud/test_variant_group.py
  - frontend/src/__tests__/VariantCompareView.spec.ts
depends-on: [feat-evals-eval-engine, feat-variants-variant-execution, feat-variants-variant-groups]
status: partial
---

# Variant Compare UI

Side-by-side eval scores, token costs, and output diffs across A/B test variants.

## Behaviours

### Variant group selection

- [x] Groups list loads from GET /api/v1/variant-groups and populates dropdown
- [x] Selecting a group fetches group detail via GET /api/v1/variant-groups/{group_id}
- [x] Auto-selects first group when list loads if none selected
- [x] Empty state shown when no groups exist: "Create a variant group first…"
- [x] Loading spinner during initial fetch

### Comparison table

- [x] Table renders one row per node, one column per variant
- [x] Cell shows pass/fail/partial badge based on eval results for that node+variant
- [x] Green "pass" badge when all evals for that cell pass
- [x] Red "fail" badge when all evals for that cell fail
- [x] Amber "partial" badge when mixed pass/fail results
- [x] Em dash shown when no eval results exist for that cell
- [x] Eval score values displayed as tabular-nums badges below the status badge
- [x] Footer summary row shows per-variant pass rate, total cost, token total, HITL counts

### Pass rate colour coding

- [x] ≥80% green border/bg
- [x] ≥40% amber border/bg
- [x] <40% red border/bg
- [x] No data shows em dash

### Run variants

- [x] Button triggers POST /api/v1/variant-groups/{group_id}/run
- [x] Button disabled when a run is already in progress
- [x] Spinner on button and "Running…" label during execution
- [x] Weighted random variant selection on backend
- [x] run_context_overrides merged into run payload
- [x] Polls run status every 2s until terminal (complete/failed/cancelled/eval_failed)
- [x] On complete: fetches run IO and eval results in parallel
- [x] On failure: error banner with status reason

### Output diff viewer

- [x] Shown when ≥2 variants have run data
- [x] Node selector dropdown
- [x] Variant A / Variant B selectors
- [x] Side-by-side pre blocks with JSON-formatted output
- [x] Auto-selects first node and first two variants when data loads

### Error and edge states

- [x] API error shows error banner with descriptive message
- [x] Run with non-terminal status shows error banner
- [x] 404 variant group handled (error banner)
- [x] 429 quota-exceeded handled (error banner)
- [x] Network error caught gracefully (try/catch wrapped)
- [x] Empty run data state: "No run data yet. Click Run Variants…"
- [x] Waiting-for-completion state: "Waiting for runs to complete…"
- [x] Empty groups state: "No variant groups found. Create a variant group first…"

### Coverage gaps (API)

- [x] GET /{group_id}/coverage-gaps detects variants missing eval definitions
- [x] Returns list of variant + missing_evals tuples
- [x] No gaps reported when all evals present for all variants
- [ ] Frontend does not call coverage-gaps endpoint — API-only, no UI

### Prompt diffs (API)

- [x] GET /{group_id}/prompt-diffs compares prompt_pins_json across snapshots
- [x] Returns agent-level diff entries with base_hash and variant_hash
- [ ] Frontend does not display prompt diffs — API-only, no UI

### Pre-eval degraded mode

- [x] No evals configured shows token cost and output diffs only (implicit — eval=[] shows -- dash)
- [ ] UI banner prompts eval configuration when no evals exist — not implemented

### Eval coverage signal

- [ ] Backend endpoint exists but frontend does not query or display coverage warning
- [ ] Warning text: "Variants diverged but evals did not differentiate…"

### HITL partial completion

- [ ] HITL counts shown in footer (hardcoded to 0 — backend does not populate)
- [ ] Pending HITL indicator for blocked variant
- [ ] Operators can cancel a variant run — excluded from aggregate scores
- [ ] Group complete when all variants reach terminal state

### Variant group run limits

- [x] Pre-flight check rejects group if limit breached (backend enforcement)
- [x] Error code returned as pipeline_concurrent_quota_exceeded

### Prompt version comparison

- [ ] Backend prompt-diff endpoint exists but has no frontend surface

### Error Handling

- [x] 501 ProgrammingError catch on create, list, get, update, delete, run, coverage-gaps, prompt-diffs endpoints
- [x] 404 on GET/PUT/DELETE/run/coverage-gaps/prompt-diffs for unknown group_id
- [x] 204 No Content on successful DELETE
- [x] 429 on run_variant when pipeline concurrent run quota exceeded
- [x] 429 when no variant selected or quota exceeded in run_variant_weighted
- [x] API errors surfaced as inline ErrorAlert in the UI
- [x] Network errors caught with try/catch and friendly error messages
- [x] Frontend error messages use JSON.stringify which can produce [object Object] for some error shapes — fixed: replaced with formatApiError(err)

## Known Gaps

- **No comparison endpoint**: The frontend polls per-run status/IO/evals directly instead of fetching a single comparison response. No `GET /{group_id}/compare` backend endpoint exists.
- **HITL counts hardcoded to zero**: The summary footer shows approved/rejected/pending placeholders (all 0). The backend run data does not populate HITL results per variant.
- **No eval coverage signal UI**: Backend `coverage-gaps` endpoint exists but frontend does not call it. The "Variants diverged but evals did not differentiate" warning has no frontend surface.
- **No pre-eval degraded mode banner**: Frontend does not display an eval-configuration banner when no evals exist, even though `degraded_evals` flag is available.
- **No prompt version comparison UI**: Backend `prompt-diffs` endpoint exists but has no frontend display.
- **No cancel/abandon variant UI**: PRD 8.19 specifies operators can cancel a variant run; no cancel button exists.
- **No "Run as variant" entry point from pipeline detail page**: PRD 8.19 specifies creating variant groups from the pipeline detail page; the current UI requires navigating to /variants/compare directly.
- **BDD coverage is thin**: `run_variants.feature` has 5 scenarios (1 @awaiting-implementation); `variant_groups.feature` has 7 scenarios (6 @awaiting-implementation for batch, sequential, comparison, coverage signal, cost breakdown, quota batch). Only the zero-weight and basic CRUD/run/404/429 scenarios are wired.
- **No frontend Playwright E2E tests**: No end-to-end test for the comparison view interaction.

## QA History

- 2026-07-05 (improve-architecture index 137): Cross-cutting QA pass 1. Marked 32 behaviour checkboxes [ ]→[x] after verifying against code (all comparison table, run variants, output diff, error state, pass rate, and variant group selection behaviours were implemented but not checked). Fixed i18n violations: replaced ~30 hardcoded strings in VariantCompareView.vue with `$t()`/`t()` wrappers. Added 32 i18n keys to `en-US.js` under `views.variantCompare`. Added Error Handling section (7 behaviour checkboxes). Refined Known Gaps: removed stale "BDD placeholder" gap (replaced with accurate gap count), added HITL hardcoded-to-zero gap, added no-comparison-endpoint gap, added i18n-fixed note. Updated frontmatter: added frontend smoke test to unit-tests. 1/1 frontend smoke test passes. Status: partial.
- 2026-07-06 (Cross-cutting QA): Fixed `JSON.stringify(err)` → `formatApiError(err)` in 3 error handlers. Removed unused `VariantCompareView` locale keys from en-US.js. Created website docs stub at `evals/variant-compare-ui.md`. Added docs path to product map `code` field. Status: partial.
- 2026-07-06: qa-iterate — Fixed MAJOR: removed `docs:` path from `unit-tests:` frontmatter field (moved to `code:`). Added `feat-variants-variant-groups` to `depends-on`. Status: partial.
- 2026-07-07: Cross-cutting QA (index 327): Fixed MAJOR — replaced `e instanceof Error ? e.message : String(e)` with `formatApiError(e)` in 3 catch blocks for richer error detail. Fixed MAJOR — removed duplicate `@pytest.mark.asyncio` decorator in `test_variant_group.py`. Confirmed 70/70 variant tests pass (4 skipped = pre-existing batch/sequential/compare/coverage-signal gaps). Fixed minor — moved i18n-fixed note from Known Gaps to QA History. Status: partial.
