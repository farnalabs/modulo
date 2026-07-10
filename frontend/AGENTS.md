# Frontend — Agent Guidance

## Lessons Learned

### reka-ui v2.10.1 ships `index.d.cts` but `package.json.types` points to `index.d.ts`

The `reka-ui` npm package v2.10.1 ships its type declarations as `dist/index.d.cts` (CommonJS TypeScript)
but its `package.json` `types` field points to the non-existent `dist/index.d.ts`. This causes
`@vue/compiler-sfc` to fail with "Unresolvable type reference" when resolving
`defineProps<RekaUiProps>()` in `.vue` components, breaking both `npm run build` and `npm run test:unit`.

The `postinstall` script in `package.json` (at `scripts/reka-ui-patch.ps1`) copies
`index.d.cts` → `index.d.ts` after each `npm install`.

**Verification:** After `npm install`, run:
```powershell
Test-Path node_modules/reka-ui/dist/index.d.ts
# Should be True
```

When upgrading reka-ui, verify the package still ships only `.d.cts` (not `.d.ts`)
and that `scripts/reka-ui-patch.ps1` still applies. If reka-ui ships proper `.d.ts`
in a future version, remove `scripts/reka-ui-patch.ps1` and the `postinstall` script.

**Note:** The postinstall only runs on `npm install`. If `scripts/reka-ui-patch.ps1`
is edited, re-run `npm install` (not just the script) to ensure the hook fires.

### Permission gating: `canSeeItem()` must check `requiredPermissions`

- The sidebar nav gating function was ignoring `requiredPermissions` from navigation config, causing unauthorized items to be visible (though non-functional). Always verify that permission-gated nav items actually check permissions — not just feature flags but also role-based `requiredPermissions`. Add an explicit assertion per item: `if (item.requiredPermissions && !userHasPermission(item.requiredPermissions)) return false;`.

### Config: avoid module-level side effects

- `buildSidebarGroups()` called at module import time crashes if any config key is missing or if the router isn't fully initialized. Module-level side effects also make tree-shaking and testing impossible. Guard module-level calls in a `try/catch` or, better, make them lazy — call only when the component mounts, not when the module loads.

### Route config: no duplicate path keys

- Frontend route configuration had duplicate keys (same path mapped twice), letting the second registration silently override the first. Use `new Map()` with unique key validation, or add a CI check (`duplicate-route-keys.ps1`) that fails if any path appears more than once in the route config.

- When rewriting or restoring a layout component (e.g., `AppLayout.vue` after an SFC parsing fix), always verify that responsive hiding classes (`hidden md:flex` on desktop sidebar, `md:hidden` on mobile elements) are preserved. These are easily lost during a restore from a pre-mobile baseline.

- Port editor forms (`PortDefinitionPanel.vue`) must preserve all port fields on edit. When adding a boolean flag like `multiline` to the `ParameterPort` interface, update `formDefaults`, `openEditForm` (to read the flag), `savePort` (to write it), and the template (to provide a UI control). Missing a flag in `openEditForm` causes silent data loss when a user edits a port.

