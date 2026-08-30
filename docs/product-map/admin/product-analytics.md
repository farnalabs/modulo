---
id: feat-product-analytics
prd: N/A
adr: []
code:
  - backend/src/modulo/api/routes/product_analytics.py
  - backend/src/modulo/api/routes/product_analytics_identity.py
  - backend/src/modulo/api/routes/product_analytics_transparency.py
  - backend/src/modulo/core/product_analytics
unit-tests:
  - backend/tests/unit/product_analytics/test_consent.py
  - backend/tests/unit/product_analytics/test_instance_identity.py
  - backend/tests/unit/product_analytics/test_license_enforcement.py
  - backend/tests/unit/product_analytics/test_metrics_dump.py
  - backend/tests/unit/product_analytics/test_metrics_ingest.py
  - backend/tests/unit/product_analytics/test_routes.py
bdd:
  - backend/tests/bdd/features/product_analytics/metrics_ingest.feature
depends-on:
  - feat-license
status: covered
---

# Product Analytics

Product usage and adoption analytics for system administrators on
`/admin/product-analytics`. The instance reports anonymised/consented usage metrics to
the vendor via an HMAC-signed ingest endpoint; an instance identity and a transparency
surface keep collection explicit, and license enforcement gates the reporting behind
an eligible tier.

## Behaviours

- [x] Usage metrics are collected and dumped per reporting window under explicit
      consent (`core/product_analytics/consent.py`, `metrics_dump.py`,
      `tests/unit/product_analytics/test_consent.py`, `test_metrics_dump.py`)
- [x] Metrics are delivered to the vendor ingest endpoint and verified by HMAC
      (`core/product_analytics/hmac_verify.py`, `vendor_client.py`,
      `test_metrics_ingest.py`, `tests/bdd/features/product_analytics/metrics_ingest.feature`)
- [x] A stable instance identity is generated and persisted per install
      (`core/product_analytics/instance_identity.py`, `test_instance_identity.py`)
- [x] The system-admin surface exposes the collected metrics on `/admin/product-analytics`
      (`api/routes/product_analytics.py`, `test_routes.py`)
- [x] Reporting is gated by license/plan eligibility and the instance-level kill
      (`core/product_analytics/license_enforcement.py`, `test_license_enforcement.py`)
- [x] Identity and transparency endpoints disclose collection state and allow opt-out
      (`api/routes/product_analytics_identity.py`, `product_analytics_transparency.py`)

## Known Gaps

- Metrics telemetry is vendor-bound; a fully self-hosted, in-product analytics
  warehouse is not a shipped surface (that is the scope of `feat-analytics`).

## QA History

- 2026-08-27: **improve-architecture (product-map walk)** — added this behaviour-tracker
  for the registered manifest feature `feat-product-analytics`, which previously had no
  `docs/product-map/` entry. Behaviours verified against `api/routes/product_analytics*.py`,
  `core/product_analytics/*` and the product-analytics unit/BDD/integration suites
  (`tests/integration/test_metrics_ingest.py`, `test_product_analytics_identity.py`).
  Status: covered.
