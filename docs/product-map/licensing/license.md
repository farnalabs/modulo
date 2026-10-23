---
id: feat-license
prd: N/A
adr: []
code:
  - backend/src/modulo/core/license.py
  - backend/src/modulo/core/license_signing.py
  - backend/src/modulo/core/feature_flags.py
  - backend/src/modulo/core/stripe_fulfilment.py
  - backend/src/modulo/core/product_analytics/license_enforcement.py
  - backend/src/modulo/api/routes/admin_license.py
  - backend/src/modulo/api/routes/admin_tiers.py
  - backend/src/modulo/api/routes/admin_feature_flags.py
  - backend/src/modulo/api/routes/admin_orgs.py
  - backend/src/modulo/api/routes/stripe_webhook.py
  - backend/src/modulo/api/routes/viewmodel.py
  - frontend/src/stores/planStore.ts
unit-tests:
  - backend/tests/unit/core/test_license_key.py
  - backend/tests/unit/core/test_license_signing.py
  - backend/tests/unit/core/test_stripe_fulfilment.py
  - backend/tests/unit/api/test_admin_license.py
  - backend/tests/unit/api/test_admin_feature_flags.py
  - backend/tests/unit/api/test_admin_tiers_endpoint.py
  - backend/tests/unit/api/test_stripe_webhook.py
  - backend/tests/unit/api/test_viewmodel_license.py
  - backend/tests/unit/api/test_viewmodel_plan.py
  - backend/tests/unit/api/test_viewmodel_resolve_tier.py
  - backend/tests/unit/api/test_admin_orgs_coverage_gaps.py
  - backend/tests/unit/mcp/test_mcp_governance_read_tools.py
  - backend/tests/unit/test_license_adversarial.py
  - backend/tests/unit/test_team_license_activation.py
  - frontend/src/__tests__/planStore.spec.ts
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
status: partial
---

# Feature Licensing and Plan Tiers

Licensing manages Ed25519-signed license keys and the Community / Team tier gates on
`/settings/license`. An uploaded key is verified (validity period + signature), expands to
the feature set for its tier, gates Team-scoped surfaces (SSO, team RBAC, audit viewer,
admin spend limits) with a 402 when absent or expired, and backs the admin license-status,
tier-catalogue and feature-flag inspection endpoints. (The public, unauthenticated
`/api/v1/license` surface resolves the deployment-level licence ladder - the in-memory
licence, then a verified env-var key - and reports community / `is_valid: false` when
neither is valid; see Behaviours.)

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
      (`test_admin_feature_flags.py` + `feature_flag_inspection.feature`)
- [x] Caller-scoped plan bootstrap: GET `/api/v1/plan` answers 200 for ANY
      authenticated org member with `license.tier` / `expires_at` / `org_id`,
      `dev_mode`, and the FULL known flag set with the org `feature_overrides`
      overlay applied (inactive flags present as `currently_active: false`) -
      so a non-admin gets their org's real tier, dev mode and effective flags
      rather than the bootstrap defaults the instance-admin-only
      `/api/v1/admin/feature-flags` + `/api/v1/admin/license` reads left them
      with. The frontend `planStore` bootstraps from this one call plus the
      viewer-readable `/api/v1/admin/tiers` catalogue; a timeout / 5xx still
      settles the flag source to the terminal `unavailable` state that falls
      back to the default nav layout rather than pending forever. The admin
      endpoints keep their system-admin gates for the inspection/toggle UI
      (`viewmodel.py` `plan_bootstrap`, `test_viewmodel_plan.py`,
      `test_plan_bootstrap.py`, `frontend/src/stores/planStore.ts`,
      `planStore.spec.ts`; FAR-1806 + FAR-1809)