- Frontend `ParameterPort` interface fields must mirror the backend Pydantic `ParameterPort` model. A type mismatch on `options` (frontend expects `{value, label}[]`, backend sends `str[]`) causes broken select dropdowns at runtime. Keep the two models in sync — check both when adding or changing a field.

 - All collapsible toggle buttons (`SidebarGroup.vue`, accordions, disclosure widgets) must have `:aria-expanded` (boolean), `:aria-controls` (pointing to the controlled region's `id`), and the content region must have `role="region"` with `:aria-label`. Without these, screen readers cannot tell whether the group is open or closed.

 - Every `<nav>` element must have an `aria-label` (e.g. `"Main navigation"`, `"Secondary navigation"`) so screen reader users can distinguish multiple nav landmarks.

 - Every active navigation link (`router-link` in a sidebar) must have `:aria-current="'page'"` when active. Without it, screen reader users cannot identify the current page in a navigation list.

 - Toggle buttons that switch between two states (e.g. view mode "simple" vs "advanced") must have `:aria-pressed` set to the active state boolean, plus a descriptive `:aria-label` explaining what the button does. A visible text label alone is insufficient for screen readers to understand the toggle state.

 - Complex template filter expressions (compound `v-if` with multiple conditions) should be extracted to a `computed` property. This improves testability and readability, and avoids repeating the filter logic in the template.

 - Composables should wrap mutable refs in `readonly()` when exporting them, to prevent consumers from bypassing the composable's mutation API and creating inconsistent state (e.g. mutating `groupPrefs` directly without calling `save()`).

 - Empty `catch {}` blocks in async API calls → at minimum log the error with `console.warn(err)`. Silent catches make debugging impossible and hide network failures, 500s, and auth errors from both developers and users. For user-visible features, also show a brief inline error or toast.

- `new Date(invalidStr)` never throws — it silently creates an invalid Date object. Always check `isNaN(d.getTime())` after constructing a Date from a string before calling any Date methods. A `try/catch` around Date construction will not catch invalid input.

- Sidebar containers must use `h-screen sticky top-0` (not `min-h-screen`) with `flex flex-col`. The `SidebarFooter` must use `mt-auto` to stay locked at the viewport bottom. Without `sticky`, the sidebar scrolls with the page instead of filling the viewport height when main content is longer than the screen. Without `mt-auto`, the footer floats below the nav items instead of anchoring to the bottom.

- Dashboard trend data (`fetchTrends`) must be fetched in parallel with summary (`fetchSummary`) on mount, not lazily on first duration-button click. The user sees a blank trend section until they interact with the 7d/30d/90d buttons, making the dashboard feel slow. Add `dashboardStore.fetchTrends(7)` to the `onMounted` promise array.

- API client 401 handling must attempt a refresh token rotation before redirecting to login. Use a module-level `_refreshingPromise` to deduplicate concurrent refresh attempts — when multiple API calls get 401 simultaneously, they should share one refresh request and all retry with the new token. Pattern: `const resp = await fn(...); if (resp.response?.status === 401) { const refreshed = await attemptTokenRefresh(); if (refreshed) resp = await fn(...); }`

### Nav link `exact` matching: child routes activate parent nav items unless `exact: true`

- When a sidebar nav item links to `/pipelines` (router-link default matches prefix), navigating to `/pipelines/copy` also activates the "My Pipelines" link because Vue Router's default matching is prefix-based. Set `exact: true` on parent nav items when child routes exist (e.g. `/pipelines/copy`, `/pipelines/new`) to prevent double-highlighting. In `manifest.yaml`, add `exact: true` to the parent route entry.

### Skills change signal: after adding/editing/deleting a skill, signal the store to rebuild system prompt

- The Remy system prompt is built once per session and caches the skill list. When a user adds or modifies a skill (via `RemySkillManager.vue` or `UserRemySkillsView.vue`), the store needs a `signalSkillsChanged()` mechanism (e.g. incrementing a `skillsVersion` ref) so the next Remy session `/stream` call re-fetches skills and includes the new one. Without this signal, newly added skills don't appear in the conversation until a page refresh. Pattern: maintain a `skillsVersion` counter in `useRemyStore.ts`, increment it on skill change, and read it when building the stream request payload.

### reka-ui TooltipContent + vue-i18n: `$t()` crashes inside Teleported content

- reka-ui's `TooltipContent` uses `Teleport` internally. `$t()` called directly inside a `<TooltipContent>` template throws `TypeError: _ctx.t is not a function` because the teleported content loses access to `app.config.globalProperties`. **Fix:** pre-translate the text in the parent component (or use plain English strings) and reference the variable in the tooltip — never call `$t()` inside `TooltipContent`.

### Null-guard computed properties that access nested properties of async-loaded refs

- When a ref is populated from an API response (`ref.value = data as SomeType`), the response may not match the expected shape in tests (catch-all mock returns `{items:[], total:0}`). Any computed or template code accessing nested properties like `data.value.native.map(...)` must use `(data.value.native ?? []).map(...)` — otherwise a shape mismatch throws `TypeError: Cannot read properties of undefined`. This applies to ALL computed properties, not just the one that first exhibits the failure.

### `locator.evaluate(el => el.click())` to bypass overlays in Playwright

- When a UI overlay (e.g. the Remy panel) covers an element, Playwright's `locator.click()` refuses to click because the element is not actionable. `click({ force: true })` dispatches the event but Vue's `@click` handler may not fire if an overlay captures the event. Use `locator.evaluate((el) => el.click())` to dispatch a native DOM click that always triggers the handler, regardless of overlays.
