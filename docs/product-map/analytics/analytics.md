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
bdd: []
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

## Known Gaps

- No dedicated BDD feature files for `/analytics`; coverage is via the unit suite and
  the integration endpoint test.

## QA History

- 2026-08-27: **improve-architecture (product-map walk)** — added this behaviour-tracker
  for the registered manifest feature `feat-analytics`, which previously had no
  `docs/product-map/` entry. Behaviours verified against `api/routes/analytics.py`,
  `core/analytics/*` and the analytics unit/integration suites. Status: covered.
