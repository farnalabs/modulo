# Maintainability Report

## Critical
*No critical findings.*

## Major

### 1. File exceeds 500 lines — SchemaEditorView.vue (770 lines)
`frontend/src/views/SchemaEditorView.vue:1-770`
This file is 770 lines and mixes sidebar (schema list), form editor, JSON preview, version history, and validation logic. Should be split into at least: `SchemaEditorSidebar.vue`, `SchemaEditorForm.vue`, `SchemaVersionHistory.vue`, and `SchemaJsonPreview.vue`.

### 2. Massive template duplication — LibraryView.vue
`frontend/src/views/LibraryView.vue:91-275`
Three nearly-identical grid sections (native primitives, preview primitives, community primitives) with ~80 lines each — card structure (badge, name, description, tags, action buttons) duplicated 3x. Extract to a `LibraryPrimitiveCard.vue` component.

### 3. Mixed API patterns — SchemaEditorView.vue
`frontend/src/views/SchemaEditorView.vue:529,561,602,651,687`
Uses both the typed `api` client (`api.GET`, `api.POST`) and raw `fetch()` with manual `getAccessToken()` auth header. Two API access patterns in the same file increases cognitive load and risks token-handling inconsistencies.

### 4. Hardcoded English not using i18n — multiple files
Several views have hardcoded English strings not wrapped in `$t()` / `t()`:

- **SettingsHitlReviewView.vue** — title, subtitle, all filter labels, option text, button text, empty-state text, status text. Nearly every user-facing string is hardcoded.
- **SettingsErrorForwardersView.vue** — most form labels, placeholders, button text, status text (only the title uses `$t()`).
- **SchemaListView.vue** — tab labels, page title, column headers, status badges, action buttons.
- **SchemaEditorView.vue** — tab labels, page title, button text, section headings, field labels, empty-state text. The `PageTabs` labels are hardcoded, unlike SchemaInferenceView which uses `$t()` for the same tabs.
- **LibraryPipelineWizard.vue** — heading, subtitle, labels, button text, empty states. No `$t()` usage at all.
- **EvalEditorView.vue** — `PageTabs` labels, eval type `<option>` text are hardcoded.

### 5. Type safety bypassed with `as any` casts — SettingsErrorForwardersView.vue
`frontend/src/views/SettingsErrorForwardersView.vue:315,355,384`
Three API calls cast the typed `api` client to `any` to bypass path/parameter type checking. Error-prone: if the API signatures change, these calls won't fail at compile time.

## Minor

### 6. Empty catch blocks — 4 files
- `frontend/src/views/SettingsHitlReviewView.vue:404` — `catch {}` with comment "Non-critical"
- `frontend/src/views/MyProfileView.vue:116-118` — `catch {}` with fallback assignment
- `frontend/src/views/SchemaEditorView.vue:553-555` — `catch {}` with comment "silently ignore"
- `frontend/src/views/SchemaEditorView.vue:752-754` — `catch {}` with comment "clipboard not available"

Empty catches hide failures and make debugging impossible. At minimum `console.warn()`.

### 7. No-op computed — SettingsHitlReviewView.vue:339
```ts
const currentClaimToken = computed(() => claimTokens)
```
`claimTokens` is already a `ref<Record<string, string>>`, so `computed(() => claimTokens)` produces a `ComputedRef<Ref<Record<string, string>>>` — wrapping a ref in an unnecessary computed that adds no value.

### 8. Duplicated field-loading logic — SchemaEditorView.vue:539-549 / 724-734
`loadLatestVersion` (lines 539–549) and `restoreVersion` (lines 724–734) contain identical field extraction logic from `definition_json.properties`. Should be extracted to a function like `parseDefinitionToFields(def)`.

### 9. Wrong i18n key reference — LibraryView.vue:28
```vue
<option value="">{{ $t('views.AdminNotificationDeliveryLogView.all_types') }}</option>
```
Uses a translation key from a completely different view (`AdminNotificationDeliveryLogView`). Either needs its own key or a shared common key.

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

### 13. HTML entity `&larr;` — LibraryPipelineWizard.vue:11
Uses `&larr;` for the back arrow instead of an SVG icon or the `<BackLink>` component's built-in styling. Inconsistent with the rest of the codebase which uses Lucide SVGs.

### 14. `$router` vs `router` inconsistency — LibraryView.vue
The template uses `$router` (line 8 injected by Vue Router), but script uses `router` from `useRouter()` (line 338). Different naming for the same thing across template and script.

### 15. Legacy `useApi` composable — MyProfileView.vue:78
Uses the legacy `get`/`put` from `useApi()` (throws on error) instead of the typed `api` client (`api.GET`/`api.PUT`). Inconsistent with newer views.

### 16. Fire-and-forget async in setInterval — SettingsHitlReviewView.vue:551-555
```ts
refreshTimer = setInterval(() => {
    loadGates()  // async function, promise not awaited
    ...
}, refreshInterval.value)
```
`loadGates()` is async but called without `await` from a `setInterval` callback. Errors are caught internally but the promise is fire-and-forget.

### 17. Duplicated Tailwind class strings — SettingsErrorForwardersView.vue
The input class string `w-full rounded-lg border border-input bg-background px-3 py-2 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring` is repeated ~20 times. Should be extracted to a shared component or a CSS utility class.
