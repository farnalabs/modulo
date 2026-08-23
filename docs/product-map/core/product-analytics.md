---
id: feat-core-product-analytics
prd: 10.5
delivery-tasks: []
bdd:
  - backend/tests/bdd/features/product_analytics/metrics_ingest.feature
code:
  - backend/src/modulo/api/routes/metrics_ingest.py
  - backend/src/modulo/core/product_analytics/metrics_constants.py
  - backend/src/modulo/db/models/metrics_staging.py
  - backend/src/modulo/db/migrations/versions/0121_metrics_staging.py
unit-tests:
  - backend/tests/unit/product_analytics/test_metrics_ingest.py
depends-on: []
status: covered
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

- Flagship `api_error` route-template behaviour was preserving only raw paths — fixed 2026-08-23: the sanitizer now walks FastAPI 0.130+ lazy `_IncludedRouter` trees so registered templates are matched (previously every `api_error` route degraded to `"unknown"`).

## QA History

- 2026-08-23: improve-architecture: closed the "No BDD feature file" gap — added `backend/tests/bdd/features/product_analytics/metrics_ingest.feature` (15 scenarios) with co-located step definitions driving the real `POST /api/v1/metrics/events` route against mocked CRUD/RLS deps. Covers valid/multi-event batches, consent-gate no-writes (off / no settings / missing org), `api_error` daily-cap skip + under-cap staging, duplicate `event_id` best-effort, DB-failure best-effort, route-template sanitisation (unmatched → `"unknown"`, registered template preserved), and 422 validation for empty/oversized/unknown-type/missing-field batches. Status: `partial` → `covered`.
  - Found + fixed a real sanitizer bug while writing the BDD case: `_sanitize_route_template` only scanned `app.routes[].path`, which is `None` for FastAPI 0.130+ lazy `_IncludedRouter` wrappers, so no registered template ever matched. Replaced with `_registered_path_templates()` (recursive router-tree walk) + added unit regression test `test_registered_route_template_preserved`.
- 2026-08-21: branch-fixer: created entry so the `metrics_ingest.py` route module is referenced by the product map graph (route-orphan check).
