# Frontend — Agent Guidance

## Lessons Learned

- When rewriting or restoring a layout component (e.g., `AppLayout.vue` after an SFC parsing fix), always verify that responsive hiding classes (`hidden md:flex` on desktop sidebar, `md:hidden` on mobile elements) are preserved. These are easily lost during a restore from a pre-mobile baseline.

- Port editor forms (`PortDefinitionPanel.vue`) must preserve all port fields on edit. When adding a boolean flag like `multiline` to the `ParameterPort` interface, update `formDefaults`, `openEditForm` (to read the flag), `savePort` (to write it), and the template (to provide a UI control). Missing a flag in `openEditForm` causes silent data loss when a user edits a port.

- Frontend `ParameterPort` interface fields must mirror the backend Pydantic `ParameterPort` model. A type mismatch on `options` (frontend expects `{value, label}[]`, backend sends `str[]`) causes broken select dropdowns at runtime. Keep the two models in sync — check both when adding or changing a field.

- Empty `catch {}` blocks in async API calls → at minimum log the error with `console.warn(err)`. Silent catches make debugging impossible and hide network failures, 500s, and auth errors from both developers and users. For user-visible features, also show a brief inline error or toast.
