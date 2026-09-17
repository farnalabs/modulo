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
- [x] Variant groups can be listed, fetched, updated, deleted and restored
      (ownership asserted per organisation, `api/routes/variants.py`)
- [x] Comparison surfaces include per-node eval scores, prompt diffs and coverage gaps
      (`prompt_diffs`, `coverage_gaps`, `batch_compare` endpoints)
- [x] The variant batch-compare UI is gated by the `variant_batch_compare` feature flag
      and hard-replaces the legacy AB-test view when enabled (frontend router guard)
- [x] Variant groups are created and batch-fired from the inline builder on
      `/variants/compare` (`components/variants/VariantGroupBuilder.vue`), which
      honours the `pipeline_id` deep-link from a pipeline's "Run as variant" action

## Known Gaps

- BDD scenarios for sequential execution order and eval-score comparison are tagged
  `@awaiting-implementation` in `variant_groups.feature`.

## QA History

- 2026-09-12: **improve-architecture (product-map walk)** — registered the shared
  `JsonViewer` surface (`components/shared/JsonViewer.vue` static testids
  `json-viewer` / `json-viewer-{copy,expand-all,collapse-all,string-expand,string-collapse}`)
  in the manifest `elements:` inventory for `/variants/compare` and
  `/variants/compare/:batchId`: both pages render the compared variant outputs
  inline with `<JsonViewer>` (`VariantCompareView.vue` / `VariantBatchCompareView.vue`),
  so the viewer shipped in the DOM while staying invisible to Remy's docs indexer /
  `/api/v1/manifest`. The component is now part of both routes' reverse
  testid-coverage guard (`test_mapped_route_elements_cover_owning_view_testids`).

- 2026-09-11: **improve-architecture (product-map walk)** — extended the reverse
  testid-coverage guard (`test_mapped_route_elements_cover_owning_view_testids`) to
  `/variants/compare` and `/variants/compare/:batchId`: the whole-page view(s) `VariantCompareView.vue` and `VariantBatchCompareView.vue` render static `data-testid`s that the
  product map `elements:` inventory already documents, but the surface was not yet
  guarded against drift. The route now maps to its owning view so a newly shipped
  testid can no longer silently stay invisible to Remy's docs indexer /
  `/api/v1/manifest`.

- 2026-09-17: **FAR-936** — retired the AB Test Models page (`/variants/ab-test`).
  Removed route, view, i18n (`views.ABTestModelsView`), manifest entry, elements
  inventory, tests, and sidebar nav entry. The page's variant-builder capability
  moved onto the `/variants/compare` page as an inline builder
  (`components/variants/VariantGroupBuilder.vue`, i18n `views.variantCreator`), so
  the A/B comparison use case stays reachable: the page's "New Comparison" button
  opens the builder, the `pipeline_id` deep-link from PipelineListView pre-selects
  the pipeline, and firing a batch navigates to the batch-compare detail route.

- 2026-08-27: **improve-architecture (product-map walk)** — added this behaviour-tracker
  for the registered manifest feature `feat-variants`, which previously had no
  `docs/product-map/` entry. Behaviours verified against `api/routes/variants.py` and
  `tests/bdd/features/variants/variant_groups.feature`. Status: covered (with the known
  BDD gaps called out above).
