---
id: feat-teams-org-entity
prd: 9.1, 6.2
delivery-tasks: []
bdd:
  - backend/tests/bdd/features/organisation/rls_isolation.feature
  - backend/tests/bdd/features/organisation/org_deletion.feature
  - backend/tests/bdd/features/organisation/org_scoping.feature
  - backend/tests/bdd/features/organisation/multi_backend.feature
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
  - backend/src/modulo/db/migrations/versions/0001_initial_schema.py
  - backend/src/modulo/db/migrations/versions/0002_rls_policies.py
  - backend/src/modulo/db/migrations/versions/0015_org_deletion.py
  - frontend/src/views/AdminOrgSettingsView.vue
unit-tests:
  - backend/tests/unit/api/test_admin_orgs.py
  - backend/tests/unit/api/test_admin.py
  - backend/tests/unit/api/test_org_deletion_bdd.py
  - backend/tests/unit/api/test_viewmodel_error_paths.py
  - backend/tests/unit/db/test_multi_backend_bdd.py
  - backend/tests/integration/crud/test_org_deletion.py
  - backend/tests/unit/api/test_org_programming_error.py
  - frontend/src/__tests__/AdminOrgSettingsView.spec.ts
depends-on:
  - feat-auth-jwt-auth
  - feat-core-db-abstraction-core
  - feat-core-run-context
  - feat-core-feature-flag-ui
status: partial
---

# Teams Org Entity

The Organisation entity is the root tenant entity in Modulo's multi-tenant architecture. Every resource belongs to an organisation. Postgres Row-Level Security (RLS) enforces tenant isolation at the database layer.

## Organisation Entity Model

- [x] `id` (UUID PK, auto-generated)
- [x] `name` (varchar 255, required)
- [x] `slug` (varchar 63, URL-safe `[a-z0-9-]+`, unique, immutable once set)
- [x] `status` (enum: `active` | `suspended` | `deleted`, default `active`)
- [x] `created_at` (DateTime, auto-set)
- [x] `deleted_at` (nullable DateTime, set on deletion)
- [x] `created_by` (nullable UUID — deliberately NOT an FK because the first org must exist before the first user)
- [x] `settings_json` (JSON, default `{}`, stores logo_url, license_key, and other config)
- [x] `plan_id` (nullable varchar 255, managed exclusively by modulo-cloud)
- [x] `otel_config_json` (JSON, default `{}` — OpenTelemetry configuration per organisation)
- [x] `daily_spend_limit` (nullable Numeric 14,6)
- [x] `deletion_token` (nullable varchar 128, set during deletion workflow)
- [x] `deletion_token_expires_at` (nullable DateTime, 24h window)
- [x] `export_bundle_json` (nullable JSON, cached export data)
- [x] Check constraint: `status IN ('active', 'suspended', 'deleted')`

## OrgMembership Model

- [x] User-to-organisation membership via `OrgMembership` entity
- [x] `account_id` UUID FK → accounts.id (CASCADE on delete)
- [x] `organisation_id` UUID FK → organisations.id (via OrgScoped)
- [x] `role` enum: `owner` | `admin` | `operator` | `runner` | `viewer`
- [x] Unique constraint: `(account_id, organisation_id)`
- [x] `joined_at` (DateTime, auto-set)
- [x] `deactivated_at` (nullable DateTime)

## System Admin Org CRUD (admin_orgs.py)

- [x] `POST /api/v1/admin/orgs` — Create org (system admin only, validates slug uniqueness, 409 on collision)
- [x] `GET /api/v1/admin/orgs` — List all orgs (system admin only)
- [x] `POST /api/v1/admin/orgs/{org_id}/users` — Create user in org (system admin only, validates password strength, prevents duplicate memberships)
- [x] `DELETE /api/v1/admin/orgs/{org_id}` — Delete org (system admin only, 404 if not found)
- [x] Slug validation: 3-63 chars, `^[a-z0-9-]+$` pattern
- [x] Slug collision → 409 Conflict
- [x] System admin gate → 403 Forbidden for non-system-admins

## Org Profile (Admin Self-Service)

- [x] `GET /api/v1/admin/org` — Get current org profile (admin role required)
- [x] `PUT /api/v1/admin/org` — Update org name/logo_url (admin role required)
- [x] `POST /api/v1/admin/org/regenerate-api-key` — Regenerate default org API key
- [x] Admin role gate → 403 for non-admin users
- [x] Org not found → 404

## Org Deletion Workflow

- [x] `POST /api/v1/admin/org/deletion-request` — Initiate soft-delete (202 + token + export bundle)
- [x] `POST /api/v1/admin/org/deletion-confirm` — Confirm with token within 24h window
- [x] `PATCH /api/v1/admin/org/deletion-cancel` — Cancel pending deletion (restores status to active)
- [x] `GET /api/v1/admin/org/export` — Export org data as JSON bundle
- [x] `DELETE /api/v1/admin/org` — Immediate hard delete (skip token workflow)
- [x] Deletion token is 64-char URL-safe string, expires in 24 hours
- [x] Export bundle captures: org metadata, memberships, pipelines, runs, audit events, library primitives, connector instances, model backends
- [x] Audit event written on deletion request (`org_deletion_requested`)
- [x] Cascade delete removes all org-scoped resources
- [x] Old terminal runs batch-deleted before FK cascade
- [x] `_require_org_admin` gate: system_admin OR org_role in (admin, owner) → 403 otherwise
- [x] Already-deleted org → 409 on new deletion request
- [x] Invalid/expired token → 409 on confirm
- [x] No pending deletion → 409 on cancel