- [x] The public, unauthenticated `/api/v1/license` surface resolves the REAL
      deployment-level licence through the canonical ladder (rungs 2-3 of
      `feature_flags._resolve_paid_plan`; the endpoint is org-less so the
      org-level rung does not apply): an in-memory licence (`store_license`)
      reports its own tier/features with `is_valid: true`; otherwise a
      present env-var key is only trusted after `parse_and_verify`
      (Ed25519 signature + expiry) and an unverifiable key falls through;
      with no valid licence the response is community / no features /
      `is_valid: false`. The response shape stays exactly
      `tier`/`features`/`is_valid` and never exposes the raw key
      (`test_viewmodel_license.py`, `feature_flag_inspection.feature`;
      FAR-1720)
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
      tampered / stale-timestamp signature or a non-JSON payload - never reaching
      fulfilment; a valid `invoice.paid` dispatches `fulfil_team_purchase` exactly once
      as a background task with the `customer_email` and `org_name` extracted,
      `checkout.session.completed` is acknowledged but NEVER fulfils (the FAR-180
      single-fulfilment guard against a double-issued licence), an event without a
      customer email or of an unrelated type is acknowledged without dispatch, and the
      webhook is 404 when Stripe is not configured (`licensing/stripe_billing.feature`,
      `test_stripe_billing.py`, `test_stripe_webhook.py`)
- [x] Stripe fulfilment fails CLOSED on an unverifiable generated key: if the key
      signed during `fulfil_team_purchase` does not verify against the
      deployment's own configured public key (a misconfigured keypair), the key
      is NOT emailed and the Stripe event is left UNCLAIMED so a corrected
      deployment plus Stripe's retry can still fulfil the purchase - mirroring
      `admin_license.issue_license`, which answers 500 for the same condition
      instead of returning a bad key (`test_stripe_fulfilment.py`,
      `test_unverifiable_generated_key_is_not_emailed_or_claimed`)
- [x] Licence-status cache is invalidated on every licence mutation: the admin
      status cache (`license:{org_id}`, 60s TTL in `admin_license.py`) is busted
      when the underlying licence changes - a system upload
      (`POST /api/v1/admin/license`) deletes every `license:*` entry, and an
      org-level rotate/clear (`PUT/DELETE /api/v1/admin/orgs/{org_id}/license`)
      deletes that org's entry - so the next status read reflects the new state
      instead of serving the stale resolution until TTL. Invalidation is
      best-effort (fails open with a logged warning; the licence mutation has
      already committed) (`test_admin_license.py`
      `TestLicenseCacheInvalidationSystemChange`,
      `test_admin_orgs_coverage_gaps.py` set/remove invalidation tests; FAR-1719)
- [x] Env-var licence keys are VERIFIED (Ed25519 signature + expiry via
      `parse_and_verify`), never trusted on mere presence, at every
      licence-reading surface: the `/viewmodel/current` plan tier
      (`viewmodel._resolve_tier` rung 3 - an unverifiable key falls through to
      community instead of reporting "team"; already verified by FAR-1720 and
      now routed through the shared helper), the admin feature-flag list's
      `license.has_license_key` and `license.is_valid`
      (`admin_feature_flags._build_registry` / `_license_is_valid` - `is_valid`
      is no longer hardcoded `true`), and the MCP `list_feature_flags` tool's
      licence block (same `has_key` rule plus a truthful `is_valid`). A valid
      signed key still enables every one of those surfaces
      (`test_viewmodel_resolve_tier.py`, `test_admin_feature_flags.py`
      `TestBuildRegistryEnvKeyVerification` / `TestLicenseIsValid`,
      `test_mcp_governance_read_tools.py`; FAR-1733). The two env-SAML
      surfaces were licence-reading when FAR-1733 shipped; FAR-2059 removed
      the licence condition from them (below).
