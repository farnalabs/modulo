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
unit-tests:
  - backend/tests/unit/api/test_variants.py
  - backend/tests/unit/db/crud/test_variant_group.py
  - backend/tests/integration/crud/test_variant_group.py
depends-on: [feat-evals-eval-engine, feat-variants-variant-execution]
status: partial
---

# Variant Compare UI

Side-by-side eval scores, token costs, and output diffs across A/B test variants.

## Behaviours

### Variant group selection

- [ ] Groups list loads from GET /api/v1/variant-groups and populates dropdown
- [ ] Selecting a group fetches group detail via GET /api/v1/variant-groups/{group_id}
- [ ] Auto-selects first group when list loads if none selected
- [ ] Empty state shown when no groups exist: "Create a variant group first…"
- [ ] Loading spinner during initial fetch

### Comparison table

- [ ] Table renders one row per node, one column per variant
- [ ] Cell shows pass/fail/partial badge based on eval results for that node+variant
- [ ] Green "pass" badge when all evals for that cell pass
- [ ] Red "fail" badge when all evals for that cell fail
- [ ] Amber "partial" badge when mixed pass/fail results
- [ ] Em dash shown when no eval results exist for that cell
- [ ] Eval score values displayed as tabular-nums badges below the status badge
- [ ] Footer summary row shows per-variant pass rate, total cost, token total, HITL counts

### Pass rate colour coding

- [ ] ≥80% green border/bg
- [ ] ≥40% amber border/bg
- [ ] <40% red border/bg
- [ ] No data shows em dash

### Run variants

- [ ] Button triggers POST /api/v1/variant-groups/{group_id}/run
- [ ] Button disabled when a run is already in progress
- [ ] Spinner on button and "Running…" label during execution
- [ ] Weighted random variant selection on backend
- [ ] run_context_overrides merged into run payload
- [ ] Polls run status every 2s until terminal (complete/failed/cancelled/eval_failed)
- [ ] On complete: fetches run IO and eval results in parallel
- [ ] On failure: error banner with status reason

### Output diff viewer

- [ ] Shown when ≥2 variants have run data
- [ ] Node selector dropdown
- [ ] Variant A / Variant B selectors
- [ ] Side-by-side pre blocks with JSON-formatted output
- [ ] Auto-selects first node and first two variants when data loads

### Error and edge states

- [ ] API error shows error banner with descriptive message
- [ ] Run with non-terminal status shows error banner
- [ ] 404 variant group handled
- [ ] 429 quota-exceeded handled
- [ ] Network error caught gracefully
- [ ] Empty run data state: "No run data yet. Click Run Variants…"
- [ ] Waiting-for-completion state: "Waiting for runs to complete…"

### Coverage gaps (API)

- [ ] GET /{group_id}/coverage-gaps detects variants missing eval definitions
- [ ] Returns list of variant + missing_evals tuples
- [ ] No gaps reported when all evals present for all variants

### Prompt diffs (API)

- [ ] GET /{group_id}/prompt-diffs compares prompt_pins_json across snapshots
- [ ] Returns agent-level diff entries with base_hash and variant_hash

### Pre-eval degraded mode

- [ ] No evals configured shows token cost and output diffs only
- [ ] UI banner prompts eval configuration when no evals exist

### Eval coverage signal

- [ ] Variants diverge but identical eval scores surfaces coverage gap warning
- [ ] Warning text: "Variants diverged but evals did not differentiate…"

### HITL partial completion

- [ ] One variant awaiting_human shows partial results for completed variants
- [ ] Pending HITL indicator for blocked variant
- [ ] Operators can cancel a variant run — excluded from aggregate scores
- [ ] Group complete when all variants reach terminal state

### Variant group run limits

- [ ] Firing N variants creates N runs, each counted against limits
- [ ] Pre-flight check rejects entire group if any limit breached
- [ ] Error code: variant_group_quota_exceeded

### Prompt version comparison

- [ ] Variants can differ by run_context_overrides.prompt_version
- [ ] Agents select prompt template based on run_context.prompt_version

## Known Gaps

- **BDD placeholder**: `run_variants.feature` contains only a dummy placeholder scenario. No BDD coverage exists for any variant comparison behaviour — no group creation, run execution, comparison view display, coverage gap detection, or HITL partial completion flows.
- **Coverage gap warning not wired to frontend**: The backend `GET /{group_id}/coverage-gaps` endpoint exists but the comparison UI does not display or query it. The eval coverage signal (PRD 8.19) is server-only.
- **Pre-eval degraded mode banner not implemented**: Frontend does not check `degraded_evals` or eval count to show the eval-configuration banner.
- **HITL partial completion UI not wired**: The comparison table footer has HITL count placeholders (approved/rejected/pending) but they are hardcoded to zero — the backend run data does not populate them.
- **Prompt version comparison UI not implemented**: The backend prompt-diff endpoint exists but has no frontend surface.
- **No pagination controls on comparison view**: The group-list API supports pagination but the frontend loads all groups at once with no pagination UI.
- **No cancellation UI for variant runs**: PRD specifies operators can cancel a variant run from the comparison view; no cancel button exists in the current implementation.
- **No "Run as variant" entry point from pipeline detail page**: PRD 8.19 specifies creating variant groups from the pipeline detail page; the current UI requires navigating to /variants/compare directly.