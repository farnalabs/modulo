---
id: feat-variants
prd: N/A
adr: []
code:
  - backend/src/modulo/api/routes/variants.py
unit-tests: []
bdd:
  - backend/tests/bdd/features/variants/variant_groups.feature
  - backend/tests/bdd/steps/test_variant_groups.py
depends-on:
  - feat-runs
  - feat-evals
status: covered
---

# Variants

Variant groups — batch comparison on `/variants/compare`. A variant group bundles
weighted variants (optional `run_context_overrides`) and fires one run per
variant; the page hosts an inline variant-group builder (create + batch-fire),
and comparison surfaces eval scores per node, prompt diffs and eval
coverage gaps. A batch-compare flow (`/variants/compare/:batchId`) is activated
by the `variant_batch_compare` feature flag.

## Behaviours

- [x] A variant group is creatable with weighted variants that fuse
      `run_context_overrides` into each run's input payload
      (`tests/bdd/features/variants/variant_groups.feature`)
- [x] A batch run triggers one run per variant in insertion order with variant names
      carried through the run record
      (`tests/bdd/features/variants/variant_groups.feature`)
- [x] Sequential execution fires one run per variant in variant insertion order,
      driving the real `run_variant_batch` seam (insertion order preserved,
      one shared `batch_id`)
      (`tests/bdd/features/variants/variant_groups.feature`)
- [x] Variant groups can be listed, fetched, updated, deleted and restored
      (ownership asserted per organisation, `api/routes/variants.py`)
- [x] Comparison surfaces include coverage gaps, prompt diffs and the batch-compare
      summary (`prompt_diffs`, `coverage_gaps`, `batch_compare` endpoints)
- [x] Eval coverage gaps report the pipeline eval definitions each variant is
      missing, driving the real `get_coverage_gaps` seam (a variant claiming the
      eval is not flagged)
      (`tests/bdd/features/variants/variant_groups.feature`)
- [x] Per-token breakdown comparison across variants ships: each variant's
      per-node token usage (input/output/total tokens + cost) rides the
      variant-batch detail surface (`_run_to_variant_run` in
      `api/routes/variant_batches.py`) and the `get_batch_compare` surface
      (`BatchRunCompare` / `batch_compare` in `api/routes/variants.py`) via the
      persisted `node_token_usage` union, serialized through the RunResponse
      bounds (`_serialize_node_token_usage` — `model_cost_raw_usd` display
      clamp + newest-N node truncation), and the `/variants/compare/:batchId`
      page renders a per-node token table per expanded variant run
      (`VariantBatchCompareView.vue`, `VariantBatchCompareView.spec.ts`,
      `test_variant_batches.py`, `test_variants.py`)
- [x] The variant batch-compare UI is gated by the `variant_batch_compare` feature flag
      and hard-replaces the legacy AB-test view when enabled (frontend router guard)
- [x] Variant groups are created and batch-fired from the inline builder on
      `/variants/compare` (`components/variants/VariantGroupBuilder.vue`), which
      honours the `pipeline_id` deep-link from a pipeline's "Run as variant" action

## Known Gaps

None acknowledged: the `@awaiting-implementation` draft scenarios in
`variant_groups.feature` (tracked since the 2026-08-27 walk) are resolved, and
the per-token breakdown comparison the manifest previously parked as the lone
`feat-variants` partial is shipped (2026-09-25 Improve Architecture walk).

## QA History

- 2026-09-25: **Improve Architecture product-map walk** — closed the last
  `feat-variants` partial: per-token breakdown comparison now ships end to end.
  `_run_to_variant_run` (`api/routes/variant_batches.py`) and `get_batch_compare`
  / `batch_compare` (`api/routes/variants.py` + `db/crud/variant_group.py`) carry
  each run's `node_token_usage` union through the RunResponse serialization
  bounds (`_serialize_node_token_usage`), and `VariantBatchCompareView.vue`
  renders a per-node input/output/total-token + cost table per expanded variant
  run (`variant-batch-token-breakdown`, registered in the manifest elements
  inventory). The manifest `feat-variants` status is now `covered`.