- [x] Licence state never gates authentication (FAR-2059/FAR-2060). A
      self-hosted key is an offline signed key delivered by email, so
      capability survives a FIXED 14-day grace past `expires_at`
      (`license.LICENCE_GRACE_DAYS`; past the grace it grants nothing), and no
      licence state may strand an administrator: the SSO login legs use
      `require_feature_anonymous(..., allow_degraded_authentication=True)`, so
      when NO valid licence resolves they pass the sign-in through (a licence
      that DOES resolve but does not grant `sso` still 402s), and
      `GET /api/v1/admin/license` answers Community with an admin principal.
      Env-var SAML availability and config build are CONFIGURATION-only
      (`sso._env_saml_available`, `auth.sso._resolve_saml_config`): a lapsed
      key neither hides the provider nor refuses to build the config, because
      either would lock an SSO-only deployment out of the licence screen. Team
      entitlement stays on the admin configuration surface
      (`require_feature("sso")` on `/api/v1/admin/sso/*`) and post-auth
      (`test_sso_licence_admin_invariant.py`,
      `test_sso_licence_degradation.py`,
      `test_sso.py::TestSamlEnvLicenceIndependence`; FAR-2059/FAR-2060)

## Known Gaps

- **License-driven gating is not applied to EVERY registered `feat-*` surface**
  - `require_feature` / `get_plan_context` gate the licensed Team-tier surfaces
  (SSO providers, `/api/v1/teams`, audit export, admin spend limits), not the
  whole feature registry (tracked as the manifest `feat-license` unchecked
  deferral). The previously-tracked "`stripe_webhook.py` and `admin_tiers.py`
  are cited as adjacents but not behaviour-covered" gap was closed by the
  2026-09-23 product-map walk - the tier catalogue now has executing BDD
  coverage (`admin/tier_catalog.feature`) and the Stripe purchase webhook ships
  a dedicated executing BDD surface (`licensing/stripe_billing.feature`), both
  cited above.

## QA History

- 2026-10-16: **FAR-1809** - documented the caller-scoped `GET /api/v1/plan`
  endpoint (delivered by **FAR-1806**; nothing here had drifted - this is a
  new-endpoint record, not a correction) and the frontend repoint onto it:
  `planStore.doFetchPlan` now reads that single call instead of the
  instance-admin `/api/v1/admin/feature-flags` + `/api/v1/admin/license`
  pair (`/api/v1/admin/tiers` stays as the viewer-readable tier catalogue),
  and the router's tier guard keys its check on `loaded` (tier known) instead
  of flag-map emptiness, so a known-empty flag set and an unknown one are no
  longer conflated. `code:` cites `frontend/src/stores/planStore.ts`;
  `unit-tests:` adds `test_viewmodel_plan.py` and `planStore.spec.ts`.

- 2026-10-13: **FAR-1733** - replaced env-var licence-key PRESENCE-trust with
  verification at the remaining surfaces. `viewmodel._resolve_tier` rung 3 had
  already been made to verify via `parse_and_verify` by **FAR-1720** earlier the
  same day, so FAR-1733 only routes it through the shared helper (behaviour
  unchanged); the surfaces actually fixed here are `admin_feature_flags`'s
  `_build_registry` and the MCP `list_feature_flags` tool (which counted the
  env key on presence for `has_license_key` AND hardcoded
  `license.is_valid = True`), `sso.is_saml_available` (a present env key
  enabled env-SAML), and `auth.sso._resolve_saml_config` (any non-empty key
  satisfied the Team-feature gate - fail-open). All now
  verify the key through one shared helper (`core.license.verify_license_key`,
  the session-free form of the canonical `_resolve_paid_plan` rung-3 check):
  an unverifiable/expired key falls through to community / `false` on the
  reporting surfaces and FAILS CLOSED on the SSO security gates (the env-SAML
  config gate raises the same 400 "requires a license key" message as an
  absent key, so the pre-auth surface never discloses whether a key is
  configured). The admin/MCP `is_valid` blocks now report whether a valid
  licence actually resolved (in-memory, verified env key, or verified
  org-level key), which resolves the residual "hardcoded `is_valid: true`"
  gap FAR-1720 left for this entry. Proved by regression tests that FAIL
  against the old code at those fixed surfaces: `test_admin_feature_flags.py`
  (`TestBuildRegistryEnvKeyVerification`, `TestLicenseIsValid`, and the
  endpoint `is_valid` block), `test_mcp_governance_read_tools.py`
  (invalid env key → `has_license_key=false`/`is_valid=false`; verified org
  key distinction), and `test_sso.py` `TestSamlEnvLicenseVerification`
  (invalid key → env-SAML unavailable / config raises / login route 400;
  valid key still enables each surface end-to-end). The SAML BDD steps
  (`sso_saml.feature`, `sso_team_mapping.feature`) now carry a genuinely
  signed key. `test_viewmodel_resolve_tier.py` pins the FAR-1720-verified rung
  against the shared-helper refactor (it passes against main too - the rung
  was already verified). Status stays `partial` (the manifest's
  universal-gating deferral is unchanged).

