---
id: feat-core-schema-inference-ui
prd: 8.16
delivery-tasks: [task-nv5-schema-inference-ui]
bdd:
  - backend/tests/bdd/features/connectors/schema_inference.feature
  - backend/tests/bdd/features/schemas/schema_inference.feature
code:
  - frontend/src/views/SchemaInferenceView.vue
  - frontend/src/views/OnboardingWizard.vue
  - frontend/src/router/index.ts
  - frontend/src/lib/api/schema.ts
  - frontend/src/__tests__/SchemaInferenceView.spec.ts
depends-on: [feat-core-ai-schema-gen]
unit-tests:
  - frontend/src/__tests__/SchemaInferenceView.spec.ts
status: partial
---

# Schema Inference UI

Standalone schema inference page (`/schemas/infer`) and onboarding wizard steps 2–3. Provides a UI for triggering LLM-assisted schema drafting from connected tool data and publishing the result to the schema registry.

## Behaviours

### Standalone view — `SchemaInferenceView.vue`

- [x] Loads available connectors from `GET /api/v1/connectors` on mount
- [x] Shows loading spinner while connectors are loading
- [x] Displays error message when connector loading fails
- [x] Shows "No connectors available" hint when connector list is empty
- [x] Connector dropdown lists all loaded connectors with type
- [x] Resource type text input with placeholder hint ("e.g. issues, repositories, pull_requests")
- [x] Sample query textarea (optional) with placeholder hint
- [x] "Infer Schema" button disabled when no connector selected or resource type empty
- [x] "Infer Schema" button shows "Inferring..." text while in progress
- [x] Calls `POST /api/v1/schemas/infer` with correct request body (`sample_query` as object `{ resource, filters, limit }`)
- [x] Displays inference error message when the API call fails
- [x] Shows draft schema section after successful inference
- [x] Displays draft schema name (from `suggestion_name`) and description (from `suggestion_description`)
- [x] Renders fields table with columns: Name (monospace), Type (monospace), Required (yes/no badge), Description (extracted from `definition_json.properties`)
- [x] Shows "No fields inferred" when fields array is empty (handled via `extractFieldsFromDefinition`)
- [x] Toggle button to show/hide raw JSON of the draft schema (raw `JSON.stringify` shows `definition_json` correctly)
- [x] "Publish" button creates schema envelope (`POST /api/v1/schemas`) then version with `definition_json` (`POST /api/v1/schemas/{schema_id}/versions`)
- [x] "Publish" button shows "Publishing..." text while in progress
- [x] Displays publish error message when the publish API call fails
- [x] Displays publish success message with schema name
- [x] Navigates to library view after successful publish (1.5s delay)
- [x] "Discard" button resets draft schema, errors, and raw JSON toggle
- [x] Page is accessible at `/schemas/infer` with route name `schema-infer`

### Onboarding wizard — `OnboardingWizard.vue` steps 2–3

- [x] Step 2 shows selected connector name from step 1
- [x] Step 2 resource type input with placeholder hint
- [x] Step 2 sample query textarea (optional)
- [x] Step 2 "Infer Schema" button disabled when resource type empty or inferring
- [x] Step 2 calls `POST /api/v1/schemas/infer` with correct request body (`sample_query` as object)
- [x] Step 2 displays inference error message
- [x] Step 2 shows draft schema in a compact table (name, description, fields)
- [x] Step 2 "No fields inferred" fallback when fields array is empty
- [x] Step 3 shows editable schema name and description fields from draft (from `suggestion_name`/`suggestion_description`)
- [x] Step 3 fields table is read-only with hint "re-infer to change"
- [x] Step 3 "Confirm & Save Schema" button creates schema envelope then version with `definition_json`
- [x] Step 3 displays save error message
- [x] Step 3 displays success message with published schema ID
- [x] Step progression: canProceed for step 2 requires draftSchema, step 3 requires publishedSchemaId

### API types and client — `schema.ts` (auto-generated from backend OpenAPI spec)

- [x] `SchemaInferRequest` defines `connector_instance_id` (uuid), `sample_query` as `SchemaSampleQuery` object
- [x] `SchemaSampleQuery` defines `resource` (string), `filters` (object), `limit` (int)
- [x] `SchemaFieldDefinition` — extracted from `definition_json.properties`
- [x] `SchemaInferResponse` defines `definition_json`, `sample_count`, `suggestion_name`, `suggestion_description`
- [x] `SchemaCreate` defines `name`, `description`, `abstract_name`
- [x] `SchemaCreateResponse` defines `id`, `name`
- [x] `POST /api/v1/schemas/infer` endpoint typed in paths
- [x] `POST /api/v1/schemas` endpoint typed in paths
- [x] `POST /api/v1/schemas/{schema_id}/versions` endpoint typed in paths

### Error handling and edge cases

- [x] Inference API failure (422/502/404) → user-visible error message with API detail
- [x] Publish API failure → user-visible error message with API detail
- [x] Connector loading failure → user-visible error message
- [x] Loading states disable buttons during API calls (prevents double-submit)
- [x] Network errors caught by try/catch with readable message
- [x] Empty resource type rejection (button disabled)
- [x] Null description renders dash placeholder ("—")
- [x] Empty fields array renders "No fields inferred"
- [x] API error objects formatted via `formatApiError()` — no bare `${err}` in template literals (fixed Jul 2026)
- [x] `.catch()` blocks use `formatApiError` for structured error display (fixed Jul 2026)

