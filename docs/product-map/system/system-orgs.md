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
- [x] DELETE `/api/v1/admin/orgs/{org_id}` deletes an organisation; returns 404
      if not found and 403 for a regular admin
      (`backend/tests/bdd/features/system_admin/system_admin_orgs.feature`)
- [x] Org-level license management: GET/PUT/DELETE on
      `/api/v1/admin/orgs/{org_id}/license` with validation via
      `parse_and_verify` (`admin_orgs.py`)
- [x] GET `/api/v1/admin/orgs/{org_id}/license` resolves the effective org
      license — the org's own `settings_json.license_key` when it verifies,
      otherwise the system license; `has_license=false` only when neither
      exists, and a missing org is 404. BDD: system_admin_orgs.feature
      (org-key resolution, system fallback, invalid-stored-key fallback,
      missing org)
- [x] PUT `/api/v1/admin/orgs/{org_id}/license` verifies the key via
      `parse_and_verify` (invalid key 422; missing org 404 before verification),
      then writes it in one FOR-UPDATE transaction preserving pre-existing
      settings keys. BDD: system_admin_orgs.feature
- [x] DELETE `/api/v1/admin/orgs/{org_id}/license` clears the org key in the
      same locked read-modify-write and returns `has_license=false`; a missing
      org is 404. BDD: system_admin_orgs.feature
- [x] License endpoints are role-gated via `require_target_org_role`:
      GET needs `org.license.view` (operator), PUT/DELETE need
      `org.license.manage` (admin); a regular org admin without the live role
      receives 403 on all three. BDD: system_admin_orgs.feature
- [x] Regular admin receives 403 on org creation and user creation in other orgs
      (`system_admin_orgs.feature`, `system_admin_users.feature`)

## QA History
- 2026-09-17: **improve-architecture (product-map walk)** — closed the "No BDD
  for org-level license management" gap: added 10 license scenarios to
  `system_admin_orgs.feature` (GET org-key resolution / system fallback /
  invalid-stored-key fallback / missing-org 404; PUT valid-200 + invalid-422 +
  missing-org 404; DELETE clears key + missing-org 404; and regular-admin 403
  across GET/PUT/DELETE), driving the real `require_target_org_role` gate and
  `admin_orgs._resolve_org_license` / `_verify_license_key` / locked
  read-modify-write. Removed the tracked known gap.
  Status: covered with no remaining known gaps.

- 2026-09-17: **improve-architecture (product-map walk)** — closed the "No BDD
  for DELETE org" half of the `feat-system-orgs` BDD gap: added DELETE coverage
  to `system_admin_orgs.feature` (successful 204 delete, 404 for a missing org,
  and 403 for a regular org admin). The narrower license-management BDD gap was
  subsequently closed by the 2026-09-17 license walk above.

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
