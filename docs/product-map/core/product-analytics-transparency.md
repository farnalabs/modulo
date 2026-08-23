---
id: feat-core-product-analytics-transparency
prd: 10.5
delivery-tasks: [task-product-analytics-transparency]
bdd:
  - backend/tests/bdd/features/product_analytics/transparency.feature
code:
  - backend/src/modulo/api/routes/product_analytics_transparency.py
  - backend/src/modulo/api/main.py
  - frontend/src/views/AdminProductAnalyticsView.vue
  - frontend/src/stores/productAnalyticsStore.ts
  - frontend/src/router/index.ts
depends-on: []
status: covered
---

# Product Analytics Transparency

Admin-facing transparency page that exposes how product analytics data is
collected, how often it is delivered to Farnalabs, the current consent level,
and whether enforcement is active. Backed by the
`GET /api/v1/product-analytics/transparency` endpoint.

## Behaviours

### Transparency endpoint
- [x] Returns last successful dump timestamp, total dump count, consent level, instance-enabled flag, enforcement-enabled flag, and an optional staleness warning.
- [x] Missing SystemConfig rows fall back to safe defaults (`consent_level: off`, `dump_count_total: 0`, instance/enforcement disabled, `last_successful_dump_at: null`, no warning).
- [x] Stale-warning (`not_reaching_farnalabs`) is raised only when consent level is `all` and the last dump is older than the configured threshold.
- [x] A fresh dump (or any consent level other than `all`) yields no staleness warning.
- [x] Requires `system.config.manage` — non-system-admin principals get 403; unauthenticated requests get 401.
- [x] Raises 501 when the required SystemConfig rows do not exist (DB not migrated) and 503 on other database errors.

## Known Gaps

~~No BDD feature file~~ — **RESOLVED 2026-08-23**: added `backend/tests/bdd/features/product_analytics/transparency.feature` (9 scenarios) with co-located step definitions driving the real `/api/v1/product-analytics/transparency` route. Covers stored-state echo, safe defaults, the stale-dump warning rule (stale vs fresh vs non-all consent), the system-admin auth gate (403 org admin / 401 unauthenticated), and the ProgrammingError→501 / SQLAlchemyError→503 error mapping. Note: the endpoint previously had **zero** test coverage (unit, integration, or BDD) — this is its first automated coverage. Status: partial → covered.

## QA History

- 2026-08-23 — improve-architecture (product-map walk): closed the `--uncovered` BDD gap for `feat-core-product-analytics-transparency` (one of 14 entries with empty `bdd:`). Added the co-located BDD feature + step definitions, expanded the behaviour list with fallback-defaults and auth-gating checkboxes, resolved the no-BDD known gap, `status: partial` → `covered`.
