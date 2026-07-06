---
id: feat-frontend-feedback-inbox-ui
prd: 8.20
delivery-tasks: [task-nv4-feedback-inbox-ui]
bdd:
  - backend/tests/bdd/features/eval/feedback_system.feature
code:
  - frontend/src/views/FeedbackInboxView.vue
unit-tests:
  - frontend/src/__tests__/FeedbackInboxView.spec.ts
  - backend/tests/unit/api/test_feedback_endpoint.py
  - backend/tests/unit/core/feedback_manager/test_feedback_manager.py
depends-on: [feat-evals-feedback-records]
status: partial
---

# Feedback Inbox UI

Frontend inbox for reviewing and managing HITL feedback records across all
pipelines. Supports status filtering, date range filtering, annotation, manual
correction trigger, and resolution.

## Behaviours

### Listing and filtering

- [x] Lists all pending FeedbackRecords across all pipelines on load
- [x] Filters by status: All, Pending, Routing, Correcting, Resolved, Escalated
- [x] Filters by pipeline (dropdown populated from `/api/v1/pipelines`)
- [x] Filters by date range (From/To date inputs)
- [x] Query params sent to `GET /api/v1/feedback/inbox` for all active filters
- [x] Re-fetches list when any filter changes
- [x] Shows loading spinner during fetch
- [x] Shows error message with inline retry on fetch failure
- [x] Shows empty state with message-bubble icon and "No feedback yet" text when no records
- [x] Displays pipeline name, rejection reason/summary, status badge, created date, handler_type per row

### Expandable detail

- [x] Clicking a row expands to show detail (chevron rotates 90°)
- [x] Collapses on second click
- [x] Loads detail via `GET /api/v1/feedback/inbox/{record_id}` on first expand
- [x] Shows inline spinner during detail load
- [x] Shows inline error with Retry button on detail load failure
- [x] Caches loaded detail per record (no re-fetch on collapse/re-expand)

### Detail content

- [x] Displays rejection reason (or "No rejection reason provided.")
- [x] Displays rejected output as formatted JSON in a `<pre>` block (max-height scrollable)
- [x] Conditionally shows correction proposal as formatted JSON in blue-bordered block
- [x] Shows "Trigger Correction Run" button when status is `pending` or `routing`
- [x] "Trigger Correction Run" sends `POST /api/v1/feedback/inbox/{record_id}/review` with `action: create_correction_run`
- [x] Button shows "Triggering..." and is disabled during request
- [x] On success: updates detail, shows success message, updates row status badge to `correcting`
- [x] On failure: shows error message, button re-enabled

### Annotation

- [x] Textarea for annotation visible in detail for every record
- [x] "Save Annotation" button sends `POST` with `action: mark_reviewed` + annotation body
- [x] "Mark Resolved" button sends `POST` with `action: mark_reviewed` + annotation body
- [x] Buttons show "Saving..." and are disabled during request
- [x] On save success: shows green success message, auto-dismisses after 3s
- [x] On resolve success: updates row status badge to `resolved`
- [x] On failure: shows red error message, buttons re-enabled

### Feedback review request schema

- [x] Review POST body supports optional `annotation` field
- [x] Review POST body uses `action` field (`mark_reviewed`, `dismiss`, `create_correction_run`) — not `status`

### Pagination

- [x] API schema includes `page` and `page_size` query params
- [ ] Frontend does not yet wire pagination controls (gap)

### Eval proposals (not yet implemented in UI)

- [x] API endpoint `GET /api/v1/feedback/proposals` exists in schema
- [ ] No frontend UI for eval proposals queue or draft eval editor (gap)

### BDD coverage

- [x] BDD feature file exists at `backend/tests/bdd/features/eval/feedback_system.feature`
- [x] Feature file has 7 real scenarios (create, status transitions, invalid transitions, eval gap, correction run spawning) — not a placeholder

## Error Handling

- [x] Main list fetch failure shows `<ErrorAlert>` with error message
- [x] Detail load failure shows inline error with Retry button
- [x] "Save Annotation" failure shows error message inline, buttons re-enabled
- [x] "Mark Resolved" failure shows error message inline, buttons re-enabled
- [x] "Trigger Correction Run" failure shows error message inline, button re-enabled
- [ ] Main list does NOT have a Retry button on error (Known Gap — see below)

## Edge Cases

- [x] Empty feedback list shows empty state with icon and explanatory text
- [x] Detail loading shows spinner while fetching
- [x] Concurrent operations prevented via disabled state on action buttons
- [ ] No maxlength validation on annotation textarea (potential DB column overflow)
- [ ] No status staleness handling — if another session resolves a record, user's action may get 409
- [ ] No producing_agent filter in frontend (API schema includes `agent_id` but UI does not expose it)
- [ ] No ai_correction_with_human_review accept/reject UI (PRD requires it for that handler type)
- [ ] No pagination controls — all records loaded on one page
- [ ] `formatDate` hardcoded to `'en-US'` locale — should use i18n locale

## Known Gaps

- Pagination controls not wired in the frontend (API supports `page`/`page_size`)
- No eval proposals queue or draft eval editor UI (required by PRD 8.20)
- No `producing_agent` filter in frontend (API schema includes `agent_id` but UI does not expose it)
- No `ai_correction_with_human_review` accept/reject UI (PRD requires it for that handler type)
- "Save Annotation" and "Mark Resolved" buttons send identical API payloads (`action: mark_reviewed`) — no semantic difference exists in the API for saving annotation without resolving
- No maxlength validation on annotation textarea
- No retry button on main list fetch error
- No explicit API request timeouts
- `loadPipelines()` catch block silently swallows all errors
- `formatDate()` hardcoded to `'en-US'` locale instead of using i18n locale

## QA History

### 2026-07-04 — Cross-cutting QA (index 130)
- **Fixed**: 39 behaviour checkboxes verified, review schema corrected to use `action` field, stale BDD placeholder gap removed, 18 i18n wrappers added, Error Handling + Edge Cases sections added.
- **Noted**: Save Annotation and Mark Resolved send identical payload — no API distinction exists for "save only" vs "save and resolve."
