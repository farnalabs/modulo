---
id: feat-license
prd: N/A
adr: []
code:
  - backend/src/modulo/core/license.py
  - backend/src/modulo/core/license_signing.py
  - backend/src/modulo/core/feature_flags.py
  - backend/src/modulo/api/routes/admin_license.py
  - backend/src/modulo/api/routes/admin_tiers.py
  - backend/src/modulo/api/routes/admin_feature_flags.py
  - backend/src/modulo/api/routes/stripe_webhook.py
unit-tests:
  - backend/tests/unit/core/test_license_key.py
  - backend/tests/unit/core/test_license_signing.py
  - backend/tests/unit/api/test_admin_license.py
  - backend/tests/unit/api/test_admin_feature_flags.py
  - backend/tests/unit/api/test_admin_tiers_endpoint.py
  - backend/tests/unit/api/test_stripe_webhook.py
  - backend/tests/unit/test_license_adversarial.py
  - backend/tests/unit/test_team_license_activation.py
bdd:
  - backend/tests/bdd/features/licensing/license_management.feature
  - backend/tests/bdd/features/licensing/team_gates.feature
  - backend/tests/bdd/features/licensing/feature_flag_inspection.feature
  - backend/tests/bdd/features/licensing/stripe_billing.feature
  - backend/tests/bdd/steps/test_license_management.py
  - backend/tests/bdd/steps/test_team_gates.py
  - backend/tests/bdd/steps/test_feature_flag_inspection.py
  - backend/tests/bdd/steps/test_stripe_billing.py
  - backend/tests/bdd/features/admin/tier_catalog.feature
  - backend/tests/bdd/features/admin/test_tier_catalog_steps.py
depends-on:
  - feat-teams
status: covered
---

# Feature Licensing and Plan Tiers

Licensing manages Ed25519-signed license keys and the Community / Team tier gates on
`/settings/license`. An uploaded key is verified (validity period + signature), expands to
the feature set for its tier, gates Team-scoped surfaces (SSO, team RBAC, audit viewer,
admin spend limits) with a 402 when absent or expired, and feeds the public `/api/v1/license`
and feature-flag inspection endpoints.

## Behaviours

- [x] A valid Team license uploads (200) and reports its tier plus features
      (`sso, team_rbac, audit_viewer, admin_spend_limits`); a tampered key is rejected
      (422 "Signature") and an expired key is rejected (422 "expired")
      (`license_management.feature`)
- [x] With no license, GET `/api/v1/admin/license` reports the community tier with
      `has_license: false`; after storage it reports tier / features / org / expires_at
      with `has_license: true` (`license_management.feature`)
- [x] Non-admins are denied license management (403) (`license_management.feature`)
- [x] Team gating: without a Team license, SSO providers, `/api/v1/teams`, audit export
      and admin spend limits all return 402 naming the gated feature; they pass (200) with
      a valid license; expiry degrades those surfaces back to community; community
      features stay accessible without a license (`team_gates.feature`)
- [x] Feature-flag inspection returns the `license` object plus the active `flags` array,
      supports per-flag detail and override toggles, and 404s unknown flags; the public
      `/api/v1/license` surface reports a `tier` and `features` list
      (`test_admin_feature_flags.py` + `feature_flag_inspection.feature`)
- [x] Keys are cryptographically verified (`core/license_signing.py`) and exercised
      adversarially (`test_license_adversarial.py`, `test_license_key.py`)
- [x] Tier activation feeds licensing/feature parity for team surfaces
      (`test_team_license_activation.py`)
- [x] Tier catalogue: GET `/api/v1/admin/tiers` lists the org's tiers ordered by rank with
      `tier_id` / `label` / `rank` / `requires_license` / `description`; authenticated
      non-admins can list tiers, unauthenticated requests are 401, an empty catalogue
      returns an empty list, and the tier query's programming / database errors degrade
      to 501 / 503 (`admin/tier_catalog.feature`, `test_tier_catalog_steps.py`,
      `test_admin_tiers_endpoint.py`)
