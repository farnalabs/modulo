---
id: feat-core-product-analytics
prd: 10.5
delivery-tasks: []
bdd: []
code:
  - backend/src/modulo/api/routes/metrics_ingest.py
  - backend/src/modulo/core/product_analytics/metrics_constants.py
  - backend/src/modulo/db/models/metrics_staging.py
  - backend/src/modulo/db/migrations/versions/0121_metrics_staging.py
unit-tests:
  - backend/tests/unit/product_analytics/test_metrics_ingest.py
depends-on: []
status: partial
---

# Product Analytics Ingest (FAR-355)

Opt-in product analytics event ingest endpoint (`POST /api/v1/metrics/events`).
Curated frontend events are staged in `metrics_staging` and consumed by the
daily `metrics_dump` cron. Consent-gated via the org's
`product_analytics.level` setting (§10.5 Opt-In Telemetry).

## Behaviours

### API — Metrics Ingest (`POST /api/v1/metrics/events`)
- [x] Accepts a batch of curated events (1..`MAX_BATCH_SIZE`), 204 on success
- [x] Consent gate: 204 (no write) when `product_analytics.level` is not `all`
- [x] `api_error` events capped at `API_ERROR_DAILY_CAP` per org per day
- [x] `UNIQUE(organisation_id, event_id)` dedups duplicate inserts
- [x] Raw route paths in `api_error` payloads are sanitised against registered route templates

## Known Gaps

- **No BDD feature file** — coverage is unit only.

## QA History

- 2026-08-21: branch-fixer: created entry so the `metrics_ingest.py` route module is referenced by the product map graph (route-orphan check).
