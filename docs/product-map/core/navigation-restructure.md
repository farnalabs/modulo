---
id: feat-core-navigation-restructure
prd: 8.26
delivery-tasks: []
bdd: []
unit-tests:
  - frontend/src/__tests__/components/AppLayout.spec.ts
code:
  - frontend/src/components/SidebarNav.vue
  - frontend/src/components/AppLayout.vue
  - frontend/src/composables/useSidebar.ts
  - frontend/src/config/navigation.ts
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

## Error Handling

- [ ] Malformed `manifest.yaml` causes `buildSidebarGroups()` to return empty array — sidebar renders blank
- [ ] Route references non-existent `sidebar_group` — logged as `console.warn`, item skipped
- [ ] JWT token missing `sub`, `org_role`, or `is_system_admin` claims — defaults to `""`, `null`, `false`
- [ ] `localStorage` unavailable (private browsing, quota exceeded) — `useStorage` from `@vueuse/core` catches silently, defaults used
- [ ] Plan/tier fetch fails (`planStore.fetchPlan` catches with `.catch(() => {})`) — sidebar renders all tier-gated items as not visible
- [ ] `viewMode` value corrupted in `localStorage` — `useStorage<'simple' | 'advanced'>` treats invalid value as `"simple"` (fallback in `readonly`)

## Edge Cases

- [ ] All sidebar groups hidden by tier/role — produces empty sidebar (no items in any group, filtered out by `g.items.length > 0`)
- [ ] Single group with single item — renders one expandable group containing one link
- [ ] Deep breadcrumb chain (6+ levels) — `Breadcrumb.vue` walks `parent` chain with `visited` Set to prevent infinite loops
- [ ] Route path exactly `/` — sidebar groups use `item.exact` check to avoid `path.startsWith("/")` activating all groups
- [ ] `sidebar_group` name mismatch between `manifest.yaml` route and `manifest.sidebar_groups` — logged as `console.warn`, item not rendered
- [ ] Group with matching routes removed after being cached — `_cachedGroups` returns stale data until page reload (no invalidation mechanism)
- [ ] `requiredRoles: []` (empty array) — `canSeeItem` returns `false` for all users (empty whitelist means no access)

## Security

- [ ] Nav items gated by `requiredRoles` — only users with matching `org_role` see the link
- [ ] Nav items gated by `requiredTier` — only orgs at that tier or above see the link
- [ ] Nav items gated by `requiredPermissions` — only users with at least one matching permission see the link
- [ ] System-admin-only groups (`systemAdminOnly`) — hidden from non-admin users
- [ ] `simpleMode` groups hidden in Essentials view — users cannot navigate to enterprise features without switching to All Features mode

## Known Gaps

- **SidebarSubgroup.vue not implemented** — PRD §8.26.2 specifies nested subgroups within Core (Pipelines, Runs & Evaluation, Schemas) and Admin (Access Control, Cost Management, System, Monitoring, Extensions). Currently all sidebar items render flat within parent groups. Requires a new `SidebarSubgroup.vue` component with collapsible sub-headers, indented children, and i18n support.
- **No BDD coverage** — No `.feature` files for navigation behaviours (breadcrumbs, sidebar groups, view mode toggling, page tabs). Should be added under `tests/bdd/features/navigation/`.
- **No SidebarNav unit tests** — `SidebarNav.vue` (90 lines) has no dedicated tests. `AppLayout.spec.ts` (110 lines) covers some sidebar link rendering but is limited.

## QA History

- **2026-07-12 — Round 2 improve-architecture**: Removed stale `depends-on: [feat-frontend-scaffold]` (entry does not exist in product map). Fixed unit-test path from `frontend/src/__tests__/AppLayout.spec.ts` to `frontend/src/__tests__/components/AppLayout.spec.ts`. Added Error Handling, Edge Cases, and Security sections. Verified all three Known Gaps are still accurate (SidebarSubgroup.vue does not exist, no BDD coverage, no SidebarNav unit tests). No dead code found in referenced TypeScript/Vue files.
