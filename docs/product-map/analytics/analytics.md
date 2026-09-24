---
id: feat-analytics
prd: N/A
adr: []
code:
  - backend/src/modulo/api/routes/analytics.py
  - backend/src/modulo/core/analytics
unit-tests:
  - backend/tests/unit/test_analytics_builder.py
  - backend/tests/unit/test_analytics_delta.py
  - backend/tests/unit/test_analytics_facts.py
  - backend/tests/unit/test_analytics_guardrails.py
  - backend/tests/unit/test_analytics_record_facts.py
  - backend/tests/unit/test_analytics_service.py
  - backend/tests/unit/test_analytics_service_execution.py
bdd:
  - backend/tests/bdd/features/analytics/query.feature
  - backend/tests/bdd/features/analytics/test_analytics_query_steps.py
depends-on:
  - feat-runs
  - feat-feedback
status: covered
---

# Analytics

Reporting and analytics over runs, costs and facts, served on `/analytics`. The
analytics builder aggregates run-derived facts/deltas into a queryable surface,
with an event-bus ingestion path (`record_facts`) so downstream cost and guardrail
reports resolve against a consistent fact model.

## Behaviours

- [x] Analytics facts are built from run outputs and cost components into structured
      fact/delta records (`core/analytics/builder.py`, `tests/unit/test_analytics_builder.py`)
- [x] Fact ingestion (`record_facts`) persists run-derived facts for later query
      (`tests/unit/test_analytics_record_facts.py`)
- [x] Analytics deltas and guardrail outcomes are reconciled into the report surface
      (`tests/unit/test_analytics_delta.py`, `test_analytics_guardrails.py`)
- [x] The analytics service executes queries against the aggregated fact model
      (`tests/unit/test_analytics_service.py`, `test_analytics_service_execution.py`)
- [x] `/analytics` and `/api/v1/analytics` expose the aggregated surface over the HTTP
      API (`backend/tests/integration/test_analytics_endpoint.py`)
- [x] The REST surface is permission-gated (`analytics.query`), org-context-required
      (a principal without an org context is 403), feature-gated (`analytics_page`,
      402 when disabled) and refuses unauthenticated callers (401);
      FastAPI-level validation rejects a malformed `date_from` and a `limit`
      outside 1..1000 with 422 (`backend/tests/bdd/features/analytics/query.feature`)
- [x] The route maps the service's typed errors to HTTP: an inverted/over-wide range
      is 422, a rate-limited org is 429 and a statement timeout is 503, while the
      bucketed response carries the `group_by`/`dimension` envelope and the
      freshness indicators (`facts_freshness_hours` / `facts_stale`)
- [x] A repeated `pipeline_id` composes an A/B comparison (both ids reach the
      service) and a `dimension` is echoed back on the response
- [x] Export (`/export`) returns paginated JSON rows (`items`/`total`/`offset`
      /`limit`) and a CSV attachment with a `Content-Disposition: attachment`
      header for `format=csv`
- [x] Scan export (`/scan`) streams the WHOLE matching fact set in one response
      (NDJSON by default, or a CSV attachment) with no offset/limit pagination —
      the server keyset-paginates over the stable `(run_date, created_at,
      run_id)` order in fixed page-size batches, sharing the typed filters, org +
      team-boundary scoping, per-org rate limit and bounded statement timeout of
      `/export` (`backend/tests/unit/test_analytics_service_execution.py`)
- [x] Concurrency (`/concurrency`) reports the pooled slot-utilisation series
      (`pool_reference` + per-bucket `max_active`/`avg_active`/`max_queued`/
      `avg_queued`), and the guardrail scorecard (`/guardrails`) is labelled
      `advisory_only: true`

## Known Gaps

- Per-org analytics rate limiting is a best-effort in-memory window (60/min) —
  not a shared Redis-scaled limiter across a fleet of workers.

## QA History

- 2026-09-21: **product-map review pass** — closed the
  "No server-side streaming / scan export for the whole org in one response"
  gap. Added `GET /api/v1/analytics/scan` (NDJSON `application/x-ndjson`
  default, CSV attachment for `format=csv`) with a new
  `stream_export_facts` service generator that keyset-paginates over
  `(run_date, created_at, run_id)` in fixed `_SCAN_PAGE_SIZE` batches inside a
  single RLS-pinned session, sharing the typed filters, team-boundary,
  per-org rate limit and statement-timeout with the paginated `/export`. The
  route primes the generator so validation / rate-limit / migration / database
  errors surface as real HTTP statuses before streaming starts. Unit-covered
  (`TestStreamExportFacts` / `TestKeysetCursor`, incl. a SQLite conformance
  smoke of the portable OR-based cursor) and integration-covered
  (`TestScanEndpoint`: NDJSON content, CSV attachment, empty org, org
  isolation, feature-gate 402, unauthenticated 401). The manifest `feat-analytics`
  deferral is demoted to only the Redis-scaled limiter follow-up.

- 2026-09-19: **product-map review pass** — closed the "No
  dedicated BDD feature files for `/analytics`" gap. Registered
  `analytics/query.feature` into the executing BDD suite from the colocated
  `features/analytics/test_analytics_query_steps.py`, driving the REAL
  `modulo/api/routes/analytics.py` routes with only the four service functions
  patched: the `/query` envelope + freshness indicators, the repeated
  `pipeline_id` A/B composition (both ids asserted to reach the service) and
  dimension echo, FastAPI-level validation (malformed date / `limit` bound →
  422), the typed error mapping (inverted range → 422, rate limit → 429,
  statement timeout → 503), the permission gate (a principal without an org
  context → 403) and the feature gate (`analytics_page` disabled → 402), an
  explicit unauthenticated 401 (HTTPBearer with no `Authorization` header),
  the JSON + CSV export surfaces, the concurrency slot-utilisation series and
  the advisory-only guardrail scorecard. Status remains covered;
  `_ORPHANED_BDD_FEATURES` stays empty.

- 2026-09-11: **product-map review pass** — extended the reverse
  testid-coverage guard (`test_mapped_route_elements_cover_owning_view_testids`) to
  `/analytics`: the whole-page view(s) `AnalyticsView.vue` render static `data-testid`s that the
  product map `elements:` inventory already documents, but the surface was not yet
  guarded against drift. The route now maps to its owning view so a newly shipped
  testid can no longer silently stay invisible to Assistant's docs indexer /
  `/api/v1/manifest`.

- 2026-08-27: **product-map review pass** — added this behaviour-tracker
  for the registered manifest feature `feat-analytics`, which previously had no
  `docs/product-map/` entry. Behaviours verified against `api/routes/analytics.py`,
  `core/analytics/*` and the analytics unit/integration suites. Status: covered.
