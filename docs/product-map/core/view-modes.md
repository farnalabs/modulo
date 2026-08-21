---
id: feat-core-view-modes
prd: 8.21
delivery-tasks: []
bdd:
  - backend/tests/bdd/features/views/views.feature
unit-tests:
  - backend/tests/unit/api/test_view_endpoint.py
  - backend/tests/unit/api/test_viewmodel_endpoint.py
  - backend/tests/unit/api/test_viewmodel_view.py
  - backend/tests/unit/api/test_viewmodel_license.py
  - backend/tests/unit/api/test_viewmodel_error_paths.py
code:
  - backend/src/modulo/api/routes/views.py
  - backend/src/modulo/api/routes/viewmodel.py
  - backend/src/modulo/db/models/view.py
  - frontend/src/composables/useSidebar.ts
  - frontend/src/components/SidebarNav.vue
  - frontend/src/components/AppLayout.vue
  - frontend/src/components/ViewToggle.vue
depends-on:
  - feat-auth-jwt-auth
  - feat-core-feature-flag-ui
status: partial
---

# View Modes (Team)

The UI supports Simple and Advanced view modes toggled via the sidebar. Simple mode hides advanced sidebar groups.

> **Reality check (2026-08-15):** the PRD §8.21 Simple/Advanced toggle system is NOT built. What ships is **Saved Views** — org-scoped persisted run-list/pipeline-list/audit-log filter configurations with full CRUD at `/api/v1/views`, an admin management page (`AdminViewsView.vue`), and a `ViewToggle` component (FeatureGate `saved_views`). The sidebar collapse/group-expansion preferences ARE persisted to localStorage, but there is no `viewMode` ref (`simple`/`advanced`) and no `simple_mode` sidebar-group gating. The unchecked boxes below mark the genuine gaps against PRD §8.21.

## Behaviours

### Saved Views Model (backend)

- [x] `SavedView` model with name, description, view_type, filters JSON, columns, sort_by, account_id
- [x] Full CRUD at /api/v1/views
- [x] Org-scoped via OrgScoped base

### Frontend View Mode Toggle

- [ ] `useSidebar` composable `viewMode` ref (simple | advanced) — NOT built: the type `ViewMode = 'simple' | 'advanced'` is exported but no `viewMode` ref or toggle exists; the shipped component is `ViewToggle.vue` (a saved-views selector gated by the `saved_views` feature flag)
- [ ] Simple/Advanced view mode persisted to localStorage — NOT built: only `sidebar-group-prefs` and `sidebar-collapsed` are persisted
- [ ] Simple mode hides sidebar groups without `simpleMode` flag — NOT built: no `simple_mode` flag exists on manifest sidebar groups and no such gating logic exists
- [x] Team-gating (`view_modes` feature flag) — `view_modes` constant registered (`feature_flags.py:129`), all 6 `/api/v1/views` routes gated via `require_feature("view_modes")`, and `AdminViewsView.vue` wraps in `<FeatureGate feature-name="view_modes" required-tier="team">`; backend gate now tested (`TestFeatureGate` — 402 when disabled)
- [ ] Admin-customisable views (assign views to users/teams/roles) — NOT built: no `view_assignments` table, no assignment endpoints, no `/settings/view-modes` admin UI
- [ ] Default Simple/Advanced seed on first setup — NOT built: no seeding of default views

## Error Handling

- [x] `views.py` catches `ProgrammingError` → 501 with migration hint
- [x] `views.py` catches `SQLAlchemyError` → 503
- [x] `views.py` catches `HTTPException` → re-raise
- [x] `views.py` catches `Exception` → 500 with `logger.exception`
- [x] `asyncio.CancelledError` is explicitly re-raised before `except Exception` in all 6 route handlers (fixed 2026-08-15; verified by `TestCancelledErrorPropagation`)
- [ ] No fallback when `useSidebar` composable can't read/write localStorage (private browsing, quota exceeded)

## Edge Cases

- [x] No saved views for org returns empty list (not 404)
- [x] Sidebar collapse/group-expansion preferences survive page refresh (localStorage persistence via `useStorage`) — note: this is sidebar state, not a Simple/Advanced mode
- [ ] Simple mode hides advanced sidebar groups correctly — NOT built (no Simple/Advanced mode)
- [x] Empty view name on create returns 422
- [x] Tier gate enforced on the backend — `view_modes` disabled returns 402 on every `/api/v1/views` route (`TestFeatureGate`)
- [x] View CRUD routes re-raise `asyncio.CancelledError` instead of wrapping it as 500 (`TestCancelledErrorPropagation`)

## Security

- [x] Saved views CRUD requires authentication (401 for unauthenticated)
- [x] Views are org-scoped — cross-org access returns 404 (RLS via `set_rls_org`)
- [x] View CRUD role gates verified in tests — `view.list` is viewer-level (viewer can list, 200) and `view.manage` is operator-level (viewer create → 403); `TestRoleGating`
- [ ] No Simple/Advanced view-mode toggle exists, so the "localStorage-only, no CSRF vector" claim is not applicable to the shipped feature

## QA History

### 2026-07-12 — Round 3 QA

- **Fixed (MINOR):** No stale frontmatter or resolved gaps found. All `code:`, `bdd:`, `unit-tests:` entries verified as accurate.
- **Finding (MINOR):** View CRUD routes (`views.py`) handle `ProgrammingError→501`, `SQLAlchemyError→503`, `HTTPException→re-raise`, and `Exception→500`, but are missing explicit `asyncio.CancelledError` guard. In practice, `except Exception` catches `CancelledError` on Python < 3.12 and wraps it as a misleading 500. Consider adding `except asyncio.CancelledError: raise` as the first exception handler.

### 2026-08-15 — distribute (partial→covered sweep)

- **Implemented (`asyncio.CancelledError` guards):** all 6 `views.py` route handlers now re-raise `asyncio.CancelledError` before the `except Exception` catch (resolves the 2026-07-12 MINOR finding). Verified by `TestCancelledErrorPropagation` (list/create direct calls).
- **Marked [x] — feature-gate enforcement:** added `TestFeatureGate` proving a disabled `view_modes` flag returns 402 on every `/api/v1/views` route. The `view_modes` constant, backend `require_feature` gates, and the `AdminViewsView.vue` FeatureGate make the tier gate real and now tested.
- **Marked [x] — role gates verified:** added `TestRoleGating` — a viewer can list saved views (200) but cannot create (403), matching `view.list` (viewer) vs `view.manage` (operator).
- **Corrected the frontend section:** the "`useSidebar` `viewMode` ref", "view mode persisted to localStorage", and "Simple mode hides sidebar groups" checkboxes were inaccurate — no Simple/Advanced mode exists in the codebase (only sidebar collapse/group prefs and the Saved Views feature). Marked unchecked with notes; PRD §8.21's Simple/Advanced system remains a genuine gap alongside admin-customisable views and default seeding.
