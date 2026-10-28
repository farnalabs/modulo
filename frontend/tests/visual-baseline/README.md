# PrimeVue visual baseline (FAR-2205)

Reference artefacts and the comparison tool used to measure the PrimeVue 5 → 4
downgrade. The standard of equivalence for that change is **no unexplained
difference** — every measured difference is either eliminated or explicitly
justified in the delivery report. This directory holds the v5 reference and the
tool that produces the delta table.

## What is captured

`frontend/tests/e2e/primevue-visual-baseline.spec.ts` (Playwright, local target
only — the mock API is deterministic) captures, for each route and interactive
state:

- a full-page PNG (`fullPage` for routes, viewport for overlays),
- the computed `--p-*` design tokens on `<html>`, and
- a computed-style fingerprint of every PrimeVue component root on the page
  (font size, padding, height, colours, radii, shadow, plus its text).

| Viewport | Captures |
| --- | --- |
| 1280×800 | 13 routes: dashboard, pipelines, pipeline editor, runs, run detail, settings/license, settings/triggers, settings/mcp, schemas, admin/users, admin/connectors, admin/model-backends, admin/run-retention |
| 1280×800 | 7 interactive states: pipelines row `Menu`, folder `Dialog`, runs `Select` open, licence `Dialog`, admin-users `Dialog`, parameter-schemas `Tabs`, dashboard `Tooltip` |
| 390×844 | dashboard, pipelines, runs |

`frontend/src/__tests__/primevue-component-dom.spec.ts` mounts all fifteen
PrimeVue components the app imports (button, dialog, inputtext, card, badge,
menu, selected, tabs + tablist + tab + tabpanels + tabpanel, popover, tooltip)
in their used variants and pins the rendered DOM with `toMatchSnapshot()`. That
snapshot is the **permanent** regression guard; the JSON signature it can emit
(`PRIMEVUE_DOM_OUT`) is the cross-version comparison record.

## Files

| Path | Meaning |
| --- | --- |
| `versions.json` | dependency versions the v5 reference was captured on |
| `v5/tokens.json` | union of every computed `--p-*` value across all captures (818 tokens; zero route-to-route conflicts) |
| `v5/fingerprints.json` | per-capture component computed-style fingerprints |
| `v5/dom.json` | per-component rendered-DOM signature (v5) |
| `compare.mjs` | diffs two capture directories into a delta table |

PNG screenshots are **not** committed: they are ~1.8 MB per run and are
reproducible from the spec. Capture them with:

```sh
cd frontend
PRIMEVUE_BASELINE_OUT=test-results/primevue-baseline-v5 \
  pnpm exec playwright test tests/e2e/primevue-visual-baseline.spec.ts --workers=1
```

## Comparing two captures

```sh
cd frontend
node tests/visual-baseline/compare.mjs \
  --expected test-results/primevue-baseline-v5 \
  --actual   test-results/primevue-baseline-v4 \
  --json     test-results/primevue-delta.json
```

Each page reports the changed `--p-*` tokens, the changed component computed
styles (grouped per selector+property with an instance count) and the
screenshot pixel delta (percentage of pixels differing beyond the per-channel
threshold, maximum channel delta and the bounding box of the change). A
dimension change is reported explicitly rather than silently compared as an
overlap.
