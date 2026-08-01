# Maintainability Report

## Critical
*No critical findings.*

## Major

### 1. File exceeds 500 lines — SchemaEditorView.vue (770 lines)
`frontend/src/views/SchemaEditorView.vue:1-770`
This file is 770 lines and mixes sidebar (schema list), form editor, JSON preview, version history, and validation logic. Should be split into at least: `SchemaEditorSidebar.vue`, `SchemaEditorForm.vue`, `SchemaVersionHistory.vue`, and `SchemaJsonPreview.vue`.

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

### 10. Pagination total incorrect for native section — LibraryView.vue:388
```ts
total.value = section.value === 'native' ? primitives.value.length : data.total
```
For the native section, `total` is set to the client-side filtered array length, not the server's total count. This means pagination controls show the wrong page count when filters reduce results — and you can never advance past page 1 if the API returned fewer items than the page size.

### 11. Graph layout algorithm embedded in view — LibraryPipelineWizard.vue:245-289
`layoutNodes()` is a 44-line topological sort with manual coordinate layout (DAG layering, in-degree calculation, position centering). This is significant algorithmic complexity in a view component — should be extracted to `utils/graph-layout.ts`.

### 12. `formatApiError(e)` in catch block — FeedbackInboxView.vue:329
```ts
error.value = `${t(...)} ${formatApiError(e)}`
```
In the `catch` block, `e` is a thrown `Error` object, not an API response. `formatApiError` expects `ProblemDetail | unknown` and its error-detail extraction logic may not produce a useful message from a bare `Error`.

### 13. ~~HTML entity `&larr;`~~ **FIXED**
`LibraryPipelineWizard.vue` and `AdminErrorDetailView.vue` now use the Lucide `ArrowLeft` icon instead of the `&larr;` HTML entity.

### 14. ~~`$router` vs `router` inconsistency~~ **FIXED**
`LibraryView.vue` and `LibraryPipelineWizard.vue` now consistently use `router` from `useRouter()` across both template and script.

### 15. ~~Legacy `useApi` composable — MyProfileView.vue:78~~ **FIXED**
Migrated to the typed `api` client (`api.GET('/api/v1/me')`, `api.PUT('/api/v1/me/password')`); the unit spec was updated to mock the client module.

### 16. Fire-and-forget async in setInterval — SettingsHitlReviewView.vue:551-555
```ts
refreshTimer = setInterval(() => {
    loadGates()  // async function, promise not awaited
    ...
}, refreshInterval.value)
```
`loadGates()` is async but called without `await` from a `setInterval` callback. Errors are caught internally but the promise is fire-and-forget.

### 17. ~~Duplicated Tailwind class strings — SettingsErrorForwardersView.vue~~ **FIXED**
The input class string `w-full rounded-lg border border-input bg-background px-3 py-2 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring` is repeated ~20 times. Should be extracted to a shared component or a CSS utility class.
Extracted into a shared `.input-base` CSS utility class in `frontend/src/style.css`; all 13 duplicated occurrences in `SettingsErrorForwardersView.vue` now reference `class="input-base"`.
