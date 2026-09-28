---
id: feat-org
prd: N/A
adr: []
code:
  - backend/src/modulo/api/routes/org_settings.py
  - backend/src/modulo/api/routes/admin_orgs.py
  - backend/src/modulo/api/routes/admin_feature_flags.py
  - backend/src/modulo/api/routes/admin.py
unit-tests:
  - backend/tests/unit/api/test_admin_orgs.py
  - backend/tests/unit/api/test_admin_orgs_coverage_gaps.py
  - backend/tests/unit/api/test_admin_feature_flags.py
  - backend/tests/unit/api/test_admin.py
bdd:
  - backend/tests/bdd/features/triggers/pause.feature
  - backend/tests/bdd/features/system_admin/system_admin_users.feature
  - backend/tests/bdd/features/auth/api_keys.feature
  - backend/tests/bdd/features/viewmodel/viewmodel_current.feature
  - backend/tests/bdd/features/organisation/org_deletion.feature
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
- [x] Self-service org profile (`GET`/`PUT /api/v1/admin/org` in
      `api/routes/admin.py`): admins read and rename the org (slug immutable —
      `test_update_org_ignores_slug_changes`), delete it immediately, and
      regenerate the org API key (`POST /org/regenerate-api-key`); every
      endpoint is admin-gated (operator/viewer → 403,
      `backend/tests/unit/api/test_admin.py`)
- [x] Member invites into the org are BDD-executed against
      `POST /api/v1/admin/orgs/{org_id}/users` (`system_admin_users.feature`:
      duplicate-membership 409, cross-tenant local-account 409, invalid role /
      weak password 422, missing org 404)
- [x] API-key revocation and the non-admin 403 (admin-only minting) are
      BDD-executed (`auth/api_keys.feature`: create 201, non-admin 403, revoke,
      active-key-only listing, invalid-key rejection)
- [x] Viewer access denial is BDD-executed at the org-context boundary
      (`viewmodel/viewmodel_current.feature`: a viewer org role is refused)

## Known Gaps

None acknowledged: the org-management flows ship on the real REST surfaces cited
above. The stale `ui/org_settings.feature` UI-journey drafts (view page, rename,
invite, revoke, viewer-denial) were archived in the 2026-09-28 Improve
Architecture walk — none of the eleven `data-testid`s they referenced
(`org-name-input`, `member-list`, `add-member-button`, `invite-member-form`,
`api-key-row`, `revoke-api-key`, `api-key-status`, etc.) exists anywhere in the
frontend, so the scenarios described a page that never shipped and could never
execute (they stayed pinned `@awaiting-implementation`). Each flow's real
behaviour is covered by the citations above.

## QA History
- 2026-09-28: **Improve Architecture product-map walk** —
  closed the lingering `ui/org_settings.feature` gap. Archived the five
  never-executing UI-journey drafts (verified: 0/11 referenced testids exist in
  `frontend/src`; `/admin/org` ships a different surface — org profile, data
  export, product-analytics + community-objects toggles, delete confirmation)
  and re-anchored `feat-org` to the real org-management coverage: self-service
  org profile read/rename/delete + admin role gate (unit, `test_admin.py`),
  member invites (`system_admin_users.feature`), API-key create/revoke +
  non-admin 403 (`auth/api_keys.feature`), viewer denial
  (`viewmodel_current.feature`), and org deletion BDD
  (`organisation/org_deletion.feature`). Removed the pinned scenarios from
  `PINNED_AWAITING_IMPLEMENTATION` and the manifest deferral. The manifest
  `feat-org` registry now has no deferral; the tracker's previous claim that the
  drafts were "exercised by component (vitest) and E2E suites" was stale.
- 2026-09-27: **Improve Architecture product-map walk** —
  reconciled the stale "org settings UI not yet built" Known Gap: the
  `/admin/org` UI ships (`AdminOrgSettingsView.vue`, org delete confirmation +
  product-analytics / community-objects toggles + the product-analytics error
  strip) and its surface is guarded in the manifest; the remaining gap was
  re-scoped to the `@awaiting-implementation` UI-journey BDD scenarios, which
  describe frontend flows covered by component + E2E suites rather than backend
  BDD. Manifest `feat-org` gained the shipped UI behaviour line and the scoped
  deferral.
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
