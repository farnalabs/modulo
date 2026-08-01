# Maintainability Report

## Critical
*No critical findings.*

## Major

### 1. File exceeds 500 lines — SchemaEditorView.vue (750 lines)
`frontend/src/views/SchemaEditorView.vue:1-750`
This file is 750 lines and mixes sidebar (schema list), form editor, JSON preview, version history, and validation logic. Should be split into at least: `SchemaEditorSidebar.vue`, `SchemaEditorForm.vue`, `SchemaVersionHistory.vue`, and `SchemaJsonPreview.vue`.

**Progress:** the pure schema logic (`parseDefinitionToFields`, `coerceDefault`, `buildJsonSchema`, `createField`) was extracted to `frontend/src/utils/schema-definition.ts`, and the duplicated `.input-base` class strings were consolidated. The remaining work is the component split itself.

### 2. ~~Massive template duplication — LibraryView.vue~~ **FIXED**
`LibraryView.vue` now renders all three sections (native primitives, preview primitives, community primitives) through the shared `LibraryPrimitiveCard.vue` component.

### 3. ~~Mixed API patterns — SchemaEditorView.vue~~ **FIXED**
`SchemaEditorView.vue` now uses only the typed `api` client (`api.GET`, `api.POST`, `api.PATCH`) — no raw `fetch()` calls remain.

### 4. ~~Hardcoded English not using i18n~~ **FIXED**
All previously-listed views (`SettingsHitlReviewView`, `SettingsErrorForwardersView`, `SchemaListView`, `SchemaEditorView`, `LibraryPipelineWizard`, `EvalEditorView`, and `AdminErrorDetailView`) now wrap user-facing strings in `$t()` / `t()`.

### 5. ~~Type safety bypassed with `as any` casts — SettingsErrorForwardersView.vue~~ **FIXED**
The three API calls no longer cast the typed `api` client to `any`.

## Minor

### 6. ~~Empty catch blocks~~ **FIXED**
All previously-silent empty catch blocks now log via `console.warn()`:
- `SchemaEditorView.vue` (loadVersions / formatDate) — added `console.warn`
- `RunDetailView.vue` (fetchHitlGates) — added `console.warn`
- `ParameterSchemasView.vue` (loadPickers) — added `console.warn`

Empty catches hide failures and make debugging impossible. At minimum `console.warn()`.

### 7. ~~No-op computed — SettingsHitlReviewView.vue:339~~ **FIXED**
The wrapping computed was removed; `currentClaimToken` is now `computed(() => claimTokens.value)`.

### 8. ~~Duplicated field-loading logic — SchemaEditorView.vue:539-549 / 724-734~~ **FIXED**
The identical field extraction logic from `definition_json.properties` was extracted into a shared `parseDefinitionToFields(def)` helper used by both `loadLatestVersion` and `restoreVersion`.

### 9. ~~Wrong i18n key reference — LibraryView.vue:28~~ **FIXED**
`LibraryView` now references its own `views.LibraryView.all_types` key instead of borrowing `AdminNotificationDeliveryLogView`'s.

### 10. ~~Pagination total incorrect for native section — LibraryView.vue~~ **FIXED**
Previously `total` was set to the client-side filtered array length, so pagination showed the wrong page count when filters reduced results. The view now uses the server's `total`, and the backend accepts a comma-separated `primitive_types` query param (`list_primitives` → CRUD + modulo/community in-memory filters + published-community DB query) so that selecting multiple primitive types filters server-side and `total`/pages stay accurate. See `backend/src/modulo/api/routes/library.py`, `backend/src/modulo/core/library_service/__init__.py`, `backend/src/modulo/db/crud/library_primitive.py`, `frontend/src/views/LibraryView.vue`.

Known approximation: the native tab merges org-local + modulo sources in one request, so community items are still counted in the server `total` (they are stripped for display). Fully excluding community server-side would require per-source requests and is tracked as a follow-up.

### 11. ~~Graph layout algorithm embedded in view — LibraryPipelineWizard.vue:245-289~~ **FIXED**
`layoutNodes()` was extracted to `frontend/src/utils/graph-layout.ts` with a unit spec (`frontend/src/__tests__/graph-layout.spec.ts`); `LibraryPipelineWizard.vue` imports it.

### 12. ~~`formatApiError(e)` in catch block — FeedbackInboxView.vue:329~~ **FIXED**
`formatApiError` in `frontend/src/lib/api/formatError.ts` already handles bare `Error` instances (returns `err.message`), and every `catch` block in `FeedbackInboxView.vue` passes the thrown error through it. No special-casing is required.

### 13. ~~HTML entity `&larr;`~~ **FIXED**
`LibraryPipelineWizard.vue` and `AdminErrorDetailView.vue` now use the Lucide `ArrowLeft` icon instead of the `&larr;` HTML entity.

### 14. ~~`$router` vs `router` inconsistency~~ **FIXED**
`LibraryView.vue` and `LibraryPipelineWizard.vue` now consistently use `router` from `useRouter()` across both template and script.

### 15. ~~Legacy `useApi` composable — MyProfileView.vue:78~~ **FIXED**
Migrated to the typed `api` client (`api.GET('/api/v1/me')`, `api.PUT('/api/v1/me/password')`); the unit spec was updated to mock the client module.

### 16. ~~Fire-and-forget async in setInterval — SettingsHitlReviewView.vue~~ **FIXED**
`startAutoRefresh()` now guards overlapping refreshes with a `refreshInFlight` flag and awaits `loadGates().finally(...)` to reset the countdown, so no two refreshes run concurrently.

### 17. ~~Duplicated Tailwind class strings — SettingsErrorForwardersView.vue~~ **FIXED**
The input class string `w-full rounded-lg border border-input bg-background px-3 py-2 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring` is repeated ~20 times. Should be extracted to a shared component or a CSS utility class.
Extracted into a shared `.input-base` CSS utility class in `frontend/src/style.css`; all 13 duplicated occurrences in `SettingsErrorForwardersView.vue` now reference `class="input-base"`. The same utility now also replaces the 3 duplicated occurrences in `SchemaEditorView.vue`.
