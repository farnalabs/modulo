---
id: feat-frontend-feedback-routing
prd: 8.20
delivery-tasks: [task-nv4-feedback-routing]
bdd:
  - backend/tests/bdd/features/eval/feedback_system.feature
  - backend/tests/bdd/features/hitl/feedback_handler.feature
code:
  - frontend/src/views/FeedbackInboxView.vue
  - frontend/src/router/index.ts
  - frontend/src/lib/api/schema.ts
  - backend/src/modulo/api/routes/feedback.py
  - backend/src/modulo/core/feedback_manager/__init__.py
  - backend/src/modulo/db/models/feedback_record.py
unit-tests:
  - backend/tests/unit/api/test_feedback_endpoint.py
  - backend/tests/unit/core/feedback_manager/test_feedback_manager.py
  - backend/tests/integration/feedback_manager/test_feedback_flow.py
depends-on: [feat-evals-feedback-records]
status: partial
---

# Feedback System — Routing

## Behaviours

### FeedbackRecord lifecycle

- [x] HITL rejection creates FeedbackRecord with status `pending`
- [x] FeedbackRecord stores rejection reason, rejected output, producing node/agent
- [x] Status state machine enforces valid transitions: pending → routing → correcting → resolved | escalated
- [x] `dismissed` is a terminal status from pending/note for future
- [x] Invalid transitions return validation error
- [x] FeedbackRecord is immutable after creation

### Handler types and routing strategy

- [x] Pipeline `default_feedback_handler` cascades to all HITL gates unless overridden
- [x] Gate-level `feedback_handler` overrides pipeline default
- [x] `human` handler: FeedbackRecord surfaced in inbox for manual review
- [x] `ai_correction` handler: auto-spawns correction run, auto-resolves if evals pass
- [x] `ai_correction_with_human_review` handler: auto-spawns correction run, marks `needs_human_review`
- [ ] `feedback_handler` supersedes `reject_target` when both set on same gate
- [ ] Setting both `feedback_handler` and `reject_target` is a validation error (`reject_routing_conflict`)

### Correction run mechanics

- [x] Correction run spawning linked to FeedbackRecord via `correction_run_id`
- [x] Correction run injects `_feedback_correction` payload into run_context
- [ ] Correction run pre-seeded with checkpoint state from target_node_id (fresh run, not seeded — known gap)
- [x] Correction run uses same PipelineSnapshot as original run
- [x] `parent_run_id` links correction run to original run
- [x] Correction run goes through full eval suite before reaching HITL gate again
- [ ] Eval failure during correction run → status set to `escalated` (known gap — currently stays in `correcting`)
- [x] `ai_correction` auto-resolves on eval pass
- [x] `ai_correction_with_human_review` marks `needs_human_review=True` on eval pass

### AI correction agent

- [ ] AI correction agent exists as a library primitive
- [ ] Agent receives: original prompt, rejected output, rejection reason, eval scores
- [ ] Agent produces: diagnosis, correction proposal, proposed new eval case
- [ ] Correction proposal injected as `feedback_correction` in run_context

### Eval suite growth

- [x] Eval gap detection retroactively runs eval suite against rejected output
- [x] Standalone `EvalEngine.evaluate()` operates outside a live LangGraph run
- [x] Records with no eval failure tagged `eval_gap`
- [ ] AI correction agent (or dedicated eval-proposal agent) drafts proposed eval case
- [ ] Proposed evals land in "Eval proposals" inbox
- [ ] Human can review, edit, and publish proposed evals
- [ ] Published evals immediately active for future runs of that pipeline
- [ ] Library contribution of curated evals (v2)

### Feedback inbox UI

- [x] Inbox shows all pending FeedbackRecords across all pipelines
- [x] Inbox filterable by status, pipeline, producing agent
- [x] Inbox filterable by date range
- [x] `human` handler: annotation UI with notes and manual correction trigger
- [x] `ai_correction_with_human_review`: correction proposal display with accept/reject
- [ ] Eval proposals queue with draft eval editor
- [x] Paginated results

### API endpoints

- [x] `POST /api/v1/runs/{run_id}/feedback` — create feedback record
- [x] `GET /api/v1/feedback` — list feedback records (paginated, filterable)
- [x] `GET /api/v1/feedback/inbox` — inbox listing with filters
- [x] `GET /api/v1/feedback/inbox/{record_id}` — inbox item detail
- [x] `POST /api/v1/feedback/inbox/{record_id}/review` — review action (annotation, resolve, trigger correction)
- [x] `PATCH /api/v1/feedback/{record_id}/status` — status update (validated transitions)
- [x] `POST /api/v1/feedback/{record_id}/detect-gap` — eval gap detection
- [x] `GET /api/v1/feedback/proposals` — eval proposals queue

### Testing

- [x] Unit tests for all API endpoints (test_feedback_endpoint.py)
- [x] Unit tests for FeedbackManager business logic (test_feedback_manager.py)
- [x] Integration tests for full feedback flow with real database
- [x] BDD: HITL rejection creates FeedbackRecord (feedback_system.feature + test_eval.py step defs)
- [x] BDD: Feedback routed per handler type (feedback_handler.feature + test_hitl.py step defs)
- [x] BDD: Correction run spawned and executes (feedback_system.feature scenario 7)
- [x] BDD: Eval gap detection triggers proposed eval generation (feedback_system.feature scenario 6)
- [x] BDD: Feedback inbox review actions complete the lifecycle (feedback_handler.feature scenario 5)