- 2026-10-13: **FAR-1720** - unified the public `/api/v1/license` surface
  with the real licence resolution. `viewmodel.license_info` previously
  reported `tier = "team"` iff `settings.modulo_license_key` was non-empty,
  hardcoded `features: ["notifications"]`, and always answered
  `is_valid: True` - it trusted the env-var key on mere presence and never
  consulted `get_license()`, so it could disagree with every other licence
  surface. It now mirrors rungs 2-3 of the canonical ladder
  (`feature_flags._resolve_paid_plan`): the in-memory licence
  (`store_license`) wins with its own tier/features and `is_valid: true`; a
  present env-var key is verified via `parse_and_verify` and an unverifiable
  key falls through; with no valid licence the response is community / no
  features / `is_valid: false`. The `LicenseInfo` response shape is
  unchanged (no new fields, no raw key). Proved by new regression tests in
  `test_viewmodel_license.py` (stored-licence precedence, invalid-env-key
  fall-through, unlicensed `is_valid: false`, exact response-shape pin) and
  two new `feature_flag_inspection.feature` scenarios (unlicensed public
  endpoint, stored-licence public endpoint). A review follow-up also unified
  the in-file sibling `viewmodel._resolve_tier` (which feeds
  `/viewmodel/current`'s `plan.tier`) to verify the env-var key via
  `parse_and_verify` instead of trusting its presence, so it can no longer
  disagree with `_resolve_license_info` for the same deployment state
  (`TestResolveTier` in `test_viewmodel_license.py`). The previously-tracked
  "public `/api/v1/license` is_valid/tier inconsistent with the real licence
  resolution" gap is resolved; the narrower `admin_feature_flags` /
  MCP `list_feature_flags` hardcoded `is_valid: true` residue stays tracked
  under Known Gaps (out of scope here). Status stays `partial` (the
  manifest's universal-gating deferral is unchanged).

- 2026-10-12: **Improve Architecture product-map walk** - Stripe fulfilment
  error-path hardening. `core/stripe_fulfilment.py::fulfil_team_purchase`
  logged a warning and then STILL emailed the generated licence key when the key
  failed verification against the deployment's own public key (a misconfigured
  keypair), and then claimed the event - so the customer received a licence the
  product rejects on upload and Stripe was told the purchase was fulfilled, with
  no retry. The path now fails CLOSED: no email, no claim, `return None`, so a
  corrected deployment plus Stripe's retry can still fulfil (mirroring
  `admin_license.issue_license`, which already answers 500 for the same
  condition). Added the regression test
  `test_unverifiable_generated_key_is_not_emailed_or_claimed` (fails without the
  fix, passes with it). Also completed the entry's stale citations: `code:` now
  names `core/stripe_fulfilment.py`, `core/product_analytics/license_enforcement.py`,
  `api/routes/admin_orgs.py` and `api/routes/viewmodel.py`; `unit-tests:` now
  names `test_stripe_fulfilment.py`, `test_viewmodel_license.py` and
  `test_admin_orgs_coverage_gaps.py`. Refreshed Known Gaps with the
  license-status cache-invalidation gap and the public-`/api/v1/license`
  resolution gap. Status stays `partial` (the manifest's universal-gating
  deferral is unchanged).