- 2026-09-25: **Improve Architecture product-map walk** — reconciled the
  manifest `feat-variants` registry entry with this tracker: per-node eval-score
  comparison (the `[x]` "eval scores per node" behaviour above) is now ticked, and
  the unchecked item is narrowed to the genuinely missing per-token breakdown only.
  The tracker's Known Gaps wording ("per-node eval-score / per-token breakdown drafts")
  is clarified by the Behaviours line that already ships per-node eval scores.
- 2026-09-23: **product-map review pass** — removed the stale
  `pipelines/run_variants.feature` "Coverage gaps are reported for a variant
  group" scenario (`@awaiting-implementation`, deselected) and its dead step
  definitions. It was a duplicate draft of the real `get_coverage_gaps` seam
  already locked by `variants/variant_groups.feature` (the `variant_groups.feature`
  eval-coverage scenario drives the same route/seam); the redundant copy and
  `PINNED_AWAITING_IMPLEMENTATION` entry for `pipelines/run_variants.feature`
  were archived. Coverage for this entry is unchanged.

- 2026-09-20: **product-map review pass** — closed the
  "BDD scenarios tagged `@awaiting-implementation`" gap
  (`variant_groups.feature`). The sequential-order scenario now drives the REAL
  `run_variant_batch` seam with the same mock-session machinery as the batch-run
  scenario (runs created sequentially in variant insertion order under one
  `batch_id`), and the eval-coverage draft was re-anchored to the REAL
  `get_coverage_gaps` seam (missing `eval_definition_ids` per variant — a
  variant that claims the eval is not flagged). The two per-node eval-score /
  per-token breakdown comparison drafts were removed: they asserted a wire shape
  the product does not ship (`get_batch_compare` returns per-run
  `eval_pass_rate` / `eval_count` / `total_tokens` / `total_cost_usd` and the
  frozen-snapshot override diff, which the batch-scope comparison scenarios
  already lock) — leaving them tagged would keep false coverage promises in the
  suite. No `@awaiting-implementation` scenarios remain in the feature.

- 2026-09-12: **product-map review pass** — registered the shared
  `JsonViewer` surface (`components/shared/JsonViewer.vue` static testids
  `json-viewer` / `json-viewer-{copy,expand-all,collapse-all,string-expand,string-collapse}`)
  in the manifest `elements:` inventory for `/variants/compare` and
  `/variants/compare/:batchId`: both pages render the compared variant outputs
  inline with `<JsonViewer>` (`VariantCompareView.vue` / `VariantBatchCompareView.vue`),
  so the viewer shipped in the DOM while staying invisible to Assistant's docs indexer /
  `/api/v1/manifest`. The component is now part of both routes' reverse
  testid-coverage guard (`test_mapped_route_elements_cover_owning_view_testids`).

- 2026-09-11: **product-map review pass** — extended the reverse
  testid-coverage guard (`test_mapped_route_elements_cover_owning_view_testids`) to
  `/variants/compare` and `/variants/compare/:batchId`: the whole-page view(s) `VariantCompareView.vue` and `VariantBatchCompareView.vue` render static `data-testid`s that the
  product map `elements:` inventory already documents, but the surface was not yet
  guarded against drift. The route now maps to its owning view so a newly shipped
  testid can no longer silently stay invisible to Assistant's docs indexer /
  `/api/v1/manifest`.

- 2026-09-17: **FAR-936** — retired the AB Test Models page (`/variants/ab-test`).
  Removed route, view, i18n (`views.ABTestModelsView`), manifest entry, elements
  inventory, tests, and sidebar nav entry. The page's variant-builder capability
  moved onto the `/variants/compare` page as an inline builder
  (`components/variants/VariantGroupBuilder.vue`, i18n `views.variantCreator`), so
  the A/B comparison use case stays reachable: the page's "New Comparison" button
  opens the builder, the `pipeline_id` deep-link from PipelineListView pre-selects
  the pipeline, and firing a batch navigates to the batch-compare detail route.

- 2026-08-27: **product-map review pass** — added this behaviour-tracker
  for the registered manifest feature `feat-variants`, which previously had no
  `docs/product-map/` entry. Behaviours verified against `api/routes/variants.py` and
  `tests/bdd/features/variants/variant_groups.feature`. Status: covered (with the known
  BDD gaps called out above).
