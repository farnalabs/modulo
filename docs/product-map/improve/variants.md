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

Variant groups — A/B test models and batch comparison on `/variants/compare` and
`/variants/ab-test`. A variant group bundles weighted variants (optional
`run_context_overrides`) and fires one run per variant; comparison surfaces eval scores
per node, prompt diffs and eval coverage gaps, and a batch-compare flow
(`/variants/compare/:batchId`) is activated by the `variant_batch_compare` feature flag.

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

## Known Gaps

- BDD scenarios for sequential execution order and eval-score comparison are tagged
  `@awaiting-implementation` in `variant_groups.feature`.

## QA History

- 2026-08-27: **improve-architecture (product-map walk)** — added this behaviour-tracker
  for the registered manifest feature `feat-variants`, which previously had no
  `docs/product-map/` entry. Behaviours verified against `api/routes/variants.py` and
  `tests/bdd/features/variants/variant_groups.feature`. Status: covered (with the known
  BDD gaps called out above).