## RLS / Multi-Tenant Scoping

- [x] All org-scoped tables carry `organisation_id` (via `OrgScoped` mixin)
- [x] Postgres RLS policy `rls_org_isolation` on 20+ tables (migration 0002)
- [x] `SET LOCAL app.organisation_id` inside transactions (safe with connection pooling)
- [x] Pool-checkout hook resets org context on every connection reuse
- [x] ORM-level tenant filter for non-Postgres backends (`register_tenant_filter`)
- [x] RLS explicitly NOT applied to the `organisations` table itself (root tenant)
- [x] Cross-org queries return empty (no data leak between tenants)
- [x] Advisory locks respect org scope
- [x] Multi-backend test coverage for tenant filtering (SQLite, MariaDB, Postgres)

## License Management

- [x] `GET /api/v1/admin/orgs/{org_id}/license` — Get org license (system-admin or own-org admin)
- [x] `PUT /api/v1/admin/orgs/{org_id}/license` — Set org license key (system-admin or own-org admin)
- [x] `DELETE /api/v1/admin/orgs/{org_id}/license` — Remove org license key
- [x] License key verified via `parse_and_verify()` (Ed25519 signed payload)
- [x] Invalid license key → 422 Unprocessable Entity
- [x] Falls back to system-level license if no org-specific key set
- [x] Org not found → 404

## Error Handling

- [x] System admin → 403 on non-admin users for system-level org operations
- [x] Admin role → 403 for org profile management
- [x] Org not found → 404 on all CRUD and profile operations
- [x] Slug collision → 409 Conflict on create
- [x] Duplicate membership → 409 Conflict on create_user
- [x] Invalid role → 422 on create_user
- [x] Weak password → 422 on create_user
- [x] Deletion already in progress → 409
- [x] Invalid/expired deletion token → 409
- [x] No pending deletion → 409 on cancel
- [x] Invalid license key → 422
- [x] ProgrammingError → 501 on all 15 DB-accessing route handlers
- [x] ViewModel current: missing org → 400, missing account → 404, team not found → 404

## Edge Cases

- [ ] `created_by` UUID is nullable and NOT an FK — first org must be created with a null `created_by` since no user exists yet
- [ ] `slug` is immutable once set — no PATCH endpoint to change slug
- [ ] Deletion token is single-use — confirm or cancel invalidates it
- [ ] Export bundle is cached in `export_bundle_json` after deletion request
- [ ] Cascade delete skips `organisations` table itself (root entity — RLS doesn't apply)
- [ ] Runs with terminal status are batch-deleted before FK cascade to avoid deadlocks
- [ ] Billing overview endpoint aggregates across org (users, teams, pipelines, runs)
- [ ] Org-level daily spend limit enforced for the entire org aggregate

## Known Gaps

- **Stale `created_by` FK design**: `created_by` is deliberately not an FK to allow bootstrap, but this means no referential integrity on the creator field. A deleted user's UUID remains in `created_by` indefinitely.
- **No org CRUD BDD feature file**: There is no `.feature` file testing the system admin org CRUD endpoints (`POST/GET/DELETE /api/v1/admin/orgs`). Only deletion workflow, scoping, and RLS have BDD coverage.
- **Integration tests skipped**: All tests in `backend/tests/integration/crud/test_org_deletion.py` (14 tests) are marked `@pytest.mark.skip(reason="awaiting-implementation")` due to schema alignment issues.
- **No `organisation exists` shared BDD step**: The step is only defined in library feature tests' conftest, not as a reusable fixture.
- **No system admin orgs frontend page**: The nav link at `/admin/system/orgs` exists but no corresponding view is implemented.
- **ProgrammingError→501 test coverage**: 15 new tests added in `test_org_programming_error.py` — these cover all route handlers but use mocking, not real DB interaction.
- **No team-scoped org role cap BDD**: The PRD describes that an org-level `viewer` role cannot be elevated by a team-level `operator` role, but this isn't tested in any org-specific `.feature` file (tested in `rbac.feature` instead).
- **RLS on organisations table**: Deliberately excluded from RLS. A system-admin bypass could enumerate org names. Acceptable by design.
- **No `modulo-cloud` integration**: The modulo-cloud service layer (§6.2) is V3-deferred. Organisation lifecycle, plan enforcement, and subdomain routing are stubs.
- **No Frontend smoke test for AdminOrgSettingsView**: The view has vitest tests but no Playwright E2E coverage.
- **Website docs**: No docs page exists at Website/modulo-website/src/docs/organisation.md (stub created).
