# Frontend — Agent Guidance

## Lessons Learned

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
