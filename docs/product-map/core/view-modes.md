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
  - frontend/src/components/ViewModeToggle.vue
depends-on:
  - feat-auth-jwt-auth
  - feat-core-feature-flag-ui
status: partial
---

# View Modes (Team)

The UI supports Simple and Advanced view modes toggled via the sidebar. Simple mode hides advanced sidebar groups.

## Behaviours

### Saved Views Model (backend)

- [x] `SavedView` model with name, description, view_type, filters JSON, columns, sort_by, account_id
- [x] Full CRUD at /api/v1/views
- [x] Org-scoped via OrgScoped base

### Frontend View Mode Toggle

- [x] `useSidebar` composable with `viewMode` ref (simple | advanced)
- [x] View mode persisted to localStorage
- [x] Simple mode hides sidebar groups without `simpleMode` flag
- [ ] Team-gating (`view_modes` feature flag) — feature flag constant exists but tier enforcement not end-to-end tested
- [ ] Admin-customisable views (assign views to users/teams/roles)
- [ ] Default Simple/Advanced seed on first setup

## Error Handling

- [x] `views.py` catches `ProgrammingError` → 501 with migration hint
- [x] `views.py` catches `SQLAlchemyError` → 503
- [x] `views.py` catches `HTTPException` → re-raise
- [x] `views.py` catches `Exception` → 500 with `logger.exception`
- [x] View mode toggle persists to localStorage — no server-side dependency for basic toggle
- [ ] No fallback when `useSidebar` composable can't read/write localStorage (private browsing, quota exceeded)

## Edge Cases

- [x] No saved views for org returns empty list (not 404)
- [x] View mode preference survives page refresh (localStorage persistence)
- [x] Simple mode hides advanced sidebar groups correctly
- [x] Empty view name on create returns 422
- [ ] Tier gate not enforced end-to-end — feature flag constant exists but `view_modes` gate not verified
- [ ] View CRUD routes missing `asyncio.CancelledError` guard — `except Exception` catches it as misleading 500 on Python < 3.12

## Security

- [x] Saved views CRUD requires authentication (401 for unauthenticated)
- [x] Views are org-scoped — cross-org access returns 404
- [x] View mode toggle is localStorage-only — no CSRF vector
- [ ] Admin-only routes for view CRUD not verified in tests

## QA History

### 2026-07-12 — Round 3 QA

- **Fixed (MINOR):** No stale frontmatter or resolved gaps found. All `code:`, `bdd:`, `unit-tests:` entries verified as accurate.
- **Finding (MINOR):** View CRUD routes (`views.py`) handle `ProgrammingError→501`, `SQLAlchemyError→503`, `HTTPException→re-raise`, and `Exception→500`, but are missing explicit `asyncio.CancelledError` guard. In practice, `except Exception` catches `CancelledError` on Python < 3.12 and wraps it as a misleading 500. Consider adding `except asyncio.CancelledError: raise` as the first exception handler.