- [x] Stripe purchase webhook: POST `/api/v1/webhooks/stripe` verifies the raw body
      against its `Stripe-Signature` header (HMAC-SHA256 over `<timestamp>.<raw_body>`
      with a ±300s replay window) and FAILS CLOSED (400) on a missing / invalid /
      tampered / stale-timestamp signature or a non-JSON payload — never reaching
      fulfilment; a valid `invoice.paid` dispatches `fulfil_team_purchase` exactly once
      as a background task with the `customer_email` and `org_name` extracted,
      `checkout.session.completed` is acknowledged but NEVER fulfils (the FAR-180
      single-fulfilment guard against a double-issued licence), an event without a
      customer email or of an unrelated type is acknowledged without dispatch, and the
      webhook is 404 when Stripe is not configured (`licensing/stripe_billing.feature`,
      `test_stripe_billing.py`, `test_stripe_webhook.py`)

## Known Gaps

None acknowledged: the previously-tracked "`stripe_webhook.py` and `admin_tiers.py`
are cited as adjacents but not behaviour-covered" gap was closed by the 2026-09-23
product-map walk — the tier catalogue now has executing BDD coverage
(`admin/tier_catalog.feature`) and the Stripe purchase webhook ships a dedicated
executing BDD surface (`licensing/stripe_billing.feature`), both cited above.

## QA History

- 2026-09-23: **product-map review pass** — closed `feat-license`'s
  "`stripe_webhook.py` / `admin_tiers.py` not behaviour-covered" gap
  (`docs/product-map/licensing/license.md`). Registered the new
  `licensing/stripe_billing.feature` into the executing BDD suite from the new
  `steps/test_stripe_billing.py` (10 scenarios), driving the REAL
  `POST /api/v1/webhooks/stripe` route with only the `fulfil_team_purchase`
  background-task seam and the `get_settings` seam patched: valid HMAC-signed
  `invoice.paid` → 200 + exactly one `fulfil_team_purchase` dispatch carrying
  `event_id` / `customer_email` / `org_name`; `checkout.session.completed` and a
  checkout→invoice pair → exactly one fulfilment total (the FAR-180
  double-issue guard); an invoice without a customer email and an unrelated
  event type → 200 with no dispatch; missing/invalid signature, a tampered body
  (signed one payload, sent another), a stale ±300s timestamp, and a non-JSON
  payload → 400 with no dispatch; and Stripe-disabled → 404. Signature
  verification runs for real (the route's own `verify_stripe_signature` /
  `verify_timestamp` seams), not mocked. Also wired the already-executing
  `admin/tier_catalog.feature` (`GET /api/v1/admin/tiers`) into this entry's
  citations as the tier-catalogue behaviour it belongs to, and added
  `test_admin_tiers_endpoint.py` / `test_stripe_webhook.py` to the unit-test
  citations. `_ORPHANED_BDD_FEATURES` stays empty.

- 2026-09-14: **product-map review pass** — closed the "no executing BDD
  surface for feature-flag inspection" gap: wired `licensing/feature_flag_inspection.feature`
  into the executing suite via the new `steps/test_feature_flag_inspection.py` (5 scenarios)
  and dropped the file from the tracked orphaned-BDD debt list (`_ORPHANED_BDD_FEATURES`).
  The feature exercises the real `/api/v1/admin/feature-flags` routes
  (`admin_feature_flags.py`) with only the DB reads patched (the licensing hermetic-mock
  pattern): the list endpoint's `license` object + `flags` array shapes, per-flag field
  presence, the unknown-flag 404, the toggle override write-back (`overridden: true`) and
  the public `/api/v1/license` surface. Feature-flag inspection is no longer unit-tested only.

- 2026-09-11: **product-map review pass** — extended the reverse
  testid-coverage guard (`test_mapped_route_elements_cover_owning_view_testids`) to
  `/settings/license`: the whole-page view(s) `SettingsLicenseView.vue` render static `data-testid`s that the
  product map `elements:` inventory already documents, but the surface was not yet
  guarded against drift. The route now maps to its owning view so a newly shipped
  testid can no longer silently stay invisible to Remy's docs indexer /
  `/api/v1/manifest`.

- 2026-08-27: **product-map review pass** — added this behaviour-tracker
  for the registered manifest feature `feat-license`, which previously had no
  `docs/product-map/` entry. Behaviours verified against `core/license.py`,
  `core/license_signing.py`, `api/routes/admin_license.py` and the licensing BDD/unit
  suites. Status: covered.
