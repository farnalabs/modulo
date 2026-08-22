---
id: feat-core-product-analytics-transparency
prd: 10.5
delivery-tasks: [task-product-analytics-transparency]
code:
  - backend/src/modulo/api/routes/product_analytics_transparency.py
  - backend/src/modulo/api/main.py
  - frontend/src/views/AdminProductAnalyticsView.vue
  - frontend/src/stores/productAnalyticsStore.ts
  - frontend/src/router/index.ts
depends-on: []
status: partial
---

# Product Analytics Transparency

Admin-facing transparency page that exposes how product analytics data is
collected, how often it is delivered to Farnalabs, the current consent level,
and whether enforcement is active. Backed by the
`GET /api/v1/product-analytics/transparency` endpoint.

## Behaviours

### Transparency endpoint
- [x] Returns last successful dump timestamp, total dump count, consent level, instance-enabled flag, enforcement-enabled flag, and an optional staleness warning.
- [x] Stale-warning (`not_reaching_farnalabs`) is raised only when consent level is `all` and the last dump is older than the configured threshold.
- [x] Raises 501 when the required SystemConfig rows do not exist (DB not migrated) and 503 on other database errors.
