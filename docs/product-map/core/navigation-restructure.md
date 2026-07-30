---
id: feat-BUILD-navigation-restructure
prd: 8.26
delivery-tasks: []
bdd: []
unit-tests:
  - frontend/src/__tests__/components/AppLayout.spec.ts
  - frontend/src/__tests__/navigation.spec.ts
code:
  - frontend/src/components/SidebarNav.vue
  - frontend/src/components/SidebarGroup.vue
  - frontend/src/components/SidebarLink.vue
  - frontend/src/components/AppLayout.vue
  - frontend/src/composables/useSidebar.ts
  - frontend/src/config/navigation.ts
  - frontend/src/manifest.yaml
depends-on: []status: partial
---

# Navigation Restructure (Frontend UX)

The frontend navigation system with sidebar groups, collapsible sections, expandable sub-items, and command palette search.

## Architecture Decision

See [ADR 017](../../adr/017-navigation-sidebar-restructure.md) for full rationale, UX research citations, and migration map.

## Behaviours

- [x] Sidebar with 4 groups (BUILD, MONITOR, CONFIGURE, ADMIN)
- [x] BUILD and MONITOR expanded by default (daily drivers)
- [x] CONFIGURE and ADMIN collapsed by default (set-and-forget pages)
- [x] Expand/collapse per group with chevron indicators, persisted to localStorage
- [x] Expandable sub-items within groups (chevron on parent item shows children inline)
- [x] Items without children navigate directly on click (one-click destinations)
- [x] Bottom bar: user profile, team, dark mode toggle, sign out
- [x] Search icon in header (Notification Bell area) opens Command Palette
- [x] Command Palette (Cmd+K / Ctrl+K) indexes all sidebar items and extras
- [x] Breadcrumbs on all pages
- [x] Sub-navigation within page domains via PageTabs (horizontal pill tabs)
- [x] Permission gating per nav item (requiredRoles, requiredTier, requiredPermissions)
- [x] Visibility gating per nav item (public, public_preview, private_preview, in_dev)

## Error Handling

- [ ] Malformed `manifest.yaml` causes `buildSidebarGroups()` to return empty array â€” sidebar renders blank
- [ ] Route references non-existent `sidebar_group` â€” logged as `console.warn`, item skipped
- [ ] JWT token missing `sub`, `org_role`, or `is_system_admin` claims â€” defaults to `""`, `null`, `false`
- [ ] `localStorage` unavailable (private browsing, quota exceeded) â€” `useStorage` from `@vueuse/BUILD` catches silently, defaults used
- [ ] Plan/tier fetch fails (`planStore.fetchPlan` catches with `.catch(() => {})`) â€” sidebar renders all tier-gated items as not visible
- [ ] Expandable sub-item path does not exist in manifest â€” `subItemConfig` entry points to orphaned route

## Edge Cases

- [ ] All sidebar groups hidden by tier/role â€” produces empty sidebar (no items in any group, filtered out by `g.items.length > 0`)
- [ ] Single group with single item â€” renders one expandable group containing one link
- [ ] Deep breadcrumb chain (6+ levels) â€” `Breadcrumb.vue` walks `parent` chain with `visited` Set to prevent infinite loops
- [ ] Route path exactly `/` â€” sidebar groups use `item.exact` check to avoid `path.startsWith("/")` activating all groups
- [ ] `sidebar_group` name mismatch between `manifest.yaml` route and `manifest.sidebar_groups` â€” logged as `console.warn`, item not rendered
- [ ] Group with matching routes removed after being cached â€” `_cachedGroups` returns stale data until page reload (no invalidation mechanism)
- [ ] `requiredRoles: []` (empty array) â€” `canSeeItem` returns `false` for all users (empty whitelist means no access)
- [ ] Expandable sub-item with zero children â€” rendered as regular nav item (no expand chevron shown)
- [ ] All children of an expandable item hidden by permissions â€” parent renders without expand chevron (no children visible)

## Security

- [ ] Nav items gated by `requiredRoles` â€” only users with matching `org_role` see the link
- [ ] Nav items gated by `requiredTier` â€” only orgs at that tier or above see the link
- [ ] Nav items gated by `requiredPermissions` â€” only users with at least one matching permission see the link
- [ ] System-admin-only groups (`systemAdminOnly`) â€” hidden from non-admin users
- [ ] Expandable sub-items inherit parent's permission gating â€” children do not bypass parent restrictions

## Known Gaps

- **No BDD coverage** â€” No `.feature` files for navigation behaviours (breadcrumbs, sidebar groups, view mode toggling, page tabs). Should be added under `tests/bdd/features/navigation/`.
- **No SidebarNav unit tests** â€” `SidebarNav.vue` has no dedicated tests. `AppLayout.spec.ts` covers some sidebar link rendering but is limited.
- **No expandable sub-item unit tests** â€” The collapse/expand logic for sidebar sub-items is untested.

## QA History

- **2026-07-29 â€” Sidebar Restructure**: Replaced 11 groups with 4 (BUILD/MONITOR/CONFIGURE/ADMIN). Added expandable sub-items. Removed Essentials/All Features toggle. Added search button in header. Added Command Palette. See ADR 017 for full rationale and UX research.
