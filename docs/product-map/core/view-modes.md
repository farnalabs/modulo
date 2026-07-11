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
  - feat-core-auth
  - feat-feature-flags
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
