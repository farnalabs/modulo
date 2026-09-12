---
id: feat-system-orgs
prd: N/A
adr: []
code:
  - backend/src/modulo/api/routes/admin_orgs.py
unit-tests:
  - backend/tests/unit/api/test_admin_orgs.py
  - backend/tests/unit/api/test_admin_orgs_coverage_gaps.py
bdd:
  - backend/tests/bdd/features/system_admin/system_admin_orgs.feature
  - backend/tests/bdd/features/system_admin/system_admin_users.feature
depends-on:
  - feat-teams
status: covered
---

# System Orgs

System-level organization administration for cross-tenant org management. System
admins can create, list, and delete organisations; create users within orgs; and
manage org-level licenses.

## Behaviours

- [x] POST `/api/v1/admin/orgs` creates a new org with slug uniqueness check and
      cost-component seeding; duplicate slug returns 409
      (`backend/tests/bdd/features/system_admin/system_admin_orgs.feature`)
- [x] GET `/api/v1/admin/orgs` lists all orgs (hides the nil-UUID orphan org)
- [x] POST `/api/v1/admin/orgs/{org_id}/users` creates a user within an org with
      cross-tenant account takeover protection and password validation
      (`backend/tests/bdd/features/system_admin/system_admin_users.feature`)
- [x] DELETE `/api/v1/admin/orgs/{org_id}` deletes an organisation
- [x] Org-level license management: GET/PUT/DELETE on
      `/api/v1/admin/orgs/{org_id}/license` with validation via
      `parse_and_verify` (`admin_orgs.py`)
- [x] Regular admin receives 403 on org creation and user creation in other orgs
      (`system_admin_orgs.feature`, `system_admin_users.feature`)

## Known Gaps

- No BDD for DELETE org or license management; coverage is via unit tests.

## QA History

- 2026-09-12: **improve-architecture (product-map walk)** — extended the reverse
  testid-coverage guard (`test_mapped_route_elements_cover_owning_view_testids`) to
  `/admin/system/orgs`: the whole-page view `AdminSystemOrgsView.vue` now maps to its
  owning view so a newly shipped testid on the system-orgs page can no longer silently
  stay invisible to Remy's docs indexer / `/api/v1/manifest`.

- 2026-09-07: **improve-architecture (product-map walk)** — added this
  behaviour-tracker for `feat-system-orgs`, which previously had no
  `docs/product-map/` entry. Behaviours verified against
  `routes/admin_orgs.py`, the admin orgs unit tests, and the system-admin BDD
  features. Status: covered.
