---
id: feat-core-core
prd: 8.16, 8.4
delivery-tasks: [task-nv5-sdlc-onboarding-path]
bdd:
  - backend/tests/bdd/features/orgs/org_onboarding.feature
unit-tests:
  - backend/tests/unit/api/test_onboarding.py
code:
  - backend/src/modulo/api/routes/onboarding.py
  - frontend/src/views/OnboardingWizard.vue
depends-on: [feat-core-schema-inference-ui, feat-core-replace-step-agent]
status: partial
---
# SDLC Onboarding

Existing SDLC onboarding: teams can map their current process (even manual steps) into Modulo pipelines and progressively replace steps with AI agents — no big-bang replacement required. Discovered from 1 completed delivery task.

## Behaviours

### PRD 8.16 — SDLC Onboarding Path (5 steps)

- [ ] Connect tools (Jira, GitHub, Notion) from the onboarding wizard
- [ ] Run schema inference on each resource type to get draft schemas
- [ ] Review and publish schemas from the inferred draft
- [ ] Browse the community library filtered by inferred data shape
- [ ] Wire agents together into a pipeline against real schemas
- [ ] Complete the full path in a single session turning an existing SDLC into a running pipeline ### PRD 8.4 — Manual Nodes as Onboarding Tool - [ ] Team models existing process using manual placeholder nodes
- [ ] Pipeline runs as governed, observable SDLC record with no AI agents
- [ ] Steps are progressively replaced with agent nodes as automation is added
- [ ] Revert agent nodes back to manual when automation underperforms
- [ ] Manual node carries output_schema_id for human output validation ### Backend — Onboarding Wizard REST API - [ ] GET /api/v1/onboarding/status returns is_first_run, completed_steps, current_step, total_steps
- [ ] Returns is_first_run=True with empty completed_steps when no state file exists
- [ ] Auto-detects first-run completion when pipelines exist (marks is_first_run=False)
- [ ] Returns current_step=null when is_first_run=False
- [ ] Returns current_step pointing to first incomplete step
- [ ] POST /api/v1/onboarding/step marks a step completed
- [ ] Duplicate step marking is idempotent
- [ ] Auto-clears is_first_run when all 4 steps are completed
- [ ] Returns 422 for invalid step_id
- [ ] Returns 401/403 for unauthenticated requests
- [ ] GET /api/v1/onboarding/step/{step_id} returns step metadata and data
- [ ] connect_tools step returns connector definitions (GitHub, Jira, Linear)
- [ ] select_template step loads templates from LibraryPrimitive
- [ ] configure_agent step returns static guidance
- [ ] run_demo step returns static guidance
- [ ] Returns 404 for unknown step_id
- [ ] Persists state to .onboarding-state.json on each mutation ### Frontend — Onboarding Wizard - [ ] 7-step wizard with numbered step indicator showing progress
- [ ] Step 0 (Welcome): overview of 6 quick steps with summary list
- [ ] Step 1 (Connect Tools): loads connectors from GET /api/v1/connectors
- [ ] Step 1 shows loading spinner while connectors load
- [ ] Step 1 shows error text when connector loading fails
- [ ] Step 1 shows "No connectors found" guidance when list is empty
- [ ] Step 1 allows selecting a connector instance via radio-button cards
- [ ] Step 1 canProceed requires a connector to be selected
- [ ] Step 2 (Run Inference): shows selected connector name in a badge
- [ ] Step 2 provides resource type text input
- [ ] Step 2 provides optional sample query textarea
- [ ] Step 2 "Infer Schema" button disabled when resource type empty or inferring
- [ ] Step 2 calls POST /api/v1/schemas/infer with connector ID and resource type
- [ ] Step 2 displays inference error text
- [ ] Step 2 shows draft schema as a fields table (name, type, required, description)
- [ ] Step 2 shows "No fields inferred" when fields array is empty
- [ ] Step 2 canProceed requires draftSchema to be populated
- [ ] Step 3 (Review Schemas): editable schema name and description
- [ ] Step 3 fields table is read-only with "re-infer to change" hint
- [ ] Step 3 "Confirm & Save Schema" calls POST /api/v1/schemas
- [ ] Step 3 displays save error text
- [ ] Step 3 displays success message with schema name
- [ ] Step 3 canProceed requires publishedSchemaId to be set
- [ ] Step 4 (Browse Library): loads library items from GET /api/v1/libraries
- [ ] Step 4 shows loading spinner while library loads
- [ ] Step 4 shows error text when loading fails
- [ ] Step 4 shows "No library items" guidance when empty
- [ ] Step 4 provides text filter and type dropdown filter
- [ ] Step 4 renders items as selectable cards (name, type badge, description, tags)
- [ ] Step 4 canProceed always true (optional step)
- [ ] Step 5 (Wire Pipeline): pipeline name and description inputs
- [ ] Step 5 shows selected library item when one was chosen
- [ ] Step 5 "Create Pipeline" disabled when name empty or creating
- [ ] Step 5 calls POST /api/v1/pipelines
- [ ] Step 5 displays create error text
- [ ] Step 5 displays success message with pipeline name
- [ ] Step 5 canProceed requires createdPipelineId
- [ ] Step 6 (Done): summary of what was accomplished
- [ ] Step 6 provides "Run Pipeline Now" and "Go to Dashboard" buttons
- [ ] Step 6 calls POST /api/v1/pipelines/{id}/run on run click
- [ ] Step 6 displays run started success message
- [ ] Step 6 displays run error text
- [ ] Previous/Next navigation with canProceed validation
- [ ] "Skip to end" button skips to step 6
- [ ] Lazy loads connectors on step 1 and library on step 4 via watchers
- [ ] Connectors pre-loaded on mount
- [ ] Loading, error, and empty states handled for all async data
- [ ] All API errors surface user-visible text
- [ ] Button disabled states prevent double-submit ### Error States & Edge Cases - [ ] Returns 401 when no valid auth token is present
- [ ] Returns 422 when marking completion for unknown step_id
- [ ] Returns 404 when GET step data for unknown step_id
- [ ] Duplicate step completion is idempotent
- [ ] First run auto-detected by checking existing pipelines
- [ ] All-steps-completed auto-clears first_run flag
- [ ] Network errors surface user-visible error text throughout wizard ## Known Gaps - **PRD 8.16 5-step path not fully integrated:** The onboarding wizard implements a simplified version. Steps 4 (library browse filtered by abstract_name) and step 5 (wire agents) are generic rather than specifically guided by inferred schema shape. No schema-inferred abstract_name is used to filter library recommendations.
- **No integrated BDD scenarios:** `backend/tests/bdd/features/orgs/org_onboarding.feature` is a placeholder with no real scenarios. `backend/tests/bdd/steps/test_alpha_mcp.py` references `onboarding.feature` under `backend/tests/bdd/features/mcp/` which does not exist.
- **No persona feature file:** Alice persona scenarios (`@goal-alice-onboard-sdlc`, `@goal-alice-replace-step`) exist in the persona doc but have no corresponding Gherkin scenarios or step definitions.
- **`depends-on` references feature IDs** (`feat-core-schema-inference-ui`, `feat-core-replace-step-agent`) — previously pointed to raw task IDs.
- **Demo pipeline + first-run walkthrough (Phase 5, item 25):** `MODULO_DEMO_MODE` and pre-loaded `prd-to-requirements` demo not yet built per the delivery tracker.
- **No E2E test** covering the full connect→infer→review→library→wire→run flow.
- **Backend step data simplified:** PRD 8.16 mentions Notion/Confluence as document-store connectors in addition to Jira/Linear/GitHub; `connect_tools` step data only lists GitHub, Jira, Linear. 