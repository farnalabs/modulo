---
id: feat-product-analytics
prd: N/A
adr: []
code:
  - backend/src/modulo/api/routes/product_analytics.py
  - backend/src/modulo/api/routes/product_analytics_identity.py
  - backend/src/modulo/api/routes/product_analytics_transparency.py
  - backend/src/modulo/api/routes/metrics_ingest.py
  - backend/src/modulo/core/product_analytics
  - frontend/src/composables/useProductAnalytics.ts
  - frontend/src/composables/productAnalyticsEvents.ts
  - frontend/src/stores/productAnalyticsStore.ts
  - frontend/src/views/AdminProductAnalyticsView.vue
unit-tests:
  - backend/tests/unit/product_analytics/test_consent.py
  - backend/tests/unit/product_analytics/test_instance_identity.py
  - backend/tests/unit/product_analytics/test_license_enforcement.py
  - backend/tests/unit/product_analytics/test_metrics_dump.py
  - backend/tests/unit/product_analytics/test_metrics_ingest.py
  - backend/tests/unit/product_analytics/test_metrics_staging_consumer.py
  - backend/tests/unit/product_analytics/test_routes.py
  - backend/tests/unit/api/routes/test_product_analytics_transparency.py
  - backend/tests/unit/api/test_product_analytics_identity_hardening.py
  - backend/tests/integration/test_product_analytics_identity.py
  - frontend/src/__tests__/composables/useProductAnalytics.spec.ts
  - frontend/src/__tests__/AdminProductAnalyticsView.spec.ts
bdd:
  - backend/tests/bdd/features/product_analytics/metrics_ingest.feature
  - backend/tests/bdd/features/personas/marcus-ciso.feature
depends-on:
  - feat-license
status: partial
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
- [x] The system-admin surface at `/admin/product-analytics` reads the transparency
      endpoint (`GET /api/v1/product-analytics/transparency` -
      `api/routes/product_analytics_transparency.py`,
      `tests/unit/api/routes/test_product_analytics_transparency.py`; frontend
      `frontend/src/views/AdminProductAnalyticsView.vue`,
      `frontend/src/stores/productAnalyticsStore.ts`)
- [x] Reporting is gated by the instance-level master switch, which the dump
      gate shares with the consent surface
      (`core/product_analytics/metrics_dump.py` `_check_instance_switch` delegates to
      `consent.is_instance_analytics_enabled`, `core/product_analytics/consent.py`;
      the transparency endpoint now delegates to the same helper, so the reported
      switch and the dump gate cannot disagree)
- [x] The daily dump applies the partner `product_analytics_required` carve-out:
      `metrics_dump` reads the enforcement kill switch once per run
      (`license_enforcement.is_enforcement_active`, fail-safe enforced) and
      `_get_consenting_orgs` excludes orgs where
      `should_degrade_to_community` fires, so a degraded org can never appear
      in a vendor payload even if the consent predicate ever widens beyond
      `all` (`core/product_analytics/metrics_dump.py`,
      `tests/unit/product_analytics/test_metrics_dump.py`
      `TestGetConsentingOrgsLicenseEnforcement`)
- [ ] License/plan eligibility gating is not wired into request-time routes: the
      effective-plan resolution (`core/feature_flags.py` `resolve_plan_context` /
      `api/dependencies.py` `require_feature`) never consults `should_degrade_to_community`, so a
      degraded org keeps its paid tier on API routes until that half lands.
      The dump half above is done; the route half remains feature completion,
      not a QA fix - tracked for a follow-up ticket.
- [x] Identity and transparency endpoints disclose collection state and allow opt-out
      (`api/routes/product_analytics_identity.py`, `product_analytics_transparency.py`)
- [x] The transparency endpoint reports the instance's REAL product-analytics
      posture, every field sourced from state a feature actually writes:
      `instance_enabled` via `consent.is_instance_analytics_enabled` (bool/string
      aware - a stored `"false"` is OFF, not fail-open - with the
      `MODULO_PRODUCT_ANALYTICS_ENABLED` env fallback), `enforcement_enabled` via
      `consent.is_license_enforcement_enabled` (the real kill switch; absent =
      enforced), `last_successful_dump_at` / `dump_count_total` from the keys the
      metrics dump writes (`constants.DUMP_WATERMARK_KEY`,
      `metrics_dump.DUMP_COUNT_KEY`), and `consent_level` from the caller's
      organisation `settings_json` (read through `consent.org_consent_level`)
      - the per-org value is the PREFERRED source; when the caller org cannot be
      resolved the endpoint reports an instance-level aggregate
      (`_instance_consent_level`: `all` if any active org has opted in, else
      `off`) instead of a hardcoded `off` (FAR-1635). The dump increments
      `product_analytics_dump_count` once per
      successful (non-skipped) dump in the same transaction that advances the
      watermark (`api/routes/product_analytics_transparency.py`,
      `tests/unit/api/routes/test_product_analytics_transparency.py`)