- 2026-10-12: **FAR-1719** - closed the licence-status cache-invalidation gap
  raised by the improve-architecture `feat-license` pass. A system upload
  (`POST /api/v1/admin/license`) now busts every `license:*` Redis entry, and
  an org-level rotate/clear (`PUT/DELETE /api/v1/admin/orgs/{org_id}/license`)
  busts the mutated org's `license:{org_id}` entry. Both invalidations are
  best-effort (fail open with a logged `license.cache_invalidate_failed`
  warning) - the licence mutation has already committed, so a Redis failure
  must not turn the request into a 500 (commit-then-error). Regression tests
  drive the real routes over a shared fakeredis server and prove the second
  status read reflects the new licence rather than the stale cache
  (`test_admin_license.py` `TestLicenseCacheInvalidationSystemChange`,
  `test_admin_orgs_coverage_gaps.py` `test_set_org_license_invalidates_org_status_cache`
  and `test_remove_org_license_invalidates_org_status_cache`). The
  previously-tracked "admin license-status Redis cache is not invalidated on a
  licence mutation" gap is resolved. The public `/api/v1/license` surface is
  intentionally untouched (FAR-1720).

- 2026-09-29: **Improve Architecture product-map walk** - reconciled the
  tracker frontmatter `status:` with the manifest `feat-license` registry: the
  entry now reads `status: partial` (matching the manifest's unchecked
  "license-driven gating is not applied to EVERY registered `feat-*` surface"
  deferral, now also named in Known Gaps) instead of `covered`. The two layers
  previously disagreed on the same feature's coverage - a reader of the graph
  got the opposite answer from the machine layer Assistant reads from the
  manifest.

- 2026-09-25: **Improve Architecture product-map walk** - sharpened the
  manifest `feat-license` registry entry: the shipped composition-root gating
  (`require_feature` / `get_plan_context`, Team-tier 402 gates verified here) is now
  ticked, while the not-yet-universal "gating across EVERY registered feature"
  claim stays unchecked (partial). No behaviour change - registry accuracy only.
- 2026-09-23: **product-map review pass** - closed `feat-license`'s
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

- 2026-09-14: **product-map review pass** - closed the "no executing BDD
  surface for feature-flag inspection" gap: wired `licensing/feature_flag_inspection.feature`
  into the executing suite via the new `steps/test_feature_flag_inspection.py` (5 scenarios)
  and dropped the file from the tracked orphaned-BDD debt list (`_ORPHANED_BDD_FEATURES`).
  The feature exercises the real `/api/v1/admin/feature-flags` routes
  (`admin_feature_flags.py`) with only the DB reads patched (the licensing hermetic-mock
  pattern): the list endpoint's `license` object + `flags` array shapes, per-flag field
  presence, the unknown-flag 404, the toggle override write-back (`overridden: true`) and
  the public `/api/v1/license` surface. Feature-flag inspection is no longer unit-tested only.

- 2026-09-11: **product-map review pass** - extended the reverse
  testid-coverage guard (`test_mapped_route_elements_cover_owning_view_testids`) to
  `/settings/license`: the whole-page view(s) `SettingsLicenseView.vue` render static `data-testid`s that the
  product map `elements:` inventory already documents, but the surface was not yet
  guarded against drift. The route now maps to its owning view so a newly shipped
  testid can no longer silently stay invisible to Assistant's docs indexer /
  `/api/v1/manifest`.

- 2026-08-27: **product-map review pass** - added this behaviour-tracker
  for the registered manifest feature `feat-license`, which previously had no
  `docs/product-map/` entry. Behaviours verified against `core/license.py`,
  `core/license_signing.py`, `api/routes/admin_license.py` and the licensing BDD/unit
  suites. Status: covered.
