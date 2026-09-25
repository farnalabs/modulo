---
id: feat-org
prd: N/A
adr: []
code:
  - backend/src/modulo/api/routes/org_settings.py
  - backend/src/modulo/api/routes/admin_orgs.py
  - backend/src/modulo/api/routes/admin_feature_flags.py
unit-tests:
  - backend/tests/unit/api/test_admin_orgs.py
  - backend/tests/unit/api/test_admin_orgs_coverage_gaps.py
  - backend/tests/unit/api/test_admin_feature_flags.py
bdd:
  - backend/tests/bdd/features/ui/org_settings.feature
  - backend/tests/bdd/features/triggers/pause.feature
depends-on:
  - feat-teams
status: covered
---

# Organization Settings

Organization-level settings including display currency, guardrails kill-switch
status, and admin-level org management (CRUD, license, trigger pause, guardrails
kill-switch, authorization enforcement).

## Behaviours

- [x] `GET /api/v1/org/settings` returns the org's display currency for any
      authenticated org member (`backend/src/modulo/api/routes/org_settings.py`)
- [x] `GET /api/v1/org/settings/guardrails/kill-switch` returns the kill-switch
      state (read-only for non-admins)
- [x] Admin endpoints handle org CRUD, license management, trigger pause/unpause,
      guardrails kill-switch toggle, and authorization enforcement flag
      (`backend/src/modulo/api/routes/admin_orgs.py`,
      `backend/tests/unit/api/test_admin_orgs.py`)
- [x] Org-wide trigger pause is admin-only with audit logging; toggling to the
      current state is an idempotent no-op
      (`backend/tests/bdd/features/triggers/pause.feature`)
- [x] Org feature-flag overrides are governance-audited: every set/clear/toggle
      of an org `feature_overrides` entry appends a tamper-evident audit event
      to the org chain (`feature_flag_override_set` / `feature_flag_override_cleared`,
      `resource_type=org`, payload `{flag_name, enabled}`) through
      `append_audit_event_isolated`, fail-open so a broken append never rolls back
      the already-committed override
      (`backend/src/modulo/api/routes/admin_feature_flags.py`,
      `backend/tests/unit/api/test_admin_feature_flags.py`)
- [x] Frontend renders org settings at `/admin/org` with org delete confirmation
      and product-analytics toggle (`frontend/src/manifest.yaml` testids)

## Known Gaps

- BDD scenarios for org settings UI are tagged `@awaiting-implementation` (UI not
  yet built for the org-settings route).

## QA History
- 2026-09-25: **Improve Architecture product-map walk** —
  closed the feat-org "governance and audit partially wired" gap: the admin
  feature-flag endpoints (`PUT /{flag}` toggle, `PUT`/`DELETE /{flag}/org-override`)
  now emit `feature_flag_override_set` / `feature_flag_override_cleared` events on
  the org's tamper-evident audit chain via `append_audit_event_isolated` (fail-open;
  a broken append never rolls back the committed override). Manifest `feat-org`
  status moved partial → covered; event payloads assert
  `{flag_name, enabled}` with `resource_type=org`. Unit tests
  `TestOrgFlagOverrideAudit` in `test_admin_feature_flags.py` pin the events and the
  fail-open contract.

- 2026-09-12: **product-map review pass** — registered the shared
  plan-entitlement gate surface (`components/FeatureGate.vue` + `LockIcon.vue` static
  testids `feature-gate*` / `lock-icon`) in the manifest `elements:` inventory for `/admin/feature-flags`, `/admin/org`
  and wired the two components into the reverse testid-coverage guard
  (`test_mapped_route_elements_cover_owning_view_testids`), so the entitlement-card
  surface on those pages stays visible to Assistant's docs indexer / `/api/v1/manifest` and
  can no longer drift unguarded.

- 2026-09-11: **product-map review pass** — extended the reverse
  testid-coverage guard (`test_mapped_route_elements_cover_owning_view_testids`) to
  `/admin/org`: the whole-page view(s) `AdminOrgSettingsView.vue` render static `data-testid`s that the
  product map `elements:` inventory already documents, but the surface was not yet
  guarded against drift. The route now maps to its owning view so a newly shipped
  testid can no longer silently stay invisible to Assistant's docs indexer /
  `/api/v1/manifest`.

- 2026-09-11: **product-map review pass** — registered the shared
  search-bar surface (`components/shared/FilterBar.vue` static testids
  `filter-bar-search` / `filter-bar-search-wrapper`) in the `/admin/feature-flags` manifest
  `elements:` inventory and wired the component into the reverse testid-coverage
  guard, so the feature-flag search control the page ships stays visible to Assistant's
  docs indexer and `/api/v1/manifest`.

- 2026-09-07: **product-map review pass** — added this
  behaviour-tracker for `feat-org`, which previously had no `docs/product-map/`
  entry. Behaviours verified against `routes/org_settings.py`,
  `routes/admin_orgs.py`, and the admin org unit tests. Status: covered.