- [x] The transparency endpoint derives the instance's data-residency posture:
      `egress_allowed` is true ONLY when the instance-level master switch AND an
      explicit `all` consent are BOTH on (`core/product_analytics/consent.py`),
      so telemetry egress is fail-closed by default and opt-in is the single
      allowed path - the Marcus CISO data-residency journey now drives this real
      transparency surface (`personas/marcus-ciso.feature`,
      `api/routes/product_analytics_transparency.py`,
      `tests/unit/api/routes/test_product_analytics_transparency.py`)
- [x] The event-ingest endpoint `POST /api/v1/metrics/events` stages consent-gated
      curated frontend events into `metrics_staging`: an org-scoped insert with
      `UNIQUE(organisation_id, event_id)` dedup (`ON CONFLICT DO NOTHING`), an
      `api_error` daily cap, route-template sanitisation so raw paths never reach
      the table, and event-specific payload validation that retains only bounded
      opaque UUID identifiers or bounded scalar API-error fields; unknown, nested,
      oversized, malformed, and PII-shaped values are dropped before staging. It
      is best-effort (always 204) and consent-gated through the shared
      `consent.is_org_consenting` predicate; the client's `event_id` is a
      per-session prefix plus a monotonic counter, so its IDs stay unique across
      page reloads and concurrent tabs (a bare counter restarting at 1 collided
      with the earlier session's IDs under this dedup and was silently dropped)
      (`api/routes/metrics_ingest.py`,
      `core/product_analytics/metrics_constants.py`,
      `frontend/src/composables/useProductAnalytics.ts`,
      `frontend/src/__tests__/composables/useProductAnalytics.spec.ts`,
      `tests/unit/product_analytics/test_metrics_ingest.py`,
      `tests/integration/test_metrics_ingest.py`,
      `tests/bdd/features/product_analytics/metrics_ingest.feature`)

- [x] Staged frontend events are consumed and purged end to end (FAR-1717): the
      daily dump's `_aggregate_staged_events` folds each UTC day's
      `metrics_staging` rows into the payload's `event_counts` section, and
      `_purge_staging` runs on EVERY `metrics_dump` cron tick before the jitter
      gate - deleting consumed rows (the watermark has passed their date) plus
      anything older than the 30-day retention bound, so the table cannot grow
      unbounded even when a dump gate skips; the supporting plain index
      `ix_metrics_staging_recorded_at` (migration 0304) serves the purge's
      `recorded_at < :cutoff` DELETE. Turning the org's level off - admin PUT or
      prompt decline - purges its staged rows immediately in the same
      transaction (`api/routes/product_analytics.py`)
      (`tests/unit/product_analytics/test_metrics_staging_consumer.py`,
      `test_routes.py`)
- [x] The frontend event-capture composable is initialised in the app root
      (FAR-1718): `App.vue` calls `initProductAnalytics` with
      `productAnalyticsStore.isOptedIn` as the live consent function, and
      curated `track()` call sites ship with pipeline create / run start,
      schema create, connector add, model-backend add, trigger create, eval
      create and HITL claim/approve/reject. No `page_view` event exists - the
      stale docstring claim of a server-side Redis counter was removed; no
      per-route data is captured, client or server side
      (`frontend/src/App.vue`,
      `frontend/src/composables/useProductAnalytics.ts`)

## Consent scoping (FAR-1635)

Decision: the transparency endpoint reports consent **per organisation, with an
instance fallback** - "per org preferable, per instance acceptable". This is
intentional, not an open question:

- **Per-org is primary.** `consent_level` / `egress_allowed` read the caller's own
  organisation `settings_json`, so a system admin sees their org's real posture.
  On a single-org (self-hosted) instance this _is_ the instance posture.
- **Instance fallback when the caller org is unknown.** When the caller has no
  `organisation_id`, or the org row cannot be found, the endpoint reports an
  instance-level aggregate (`_instance_consent_level`): `all` if at least one
  active organisation has opted in, else `off`. It no longer hardcodes `off`.
- The aggregate and the daily dump share ONE consent predicate
  (`consent.org_consent_level` / `consent.is_org_consenting`), which both
  `_instance_consent_level` and `metrics_dump._get_consenting_orgs` call, so the
  reported posture cannot drift from what actually leaves the instance. A
  malformed (non-dict) `settings_json` degrades to `off` rather than raising.

## Known Gaps

- Metrics telemetry is vendor-bound; a fully self-hosted, in-product analytics
  warehouse is not a shipped surface (that is the scope of `feat-analytics`).
- On a multi-org instance the transparency page still shows the **caller's own
  org** consent by preference; it does not display every consenting org's posture
  side by side. Reporting a full per-org breakdown to the admin is a future
  enhancement (the instance aggregate is used only when the caller org is
  unknown).
- The `/admin/product-analytics` page still cannot show in-product usage/adoption
  metrics - it renders only the transparency endpoint's delivery/consent/enforcement
  fields, which now reflect real state. An in-product analytics export/administration
  surface (self-serve downloads, configurable schedule, or a self-hosted warehouse)
  remains unshipped (warehouse scope sits under `feat-analytics`).
- Events staged AFTER the daily dump moment on dump day are not delivered: the
  dump is a point-in-time snapshot and the watermark advances to dump day, so
  the next dump starts the following day; such rows are held back by the purge
  (never deleted by their own dump day) and drop out via the 30-day retention
  bound. Closing this would require the watermark to lag a full closed day -
  which makes the vendor receive two payloads for the current date - i.e. a
  payload-dedup contract change on the vendor side, not taken in FAR-1717.

## QA History
- 2026-10-19: **Improve Architecture product-map walk** - fixed MAJOR
  (behaviour-adversarial / Lens 9 List B - *listed but vacuously covered*): the
  frontend event-capture composable generated `event_id` from a module-scoped
  counter that restarted at `1` on every page load (and was independent per
  tab), while the ingest route dedups on `UNIQUE(organisation_id, event_id)`
  with `ON CONFLICT DO NOTHING`. A reload therefore re-emitted IDs `1..N` that
  had already been staged by the earlier session and the backend silently
  discarded every one of them - analytics undercounted for the rest of the
  dedup window, with a green suite because the tests asserted the counter
  format rather than cross-session uniqueness. IDs are now a per-session prefix
  (random UUID, falling back to CSPRNG bytes and only then to a
  timestamp/random suffix) plus the monotonic counter,
  so reloads and concurrent tabs cannot collide; the regression test
  re-initialises the module and proves the new session's ID differs from the
  first (`frontend/src/composables/useProductAnalytics.ts`,
  `frontend/src/__tests__/composables/useProductAnalytics.spec.ts`).
- 2026-10-12: **Improve Architecture product-map walk** - wired the partner
  `product_analytics_required` carve-out into the daily metrics dump (the dump
  half of the license-gating behaviour): `metrics_dump` now reads
  `is_enforcement_active` once per run and `_get_consenting_orgs` drops orgs
  where `should_degrade_to_community` fires, in the same system session (one
  transaction, not two back-to-back sessions). Fixed the 10-test breakage the
  first cut left behind (the enforcement seam was patched at the defining
  module instead of the `metrics_dump` import site, so every `metrics_dump`
  caller hit the real DB read). Added `TestGetConsentingOrgsLicenseEnforcement`
  (degraded-consenting excluded, non-degraded kept, enforcement-off short
  circuit). The request-time route half (`resolve_plan_context` /
  `require_feature` degrading the effective plan) is still open and stays
  tracked as the remaining unchecked behaviour.
- 2026-10-12: **FAR-1717 + FAR-1718 (staging consumer + capture init)** - closed
  both unwired halves of the frontend ingest path. Backend: `_build_payload`
  now folds `metrics_staging` into the payload (`_aggregate_staged_events` →
  the new `event_counts` section; `SCHEMA_VERSION` bumped 1 → 2 per the module
  contract that it changes when the payload shape changes); every
  `metrics_dump` tick purges consumed + expired staging rows (`_purge_staging`,
  bounded by the watermark and a 30-day retention cap) with the supporting
  plain index `ix_metrics_staging_recorded_at` (migration 0304 - a *partial*
  index is impossible here: the purge cutoff is a moving bind parameter and
  Postgres requires IMMUTABLE index predicates); the admin PUT and the consent
  decline path purge the org's staged rows in the same transaction, replacing
  the "will be implemented in FAR-355" docstring on the level-update route.
  Frontend: `App.vue` initialises `initProductAnalytics` with the store's live
  `isOptedIn` as the consent function, curated `track()` call sites landed in
  the same change (so captured events have a consumer), and the non-existent
  `page_view` Redis-counter docstring claim was dropped. Evidence: the new
  real-SQLite suite `test_metrics_staging_consumer.py` (aggregation, payload
  section, purge cutoffs, jitter-skip wiring) and `test_routes.py::TestStagingPurgeOnOptOut`
  both fail against the pre-change code (missing functions / no DELETE executed).
- 2026-10-12: **Improve Architecture product-map walk** - documented the
  event-ingest path and found its two unwired halves (both feature completion, not
  QA fixes). (1) `POST /api/v1/metrics/events` → `metrics_staging` has no consumer
  and no purge: `metrics_dump._build_payload` reads only aggregate tables
  (`RunDailyFact`, `ErrorGroup`, entity counts) and never `MetricsStaging`, while
  `db/models/metrics_staging.py` and migration 0121 both claim the dump consumes
  and purges the rows. (2) `initProductAnalytics` / `useProductAnalytics` has no
  production call site, so the client never captures or posts an event; its
  `page_view` Redis-counter docstring describes a mechanism that does not exist.
  Also corrected the entry's frontmatter: added `api/routes/metrics_ingest.py`, the
  capture composable / event list / store / view to `code:`, and the identity-route
  hardening tests + the new frontend specs to `unit-tests:`. Verified the linked
  code read paths and re-ran the product analytics unit suites (all green); the
  multi-org soft-delete filter audit (Lens 4) was falsified by direct observation
  (the global `with_loader_criteria` filter DOES apply to the core-style
  `select(func.count()).select_from(Model)` counts, so the counts exclude
  soft-deleted rows).
- 2026-10-10: **FAR-1635 (consent scope)** - replaced the transparency
  endpoint's hardcoded `off` fallback with an instance-level posture. Per-org
  consent remains the primary source; when the caller org cannot be resolved
  (no `organisation_id`, or no matching row) the endpoint reports
  `_instance_consent_level` - `all` if any active org has opted in, else `off`.
  `_resolve_org` now returns the org row (not just its settings) so "resolved but
  no settings" stays per-org `off` and only a genuinely unresolvable org takes
  the instance fallback. Documented the decision (was an undecided Known Gap) and
  added tests for both paths; the fallback tests fail against the previous
  hardcoded-`off` behaviour.
- 2026-10-10: **deliver/pa-transparency** - closed the transparency "phantom
  keys" gap: the endpoint read five `system_config` keys that no feature wrote, so
  `/admin/product-analytics` always rendered defaults. It now sources every field
  from real state: `instance_enabled` / `enforcement_enabled` delegate to the shared
  consent helpers (`is_instance_analytics_enabled`, `is_license_enforcement_enabled`),
  `last_successful_dump_at` reads `constants.DUMP_WATERMARK_KEY`, `consent_level` reads
  the caller's org `settings_json`, and `dump_count_total` reads a new
  `product_analytics_dump_count` the metrics dump increments once per successful
  (non-skipped) dump. This removes the `bool("false") -> True` fail-open on the
  instance switch and the wrong-key enforcement badge. Tests now seed the REAL
  sources and fail without the change.
- 2026-10-09: **Improve Architecture product-map walk** - fixed CRITICAL: the daily
  metrics dump (`core/product_analytics/metrics_dump.py`) crashed with `ValueError`
  for every consenting org. `consent.apply_consent_action` / `set_level` persist
  `level_changed_at` as a full ISO datetime (`now.isoformat()`), but
  `_get_consenting_orgs` parsed it with `date.fromisoformat`, which rejects any
  string carrying a time component - so the cron raised before building a payload and
  never delivered. Added `_parse_iso_date` (date-or-datetime) and routed the live
  parse sites (the watermark read and `_get_consenting_orgs`) through it. Regression
  tests now seed the production-written format
  (the previous dump tests seeded a date-only string the writer never produces, which
  is why CI stayed green). Also documented the MAJOR transparency-writer gap above.
  Pre-PR QA gate (same day): removed the now-dead re-parse in `_resolve_start_date`
  (normalisation is owned by `_get_consenting_orgs`), pinned `_parse_iso_date`'s
  fail-hard contract with negative tests, and aligned `_check_instance_switch` with
  `consent.is_instance_analytics_enabled` - a stored string `"false"` previously read
  as truthy and ran the dump while the consent surface reported the switch OFF
  (fail-open), and the `MODULO_PRODUCT_ANALYTICS_ENABLED` fallback the surface honours
  was ignored. Corrected the Known-Gaps key count (five, not four) and the
  license/plan-gating behaviour claim (module has no production call site).
- 2026-10-03: **Improve Architecture product-map walk** - closed the CISO
  data-residency persona gap (`personas/marcus-ciso.feature`, pinned
  `@awaiting-implementation` since 2026-08): the scenario now executes against
  the REAL transparency endpoint (`GET /api/v1/product-analytics/transparency`)
  through a minimal FastAPI app (handler, permission gate, pydantic response and
  the real `is_egress_allowed` seam all run; only the `get_config` DB seam and
  the auth principal are patched - steps in `backend/tests/bdd/steps/test_personas.py`).
  Product delta closing the wire gap the scenario describes: the transparency
  response now carries `egress_allowed`, derived through the SAME
  `is_egress_allowed` seam as the consent response (instance master switch AND
  explicit `all` consent), so the data-residency posture is explicitly visible
  on the transparency/administration surface instead of being a consent-only
  read. The pin was removed from `PINNED_AWAITING_IMPLEMENTATION` and the
  scenario now executes in CI; unit coverage added in
  `tests/unit/api/routes/test_product_analytics_transparency.py`
  (`TestEgressPosture`, matrix-parametrised over both axes). The `feat-product-analytics`
  manifest registry gained the shipped egress-posture behaviour line; the tracker
  `bdd:` citations now include the persona journey.
- 2026-09-29: **Improve Architecture product-map walk** - reconciled the
  tracker frontmatter `status:` with the manifest `feat-product-analytics`
  registry: the entry now reads `status: partial` (matching the manifest's
  unchecked "in-product analytics export/administration surface is not shipped"
  deferral) instead of `covered`. The two layers previously disagreed on the
  same feature's coverage - a reader of the graph got the opposite answer from
  the machine layer Assistant reads from the manifest.
- 2026-09-26: **Improve Architecture product-map walk** - sharpened the manifest
  `feat-product-analytics` registry entry: the vague "export and scheduling is
  partially wired" gap is now split into what ships versus what is deferred.
  Scheduling ships: the jittered daily metrics-dump SAQ system cron
  (`core/product_analytics/metrics_dump.py`) gates each instance's dump window on a
  stored jitter offset aligned to the cron grid, backfills from the consented
  validity window up to the watermark, delivers to the vendor ingest endpoint, and
  advances the watermark only on full success - ticked. An in-product analytics
  export/administration surface (self-serve downloads, configurable schedule, or a
  self-hosted warehouse) remains unshipped - tracked as the unchecked deferral
  (warehouse scope sits under `feat-analytics`). Status stays `partial`.
- 2026-09-12: **product-map review pass** - registered the shared
  plan-entitlement gate surface (`components/FeatureGate.vue` + `LockIcon.vue` static
  testids `feature-gate*` / `lock-icon`) in the manifest `elements:` inventory for `/admin/product-analytics`
  and wired the two components into the reverse testid-coverage guard
  (`test_mapped_route_elements_cover_owning_view_testids`), so the entitlement-card
  surface on those pages stays visible to Assistant's docs indexer / `/api/v1/manifest` and
  can no longer drift unguarded.

- 2026-09-11: **product-map review pass** - registered the
  consent prompt surface (`product-analytics/ProductAnalyticsConsentPrompt.vue`
  static testid `product-analytics-consent-prompt`) in the `/admin/product-analytics`
  manifest `elements:` inventory - the prompt renders organisation-wide via
  `AppLayout.vue` (alongside the page's existing `consent-level` indicator) but
  had no product-map home. `test_mapped_route_elements_cover_owning_view_testids`
  now maps the route to its page view + the prompt component, so the consent
  surface cannot drift invisible to Assistant's docs indexer / `/api/v1/manifest`.
- 2026-08-27: **product-map review pass** - added this behaviour-tracker
  for the registered manifest feature `feat-product-analytics`, which previously had no
  `docs/product-map/` entry. Behaviours verified against `api/routes/product_analytics*.py`,
  `core/product_analytics/*` and the product-analytics unit/BDD/integration suites
  (`tests/integration/test_metrics_ingest.py`, `test_product_analytics_identity.py`).
  Status: covered.
