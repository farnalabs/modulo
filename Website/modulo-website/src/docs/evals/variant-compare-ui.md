---
title: Variant Compare UI
---

# Variant Compare UI

Side-by-side eval scores, token costs, and output diffs across A/B test variants.

- Route: `/variants/compare`
- PRD: §8.19

Displays a comparison table with one row per pipeline node and one column per variant. Each cell shows a pass/fail/partial badge based on eval results. Footer summary shows per-variant pass rate, total cost, token total, and HITL counts.

Includes an output diff viewer with side-by-side JSON rendering, node selector, and Variant A/B selectors. Run variants via the "Run Variants" button with weighted random selection.

See the [PRD §8.19](../../prd.md#819-variant-ab-testing) for the full specification.
