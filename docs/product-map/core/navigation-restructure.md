---
id: feat-core-navigation-restructure
prd: 8.26
delivery-tasks: []
bdd: []
unit-tests:
  - frontend/src/__tests__/SidebarNav.spec.ts
  - frontend/src/__tests__/AppLayout.spec.ts
code:
  - frontend/src/components/layout/SidebarNav.vue
  - frontend/src/components/layout/AppLayout.vue
  - frontend/src/composables/useSidebar.ts
  - frontend/src/config/navigation.ts
depends-on: []
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
- [ ] Breadcrumbs on all pages
- [ ] Sub-navigation within page domains (hierarchy)
