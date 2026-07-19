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
- [x] `dismiss` action sends `action: dismiss` via POST review endpoint, sets status to `dismissed`

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

- [x] Main list fetch failure shows `<ErrorAlert>` with error message and Retry button
- [x] Detail load failure shows inline error with Retry button
- [x] "Save Annotation" failure shows error message inline, buttons re-enabled
- [x] "Mark Resolved" failure shows error message inline, buttons re-enabled
- [x] "Trigger Correction Run" failure shows error message inline, button re-enabled
- [x] "Dismiss" failure shows error message inline, button re-enabled
- [x] Pipeline load failure shows inline destructive alert with error message and Retry button
- [x] Main list has Retry button on error via `ErrorAlert` `:on-retry` prop

## Edge Cases

- [x] Empty feedback list shows empty state with icon and explanatory text
- [x] Detail loading shows spinner while fetching
- [x] Concurrent operations prevented via disabled state on action buttons
- [x] Annotation textarea has `maxlength="2000"` as client-side hint (backend uses Text column, unlimited)
- [ ] No status staleness handling — if another session resolves a record, user's action may get 409
- [ ] No producing_agent filter in frontend (API schema includes `agent_id` but UI does not expose it)
- [ ] No ai_correction_with_human_review accept/reject UI (PRD requires it for that handler type)
- [ ] No pagination controls — all records loaded on one page
- [x] `formatDate` uses `locale.value` from `useI18n()` with invalid-date guard

## Known Gaps

- Pagination controls not wired in the frontend (API supports `page`/`page_size`)
- No eval proposals queue or draft eval editor UI (required by PRD 8.20)
- No `producing_agent` filter in frontend (API schema includes `agent_id` but UI does not expose it)
- No `ai_correction_with_human_review` accept/reject UI (PRD requires it for that handler type)
- "Save Annotation" and "Mark Resolved" buttons send identical API payloads (`action: mark_reviewed`) — no semantic difference exists in the API for saving annotation without resolving
- No explicit API request timeouts

## QA History

### 2026-07-04 — Cross-cutting QA (index 130)
- **Fixed**: 39 behaviour checkboxes verified, review schema corrected to use `action` field, stale BDD placeholder gap removed, 18 i18n wrappers added, Error Handling + Edge Cases sections added.
- **Noted**: Save Annotation and Mark Resolved send identical payload — no API distinction exists for "save only" vs "save and resolve."

### 2026-07-09 — Cross-cutting QA (index 279)
- **Fixed MAJOR**: `formatDate` hardcoded to `'en-US'` — now uses `locale.value` from `useI18n()`. Added `isNaN(d.getTime())` guard for invalid date strings.
- **Fixed MAJOR**: Added Dismiss button with proper `action: dismiss` backend call. Was defined in API (backend supports it at feedback.py:538) and i18n key existed but no UI button was wired. Sends `POST /api/v1/feedback/inbox/{record_id}/review` with `{ action: 'dismiss' }`, sets row status to `dismissed`.
- **Fixed MAJOR**: `loadPipelines()` failure now sets `pipelinesError` ref shown as inline destructive alert — previously silently swallowed errors with only `console.warn`. Pipeline filter stays empty but user sees error message.
- **Fixed MINOR**: Added `maxlength="2000"` on annotation textarea (client-side hint; backend uses `Text` column with no limit).
- **Fixed MINOR**: Added 4 new i18n keys: `dismiss_failed`, `dismissed`, `dismissing`, `failed_to_load_pipelines`.
- **Added product map**: Dismiss action behaviour checkbox, annotation maxlength edge case as [x].

### 2026-07-10 — Cross-cutting QA (index 300)
- **Fixed CRITICAL**: DB CHECK constraint `ck_feedback_records_status` excluded `'dismissed'` — every dismiss call to `POST /feedback/inbox/{record_id}/review` with `action: dismiss` crashed with `IntegrityError`. Created migration `0082_feedback_dismissed_status` to add `'dismissed'` to the allowed statuses. Updated model CHECK constraint to match.
- **Fixed MAJOR**: Main list error state now shows a Retry button via `<ErrorAlert :on-retry="loadFeedback">` — previously the error was displayed inline with no recovery action other than manual page reload.
- **Removed Known Gap**: "No retry button on main list fetch error" — now resolved.