## Error Handling

### DB-backed routes (501 Not Implemented)
- [x] All 9 API route handlers in `feedback.py` catch `ProgrammingError` → 501 with descriptive message
- [x] Pattern matches AGENTS.md Lessons Learned ("every DB-backed route must catch ProgrammingError")

### 404 Not Found
- [x] `create_feedback`: run not found → 404
- [x] `get_feedback`: record not found → 404
- [x] `update_feedback_status`: record not found → 404
- [x] `detect_eval_gap`: record not found → 404 (via `get_feedback_record`)
- [x] `get_inbox_item`: record not found → 404
- [x] `review_feedback`: record not found → 404

### 422 Validation
- [x] `update_feedback_status`: invalid status string → 422
- [x] `review_feedback`: invalid action → 422
- [x] `review_feedback`: `create_correction_run` on record with no run_id → 422

### 409 Conflict
- [x] `review_feedback`: `mark_reviewed` on terminal status → 409
- [x] `review_feedback`: `dismiss` on terminal status → 409

### Exception propagation (FeedbackManager → HTTP)
- [x] `spawn_correction_run`: record not found → FeedbackRecordNotFoundError → 404
- [x] `spawn_correction_run`: original run not found → FeedbackManagerError → 404
- [x] `update_status`: invalid transition → InvalidTransitionError → 409 Conflict in review handler
- [x] `update_status`: concurrent modification → ConcurrentModificationError → 409 Conflict in review handler

### Missing error handling
- [ ] `run_post_correction_eval` exceptions (record not found, wrong status, no linked run) not caught at API level (method not yet wired into run completion lifecycle)
- [x] Date parsing in `list_feedback_inbox`: invalid `date_from`/`date_to` format caught → 422

## QA History

### 2026-07-03 — Cross-cutting QA (index 87)
- **Fixed**: Frontend `resolveRecord()` and `triggerCorrection()` sent `{ status: ... }` instead of `{ action: ... }` to review endpoint. Pydantic v2 silently dropped extra fields, defaulting `action` to `"mark_reviewed"` — causing "Trigger Correction Run" to mark as reviewed instead. Corrected all three frontend review API calls to use proper `action` field.
- **Fixed**: Backend `ReviewFeedbackRequest` model lacked `annotation` field. Added the field and persistence logic in the review handler.
- **Fixed**: `FeedbackRecord` model lacked `annotation` column. Added column + Alembic migration `0059_feedback_annotation`.
- **Fixed**: Stale Known Gaps — BDD features now correctly marked as real scenarios (not placeholders).
- **Added**: Error Handling section with audited error paths.
- **Added**: Annotation serialisation in `_serialise_record()`.
- **Not fixed (requires separate task)**: `reject_routing_conflict` validation, AI correction agent primitive, eval proposals UI, checkpoint seeding, eval suite population for `detect_eval_gap`.

### 2026-07-06 — Cross-cutting QA (this session)
- **Fixed**: Backend `list_feedback_inbox` — invalid `date_from`/`date_to` ISO format now returns 422 instead of uncaught ValueError → 500.
- **Fixed**: Backend `review_feedback` — `InvalidTransitionError` and `ConcurrentModificationError` from `update_status` caught and returned as 409 instead of propagating as 500.
- **Fixed**: Backend `review_feedback` — `spawn_correction_run` exception handler changed from dead `except ValueError` to `except (FeedbackRecordNotFoundError, FeedbackManagerError)` → 404.
- **Fixed**: Frontend `FeedbackInboxView` — all error/success messages now use `$t()` / `t()` i18n keys instead of hardcoded English strings.
- **Fixed**: Frontend `FeedbackInboxView` — `openapi-fetch` error objects now rendered via `formatApiError()` instead of raw `${err}` (was producing `[object Object]`).
- **Added**: Website docs stub for feedback routing at `Website/modulo-website/src/docs/feedback-routing.md`.
- **Updated**: Product map entries for fixed error handling gaps.

## Known Gaps

- **Correction run checkpoint seeding**: Correction run creates a fresh run rather
  than seeding from original LangGraph checkpoint at target_node_id
  (per PRD 8.20: "pre-seeded with checkpoint state")
- **No AI correction agent library primitive**: PRD 8.20 describes an agent that
  produces diagnosis + correction proposal + proposed eval case, but no code
  exists for it
- **No `reject_routing_conflict` validation**: Gate-level validation for setting
  both `feedback_handler` and `reject_target` on the same gate not yet implemented
- **No eval proposals UI**: Eval proposals queue with draft eval editor
  (PRD 8.20 ¶1495) not yet built
- **detect_eval_gap hardcodes eval_suite=[]**: The API endpoint at
  `POST /feedback/{id}/detect-gap` passes `eval_suite=[]` instead of reading
  the pipeline's eval suite, so gap detection always returns `False` when no
  eval suite is explicitly provided.
- **`dismissed` not fully wired as terminal status**: The status machine
  allows `pending→dismissed` and `escalated→dismissed`, but the UI does not
  display a dismiss action button.
- **Eval failure during correction does NOT escalate**: Per PRD §8.20, eval
  failure should set status to `escalated`, but the current implementation
  keeps the record in `correcting`.
