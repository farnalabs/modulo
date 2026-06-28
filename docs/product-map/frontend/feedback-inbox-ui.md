---
id: feat-frontend-feedback-inbox-ui
prd: 8.20
delivery-tasks: [task-nv4-feedback-inbox-ui]
bdd: backend/tests/bdd/features/eval/feedback_system.feature
code:
  - frontend/src/views/FeedbackInboxView.vue
  - frontend/src/lib/api/schema.ts
depends-on: [feat-evals-feedback-records]
status: partial
---
# Feedback Inbox UI ## Behaviours ### Listing and filtering
- [ ] Lists all pending FeedbackRecords across all pipelines on load
- [ ] Filters by status: All, Pending, Routing, Correcting, Resolved, Escalated
- [ ] Filters by pipeline (dropdown populated from `/api/v1/pipelines`)
- [ ] Filters by date range (From/To date inputs)
- [ ] Query params sent to `GET /api/v1/feedback/inbox` for all active filters
- [ ] Re-fetches list when any filter changes
- [ ] Shows loading spinner during fetch
- [ ] Shows error message with inline retry on fetch failure
- [ ] Shows empty state with message-bubble icon and "No pending feedback" text when no records
- [ ] Displays pipeline name, rejection reason/summary, status badge, created date, handler_type per row ### Expandable detail
- [ ] Clicking a row expands to show detail (chevron rotates 90°)
- [ ] Collapses on second click
- [ ] Loads detail via `GET /api/v1/feedback/inbox/{record_id}` on first expand
- [ ] Shows inline spinner during detail load
- [ ] Shows inline error with Retry button on detail load failure
- [ ] Caches loaded detail per record (no re-fetch on collapse/re-expand) ### Detail content
- [ ] Displays rejection reason (or "No rejection reason provided.")
- [ ] Displays rejected output as formatted JSON in a `<pre>` block (max-height scrollable)
- [ ] Conditionally shows correction proposal as formatted JSON in blue-bordered block
- [ ] Shows "Trigger Correction Run" button when status is `pending` or `routing`
- [ ] "Trigger Correction Run" sends `POST /api/v1/feedback/inbox/{record_id}/review` with `status: correcting`
- [ ] Button shows "Triggering..." and is disabled during request
- [ ] On success: updates detail, shows success message, updates row status badge to `correcting`
- [ ] On failure: shows error message, button re-enabled ### Annotation
- [ ] Textarea for annotation visible in detail for every record
- [ ] "Save Annotation" button sends `POST` with annotation body (no status change)
- [ ] "Mark Resolved" button sends `POST` with `status: resolved` + annotation
- [ ] Buttons show "Saving..." and are disabled during request
- [ ] On save success: shows green success message, auto-dismisses after 3s
- [ ] On resolve success: updates row status badge to `resolved`
- [ ] On failure: shows red error message, buttons re-enabled ### Feedback review request schema
- [ ] Review POST body supports optional `annotation` field
- [ ] Review POST body supports optional `status` field (e.g. `resolved`, `correcting`) ### Pagination
- [ ] API schema includes `page` and `page_size` query params
- [ ] Frontend does not yet wire pagination controls (gap) ### Eval proposals (not yet implemented in UI)
- [ ] API endpoint `GET /api/v1/feedback/proposals` exists in schema
- [ ] No frontend UI for eval proposals queue or draft eval editor (gap) ### BDD coverage
- [ ] BDD feature file exists at `backend/tests/bdd/features/eval/feedback_system.feature`
- [ ] Feature file is a placeholder with no real scenarios (gap) ## Known Gaps
- Pagination controls not wired in the frontend (API supports `page`/`page_size`)
- No eval proposals queue or draft eval editor UI (required by PRD 8.20)
- BDD feature file is a placeholder — no scenarios defined
- No `producing_agent` filter in frontend (API schema includes `agent_id` but UI does not expose it)
- No `ai_correction_with_human_review` accept/reject UI (PRD requires it for that handler type) 