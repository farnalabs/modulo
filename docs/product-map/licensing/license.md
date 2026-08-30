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
  - backend/tests/unit/test_license_adversarial.py
  - backend/tests/unit/test_team_license_activation.py
bdd:
  - backend/tests/bdd/features/licensing/license_management.feature
  - backend/tests/bdd/features/licensing/team_gates.feature
  - backend/tests/bdd/steps/test_license_management.py
  - backend/tests/bdd/steps/test_team_gates.py
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
      supports per-flag detail and override toggles, and 404s unknown flags
      (`test_admin_feature_flags.py`)
- [x] Keys are cryptographically verified (`core/license_signing.py`) and exercised
      adversarially (`test_license_adversarial.py`, `test_license_key.py`)
- [x] Tier activation feeds licensing/feature parity for team surfaces
      (`test_team_license_activation.py`)

## Known Gaps

- **`stripe_webhook.py` and `admin_tiers.py` are cited as adjacents but not behaviour-covered
  here** — subscription fulfilments and the tier catalogue are separate surfaces under the
  same feature id; their behaviours are not asserted by the licensing BDD suite.
- **No executing BDD surface for feature-flag inspection** —
  `licensing/feature_flag_inspection.feature` ships under
  `tests/bdd/features/licensing/` but no step module registers it via
  `scenarios(...)`, so it never executes and is no longer cited as coverage here.
  The inspection/override endpoints are unit-tested
  (`test_admin_feature_flags.py`); wiring the feature file up needs its missing
  step definitions written.

## QA History

- 2026-08-27: **improve-architecture (product-map walk)** — added this behaviour-tracker
  for the registered manifest feature `feat-license`, which previously had no
  `docs/product-map/` entry. Behaviours verified against `core/license.py`,
  `core/license_signing.py`, `api/routes/admin_license.py` and the licensing BDD/unit
  suites. Status: covered.
