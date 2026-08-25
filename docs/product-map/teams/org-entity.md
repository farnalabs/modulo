---
id: feat-teams-org-entity
prd: 9.1, 6.2
delivery-tasks: []
code:
  - backend/src/modulo/db/models/organisation.py
  - backend/src/modulo/db/models/org_membership.py
  - backend/src/modulo/db/crud/organisation.py
  - backend/src/modulo/db/crud/org_membership.py
  - backend/src/modulo/db/crud/org_deletion.py
  - backend/src/modulo/db/rls.py
  - backend/src/modulo/api/routes/admin_orgs.py
  - backend/src/modulo/api/routes/admin.py
  - backend/src/modulo/api/routes/viewmodel.py
  - frontend/src/views/AdminOrgSettingsView.vue
  - frontend/src/views/AdminSystemOrgsView.vue
unit-tests:
  - backend/tests/unit/api/test_admin_orgs.py
  - backend/tests/unit/api/test_admin.py
  - backend/tests/unit/api/test_viewmodel_error_paths.py
  - backend/tests/unit/db/test_multi_backend_bdd.py
  - backend/tests/integration/crud/test_org_deletion.py
  - frontend/src/__tests__/AdminOrgSettingsView.spec.ts
bdd:
  - backend/tests/bdd/features/organisation/rls_isolation.feature
  - backend/tests/bdd/features/organisation/org_deletion.feature
  - backend/tests/bdd/features/organisation/org_scoping.feature
  - backend/tests/bdd/features/organisation/multi_backend.feature
depends-on:
  - feat-auth-jwt-auth
  - feat-core-db-abstraction-core
  - feat-core-run-context
  - feat-core-feature-flag-ui
status: covered
---

# Teams Org Entity

The Organisation entity is the root tenant entity in Modulo's multi-tenant architecture.
Every resource belongs to an organisation (route `feat-org` / `/admin/org`). Postgres
Row-Level Security (RLS) enforces tenant isolation at the database layer, and
`OrgMembership` scopes users (`admin | operator | runner | viewer`) to organisations.
Referenced by ADR 017/018 as the product-map entry updated during centralized
authorization cleanup.

## Behaviours

- [x] Organisation model: `id`, `name`, immutable `slug`, `status`
      (`active | suspended | deleted`), `created_by` (nullable, deliberately not an FK),
      `settings_json`, `plan_id`, `otel_config_json`, `daily_spend_limit`,
      `deletion_token` (24h, single-use), `export_bundle_json`
- [x] `OrgMembership`: `(account_id, organisation_id)` unique, role check constraint
      `admin | operator | runner | viewer`, `joined_at`, `deactivated_at`
- [x] System-admin org CRUD (`/api/v1/admin/orgs`): create/list, create user in org,
      delete org — 403 for non-system-admins, 409 on slug collision / duplicate
      membership, 422 on invalid slug/role/weak password, 404 when missing
- [x] Self-service org profile (`/api/v1/admin/org`): get/update name/logo, regenerate
      API key — admin role gate → 403
- [x] Org deletion workflow: `deletion-request` (202 + token + export bundle),
      `deletion-confirm` (within 24h), `deletion-cancel`, `export`, immediate `DELETE` —
      token single-use, audit event on request, terminal runs batch-deleted before FK cascade
- [x] RLS tenant isolation: org-scoped tables carry `organisation_id`, `SET LOCAL
      app.organisation_id` in transactions, pool-checkout org reset, ORM tenant filter
      for non-Postgres backends, `organisations` table itself excluded (root tenant)
- [x] License management via `/api/v1/admin/orgs/{org_id}/license` (Ed25519 verified,
      422 on invalid key, falls back to system license)
- [x] ViewModel `current` supplies org context, plan, team memberships, preferences
- [x] ProgrammingError → 501 / SQLAlchemyError → 503 on the DB-accessing org route handlers
- [x] Org-level `daily_spend_limit` enforced for the whole-org aggregate

## Known Gaps

- **Stale `created_by` FK design** — deliberately not an FK (bootstrap), so no
  referential integrity on the creator field.
- **No org-CRUD BDD feature file** — system-admin org CRUD is unit-tested only; BDD
  covers deletion/scoping/RLS/multi-backend.
- **No `organisation exists` shared BDD step** — only defined in the library feature
  conftest, not as a reusable fixture.
- **No E2E smoke test for `AdminOrgSettingsView`** — vitest coverage only.

## QA History

- 2026-08-25: **improve-architecture (product-map walk)** — restored this entry as part of
  rebuilding the `docs/product-map/` feature graph. The entry is referenced by ADR 017/018
  (centralized-authorization cleanup). Re-verified model columns, the RLS exclusion, the
  org deletion workflow, and member role constraints against the current tree. Status:
  covered.