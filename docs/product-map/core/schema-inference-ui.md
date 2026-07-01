---
id: feat-core-schema-inference-ui
prd: 8.16
delivery-tasks: [task-nv5-schema-inference-ui]
bdd:
  - backend/tests/bdd/features/connectors/schema_inference.feature
code:
  - frontend/src/views/SchemaInferenceView.vue
  - frontend/src/views/OnboardingWizard.vue
  - frontend/src/router/index.ts
  - frontend/src/lib/api/schema.ts
depends-on: [feat-core-ai-schema-gen]
unit-tests: []
status: partial
---

# Schema Inference UI

Standalone schema inference page (`/schemas/infer`) and onboarding wizard steps 2–3. Provides a UI for triggering LLM-assisted schema drafting from connected tool data and publishing the result to the schema registry.

## Behaviours

### Standalone view — `SchemaInferenceView.vue`

- [ ] Loads available connectors from `GET /api/v1/connectors` on mount
- [ ] Shows loading spinner while connectors are loading
- [ ] Displays error message when connector loading fails
- [ ] Shows "No connectors available" hint when connector list is empty
- [ ] Connector dropdown lists all loaded connectors with type
- [ ] Resource type text input with placeholder hint ("e.g. issues, repositories, pull_requests")
- [ ] Sample query textarea (optional) with placeholder hint
- [ ] "Infer Schema" button disabled when no connector selected or resource type empty
- [ ] "Infer Schema" button shows "Inferring..." text while in progress
- [ ] Calls `POST /api/v1/schemas/infer` with connector_instance_id, resource_type, and optional sample_query
- [ ] Displays inference error message when the API call fails
- [ ] Shows draft schema section after successful inference
- [ ] Displays draft schema name and description (description conditional)
- [ ] Renders fields table with columns: Name (monospace), Type (monospace), Required (yes/no badge), Description
- [ ] Shows "No fields inferred" when fields array is empty
- [ ] Toggle button to show/hide raw JSON of the draft schema
- [ ] "Publish" button calls `POST /api/v1/schemas` with draft name, description, and fields
- [ ] "Publish" button shows "Publishing..." text while in progress
- [ ] Displays publish error message when the publish API call fails
- [ ] Displays publish success message with schema name
- [ ] Navigates to library view after successful publish (1.5s delay)
- [ ] "Discard" button resets draft schema, errors, and raw JSON toggle
- [ ] Page is accessible at `/schemas/infer` with route name `schema-infer`

### Onboarding wizard — `OnboardingWizard.vue` steps 2–3

- [ ] Step 2 shows selected connector name from step 1
- [ ] Step 2 resource type input with placeholder hint
- [ ] Step 2 sample query textarea (optional)
- [ ] Step 2 "Infer Schema" button disabled when resource type empty or inferring
- [ ] Step 2 calls `POST /api/v1/schemas/infer` with the pre-selected connector ID
- [ ] Step 2 displays inference error message
- [ ] Step 2 shows draft schema in a compact table (name, description, fields)
- [ ] Step 2 "No fields inferred" fallback when fields array is empty
- [ ] Step 3 shows editable schema name and description fields from draft
- [ ] Step 3 fields table is read-only with hint "re-infer to change"
- [ ] Step 3 "Confirm & Save Schema" button calls `POST /api/v1/schemas`
- [ ] Step 3 displays save error message
- [ ] Step 3 displays success message with published schema ID
- [ ] Step progression: canProceed for step 2 requires draftSchema, step 3 requires publishedSchemaId

### API types and client — `schema.ts`

- [ ] `SchemaInferRequest` defines connector_instance_id (string), resource_type (string), sample_query (optional string)
- [ ] `SchemaFieldDefinition` defines name (string), type (string), required (boolean), description (optional string)
- [ ] `SchemaInferResponse` defines name (string), description (optional string), fields (SchemaFieldDefinition[])
- [ ] `SchemaCreateRequest` defines name (string), description (optional string), fields (SchemaFieldDefinition[])
- [ ] `SchemaCreateResponse` defines id (string), name (string)
- [ ] `POST /api/v1/schemas/infer` endpoint typed in paths
- [ ] `POST /api/v1/schemas` endpoint typed in paths

### Error handling and edge cases

- [ ] Network errors during connector loading show user-visible error text
- [ ] Network errors during inference show user-visible error text
- [ ] Network errors during publish/save show user-visible error text
- [ ] Empty connector list shows guidance ("Create one first")
- [ ] Empty fields array shows "No fields inferred" instead of empty table
- [ ] Null description on draft or fields renders dash placeholder ("—")
- [ ] API errors propagate the err message directly to the user
- [ ] Loading state prevents double-submit on inference and publish buttons

### Missing — not yet implemented

- [ ] Schema editor component: operator cannot rename fields, adjust types, toggle required, or edit field descriptions
- [ ] Frontend unit tests for SchemaInferenceView or OnboardingWizard schema steps
- [ ] BDD feature file has zero scenarios (TODO placeholder)
- [ ] No `abstract_name` display or filter in library browse step (per 8.16 step 4)
- [ ] No rare-field flagging or exclusion summary shown in draft UI (per 8.16)
- [ ] No enum detection display for issue_type, status, priority fields
- [ ] No sample count display (PRD default 200, code caps at 50)
- [ ] No versioning display when publishing schema
- [ ] No connector-type validation feedback for unsupported connector types

## Known Gaps - **No schema editor:** PRD 8.16 says "draft opens in the schema editor for the operator to review, rename fields, adjust types". Current UI shows fields as read-only tables in both views — no inline editing, no type picker, no required toggle. OnboardingWizard step 3 explicitly labels fields as "read-only — re-infer to change".
- **No frontend tests:** Zero spec files for SchemaInferenceView or OnboardingWizard schema steps. BDD feature file is a TODO placeholder.
- **No `abstract_name` integration:** PRD 8.16 step 4 requires browsing the community library filtered by inferred `abstract_name`. The inference service doesn't return `abstract_name`, and the frontend library filter dropdown doesn't support it.
- **Sample cap mismatch:** PRD says 200 default sample records; code caps at 50 in prompt builder and 100 in API limit — no sample count displayed in UI.
- **PRD gaps cascade:** Several PRD 8.16 requirements not yet implemented in the backend (rare-field flagging, enum detection, SandboxedEnvironment, data lifecycle enforcement) cascade to the UI — no UI can surface what the backend doesn't provide. 