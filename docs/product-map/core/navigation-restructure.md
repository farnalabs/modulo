---
id: feat-core-navigation-restructure
prd: 8.26
delivery-tasks: []
bdd: []
unit-tests:
  - frontend/src/__tests__/AppLayout.spec.ts
code:
  - frontend/src/components/SidebarNav.vue
  - frontend/src/components/AppLayout.vue
  - frontend/src/composables/useSidebar.ts
  - frontend/src/config/navigation.ts
depends-on: [feat-frontend-scaffold]
status: partial
---

# Navigation Restructure (Frontend UX)

The frontend navigation system with sidebar groups, collapsible sections, breadcrumbs, and view mode toggling.

## Behaviours

- [x] Sidebar with 11 collapsible groups (Core, Pipelines, Runs, Schemas, Remy, Settings, Access Control, Cost Management, System, Monitoring, Extensions)
- [x] Expand/collapse per group with chevron indicators
- [x] Simple/Advanced view mode toggle
- [x] "Show all" reveals hidden groups
- [x] Bottom bar: user profile, team, dark mode toggle, sign out
- [x] Breadcrumbs on all pages
- [x] Sub-navigation within page domains (hierarchy)

## Known Gaps

- **SidebarSubgroup.vue not implemented** — PRD §8.26.2 specifies nested subgroups within Core (Pipelines, Runs & Evaluation, Schemas) and Admin (Access Control, Cost Management, System, Monitoring, Extensions). Currently all sidebar items render flat within parent groups. Requires a new `SidebarSubgroup.vue` component with collapsible sub-headers, indented children, and i18n support.
- **No BDD coverage** — No `.feature` files for navigation behaviours (breadcrumbs, sidebar groups, view mode toggling, page tabs). Should be added under `tests/bdd/features/navigation/`.
- **No SidebarNav unit tests** — `SidebarNav.vue` (90 lines) has no dedicated tests. `AppLayout.spec.ts` (110 lines) covers some sidebar link rendering but is limited.
