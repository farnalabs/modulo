---
id: feat-core-navigation-restructure
prd: 8.26
delivery-tasks: []
bdd: []
unit-tests:
  - frontend/src/__tests__/components/AppLayout.spec.ts
  - frontend/src/__tests__/components/SidebarNav.spec.ts
  - frontend/src/__tests__/components/SidebarGroup.spec.ts
  - frontend/src/__tests__/components/SidebarLink.spec.ts
  - frontend/src/__tests__/navigation.spec.ts
  - frontend/src/__tests__/navigation-errors.spec.ts
  - frontend/src/__tests__/Breadcrumb.spec.ts
code:
  - frontend/src/components/SidebarNav.vue
  - frontend/src/components/SidebarGroup.vue
  - frontend/src/components/SidebarLink.vue
  - frontend/src/components/AppLayout.vue
  - frontend/src/components/Breadcrumb.vue
  - frontend/src/composables/useSidebar.ts
  - frontend/src/config/navigation.ts
  - frontend/src/manifest.yaml
depends-on: []
status: partial
---

# Navigation Restructure (Frontend UX)

The frontend navigation system with sidebar groups, collapsible sections, and command palette search.

## Architecture Decision

The sidebar was restructured from 11 groups into 4 (BUILD, MONITOR, CONFIGURE, ADMIN) with collapsible groups whose state persists to `localStorage`. Expandable sub-menus were removed in #373 — all routes render as top-level items within their group, sorted by `sidebar_order`. Groups and items are gated by role, tier, permission, visibility, and dev-mode.

## Behaviours

- [x] Sidebar with 4 groups (BUILD, MONITOR, CONFIGURE, ADMIN)
- [x] BUILD and MONITOR expanded by default (daily drivers)
- [x] CONFIGURE and ADMIN collapsed by default (set-and-forget pages)
- [x] Expand/collapse per group with chevron indicators, persisted to localStorage
- [x] Groups render a flat list of top-level items (expandable sub-menus removed in #373; children are top-level items)
- [x] Items navigate directly on click (one-click destinations)
- [x] Bottom bar: user profile, team, dark mode toggle, sign out
- [x] Search icon in header (Notification Bell area) opens Command Palette
- [x] Command Palette (Cmd+K / Ctrl+K) indexes all sidebar items and extras
- [x] Breadcrumbs on all pages
- [x] Sub-navigation within page domains via PageTabs (horizontal pill tabs)
- [x] Permission gating per nav item (requiredRoles, requiredTier, requiredPermissions)
- [x] Visibility gating per nav item (public, public_preview, private_preview, in_dev)

## Error Handling

- [x] Malformed `manifest.yaml` (missing `routes` or `sidebar_groups`) — `buildSidebarGroups()` returns empty array, `console.error` logged, sidebar renders blank
- [x] Route references non-existent `sidebar_group` — logged as `console.warn`, item skipped
- [x] Route name missing from `routeConfigMap` — falls back to generic `File` icon and `nav.<name>` labelKey
- [x] JWT token missing `sub`, `org_role`, or `is_system_admin` claims — defaults to `""`, `null`, `false` (AppLayout.vue)
- [x] `localStorage` unavailable (private browsing, quota exceeded) — `useStorage` from `@vueuse/core` catches and falls back to defaults (errors routed to vueuse `onError`)
- [x] Plan/tier fetch fails (`fetchPlan` caught with `.catch(() => {})`) — sidebar renders all tier-gated items as hidden
- [x] Route missing numeric `sidebar_order` — skipped by `isManifestRoute` guard (never rendered)

## Edge Cases

- [x] All sidebar groups hidden by tier/role — produces empty sidebar (groups filtered by `g.items.length > 0`)
- [x] Single group with single item — renders one expandable group containing one link
- [x] Deep breadcrumb chain or cyclic parent refs — `Breadcrumb.vue` walks `parent` chain with `visited` Set to prevent infinite loops
- [x] Route path exactly `/` — sidebar groups use `item.exact` check to avoid `path.startsWith("/")` activating all groups
- [x] `sidebar_group` name mismatch between `manifest.yaml` route and `manifest.sidebar_groups` — logged as `console.warn`, item not rendered
- [x] Group with matching routes removed after being cached — `_cachedGroups` returns stale data until page reload (no invalidation mechanism)
- [x] `requiredRoles: []` (empty array) — `canSeeItem` returns `false` for all users (empty whitelist means no access)
- [x] Non-exact items activate their group for child paths — `path.startsWith(item.to)` matches nested routes
- [x] Tier-gated item while plan is not yet loaded — `tierInfoLoaded` is false, item hidden until tiers arrive

## Security

- [x] Nav items gated by `requiredRoles` — only users with matching `org_role` see the link
- [x] Nav items gated by `requiredTier` — only orgs at that tier or above see the link
- [x] Nav items gated by `requiredPermissions` — only users with at least one matching permission see the link
- [x] System-admin-only groups (`systemAdminOnly`) — hidden entirely for non-admin users (group-level gate; all items inherit)
- [x] Visibility gating — `private_preview` / `in_dev` items hidden unless plan `devMode` is enabled

## Known Gaps

- **No BDD coverage (accepted)** — Navigation behaviours are covered by Vitest component/unit tests (`SidebarNav.spec.ts`, `SidebarGroup.spec.ts`, `SidebarLink.spec.ts`, `navigation.spec.ts`, `navigation-errors.spec.ts`, `Breadcrumb.spec.ts`). There is no pytest-bdd harness for Vue components, so a backend `.feature` file would not exercise the frontend code.
- **No CommandPalette unit tests** — the Cmd+K palette indexing logic is untested.
- **No SidebarFooter unit tests** — bottom-bar behaviours (profile, team, dark mode toggle, sign out) are asserted only via AppLayout smoke tests.
- **`_cachedGroups` is never invalidated** — a runtime manifest change is not reflected until a full page reload (module-level cache in `config/navigation.ts`).
- **`useSidebar` storage-quota path not asserted** — quota-exceeded writes route to vueuse `onError`; fallback defaults are tested only through `SidebarNav.spec.ts`.

## QA History

- **2026-07-31 — improve-architecture**: Verified 20 previously-unchecked Error Handling / Edge Case / Security behaviours against code and marked `[x]`. Added `SidebarNav.spec.ts` (10 tests), `SidebarGroup.spec.ts` (4), `SidebarLink.spec.ts` (3), `navigation-errors.spec.ts` (3), a breadcrumb cycle-protection test, and empty-whitelist `canSeeItem` edge cases in `navigation.spec.ts`. Fixed a broken ADR reference (pointed at a non-existent `017-navigation-sidebar-restructure.md`; ADR 017 is the Celery→SAQ migration). Aligned the entry with the current flat-list design (expandable sub-menus removed in #373).
- **2026-07-29 — Sidebar Restructure**: Replaced 11 groups with 4 (BUILD/MONITOR/CONFIGURE/ADMIN). Added expandable sub-items. Removed Essentials/All Features toggle. Added search button in header. Added Command Palette.
