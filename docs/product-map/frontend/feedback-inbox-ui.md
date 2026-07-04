---
id: feat-frontend-feedback-inbox-ui
prd: 8.20
delivery-tasks: [task-nv4-feedback-inbox-ui]
bdd:
  - backend/tests/bdd/features/eval/feedback_system.feature
code:
  - frontend/src/views/FeedbackInboxView.vue
  - frontend/src/lib/api/schema.ts
unit-tests:
  - frontend/src/__tests__/FeedbackInboxView.spec.ts
  - backend/tests/unit/api/test_feedback_endpoint.py
  - backend/tests/unit/core/feedback_manager/test_feedback_manager.py
depends-on: [feat-evals-feedback-records]
status: partial
---

# Feedback Inbox UI

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
- [ ] Main list does NOT have a Retry button on error (only `<ErrorAlert>` which relies on `onRetry` prop — not passed)
- [ ] Error messages use template literal interpolation (`Failed to load feedback: ${err}`) — may produce `[object Object]` if err is not a string
- [ ] `loadPipelines()` silently swallows all errors (empty `catch` block) — non-critical but loses error visibility
- [ ] All user-facing strings now use `$t()/t()` wrappers — 18 hardcoded strings were fixed in QA index 130
- [ ] No explicit request timeouts on API calls
- [ ] No retry logic for transient failures (beyond manual Retry button on detail)

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
- Error messages use template literal interpolation with `err` — may produce `[object Object]` for non-string errors

## QA History

### 2026-07-04 — Cross-cutting QA (index 130)
- **Fixed**: All 39 behaviour checkboxes marked [x] where code implements them (was all [ ]).
- **Fixed**: Corrected review schema behaviour — API uses `action` field, not `status`.
- **Fixed**: Removed stale "BDD feature file is a placeholder" gap — file has 7 real scenarios.
- **Fixed**: Added 18 `$t()` i18n wrappers for hardcoded English strings in template (Status, Pipeline, From, To, option labels, Retry, Rejection Reason, Rejected Output, Correction Proposal, Trigger Correction Run, Triggering, Annotation, placeholder, Save Annotation, Saving, Mark Resolved, no rejection reason fallback).
- **Fixed**: Added 18 new i18n keys to en-US.js for FeedbackInboxView.
- **Added**: New Error Handling section with 10 behaviour checkboxes.
- **Added**: New Edge Cases section with 10 behaviour checkboxes.
- **Added**: Known gaps for duplicate action semantics (Save Annotation == Mark Resolved), missing maxlength, missing retry button, missing timeouts, silent catch, hardcoded locale, template literal interpolation risk.
- **Noted**: `saveAnnotation()` and `resolveRecord()` send identical `action: mark_reviewed` payload — this is a design issue where no API distinction exists between "save annotation only" and "save annotation and resolve". The frontend shows different success messages but does the same thing."