### i18n compliance

- [ ] All user-facing strings use `$t()` or `t()` — **remaining hardcoded strings verified 2026-08-15**: the "raw JSON" toggle suffix in `SchemaInferenceView.vue` and ~5 script-side error/success strings in the same view ("Failed to load connectors: …", "Schema inference failed: …", "Publish failed: …", "Publish failed: no response", "Schema \"…\" published."). The OnboardingWizard step titles/subtitles and schema-step labels/buttons already use `t()`/`$t()`.
- [x] Template button text, labels, placeholders, table headers use `$t()` in both views (verified 2026-08-15 — only the "raw JSON" toggle suffix remains hardcoded)
- [x] Error messages use `formatApiError()` instead of bare `${err}` in both views (verified 2026-08-15)

### Missing — not yet implemented

- [ ] Schema editor component: operator cannot rename fields, adjust types, toggle required, or edit field descriptions
- [ ] No `abstract_name` display or filter in library browse step (per 8.16 step 4)
- [ ] No rare-field flagging or exclusion summary shown in draft UI (per 8.16)
- [ ] No enum detection display for issue_type, status, priority fields
- [ ] No sample count display (PRD default 200, code caps at 50)
- [ ] No versioning display when publishing schema
- [x] Connector-type validation feedback: the backend rejects unsupported connector types with 400 + the supported-type list, surfaced through the view's `formatApiError` error display (verified 2026-08-15 — previously listed as missing)
- [x] ~~**BDD step definitions not yet implemented**~~ **RESOLVED 2026-08-15**: `backend/tests/bdd/steps/test_schema_inference.py` (517 lines) provides step definitions for both `connectors/schema_inference.feature` and `schemas/schema_inference.feature` (all scenarios wired, run against mocked backends)

## Known Gaps
- **FIXED: API contract mismatch** (Jul 2026) — Frontend now sends correct `SchemaInferRequest` format (`sample_query` as object with `resource`, `filters`, `limit`), reads `suggestion_name`/`suggestion_description`/`definition_json` from the response, and uses two-step publish (create envelope + create version with `definition_json`). Applied to both `SchemaInferenceView.vue` and `OnboardingWizard.vue`.
- **No schema editor:** PRD 8.16 says "draft opens in the schema editor for the operator to review, rename fields, adjust types". Current UI shows fields as read-only tables in both views — no inline editing, no type picker, no required toggle.
- **BDD step definitions (RESOLVED 2026-08-15):** both feature files have real Gherkin scenarios (6 and 7) and `backend/tests/bdd/steps/test_schema_inference.py` provides matching step definitions.
- **Frontend test coverage improved (2026-08-15):** `SchemaInferenceView.spec.ts` now has 9 tests covering connector load (success/empty/error), infer-button gating, infer request body, draft rendering, inference errors, publish (envelope + version + navigate to library), and publish errors. OnboardingWizard schema steps (2–3) still have no spec.
- **No `abstract_name` integration:** PRD 8.16 step 4 requires browsing the community library filtered by inferred `abstract_name`. The inference service doesn't return `abstract_name`, and the frontend library filter dropdown doesn't support it.
- **Sample cap mismatch:** PRD says 200 default sample records; code caps at 50 in prompt builder and 100 in API limit — no sample count displayed in UI.
- **i18n violations (partial fix, verified 2026-08-15):** remaining hardcoded strings are the "raw JSON" toggle suffix and ~5 script-side error/success strings in `SchemaInferenceView.vue` (locale keys not yet added). All template labels/headers/buttons/placeholders and all error paths use `$t()` / `formatApiError()`.
- **formatApiError fix (Jul 2026):** All 8 bare `${err}` template literals in error messages across SchemaInferenceView.vue and OnboardingWizard.vue were replaced with `formatApiError(err)`. `openapi-fetch` returns error objects (not strings) on non-2xx — bare `${err}` renders `[object Object]` instead of the API's error detail.
- **PRD gaps cascade:** Several PRD 8.16 requirements not yet implemented in the backend (rare-field flagging, enum detection, SandboxedEnvironment, data lifecycle enforcement) cascade to the UI — no UI can surface what the backend doesn't provide.

## QA History

- 2026-08-15: Coverage drive (FAR-234). Verified i18n state (template strings + error paths use `$t()`/`formatApiError()`; remaining hardcoded strings narrowed to the "raw JSON" toggle suffix + ~5 script-side strings in `SchemaInferenceView.vue`). Marked checked the connector-type-validation feedback behaviour (backend 400 + supported-type list surfaced via `formatApiError`). Resolved the "BDD step definitions not implemented" gap (`backend/tests/bdd/steps/test_schema_inference.py` wires both feature files). Expanded `SchemaInferenceView.spec.ts` from 1 render test to 9 interaction/API-mock tests (connector load, gating, infer body, draft render, publish envelope+version, navigation, error paths). Status: partial (schema editor, abstract_name display/filter, rare-field flagging, enum display, sample-count display, versioning display remain).
