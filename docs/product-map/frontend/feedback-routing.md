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
depends-on: [feat-evals-feedback-records]
status: partial
---

# Feedback System — Routing

Feedback routing layer: handler dispatch (human, ai_correction,
ai_correction_with_human_review), correction run lifecycle, and eval gap
detection for HITL rejection feedback. The frontend inbox UI is documented
in `feat-frontend-feedback-inbox-ui`.

## Behaviours

### FeedbackRecord lifecycle

- [x] HITL rejection creates FeedbackRecord with status `pending`
- [x] FeedbackRecord stores rejection reason, rejected output, producing node/agent
- [x] Status state machine enforces valid transitions: pending → routing | correcting | resolved | dismissed, routing → escalated | correcting | resolved, correcting → correcting | resolved | escalated, escalated → resolved | dismissed, resolved terminal, dismissed terminal (DB CHECK constraint: pending, routing, correcting, resolved, escalated, dismissed)
- [x] Invalid transitions return validation error
- [x] FeedbackRecord is immutable after creation
- [x] `dismissed` is a valid DB status — `dismiss` action sets status to `dismissed` (migration 0082 added to CHECK constraint)

### Handler types and routing strategy

- [x] Pipeline `default_feedback_handler` cascades to all HITL gates unless overridden
- [x] Gate-level `feedback_handler` overrides pipeline default
- [x] `human` handler: FeedbackRecord surfaced in inbox for manual review
- [x] `ai_correction` handler: auto-spawns correction run, auto-resolves if evals pass
- [x] `ai_correction_with_human_review` handler: auto-spawns correction run, marks `needs_human_review`
- [ ] Setting both `feedback_handler` and `reject_target` is a validation error (`reject_routing_conflict`)

### Correction run mechanics

- [x] Correction run spawning linked to FeedbackRecord via `correction_run_id`
- [x] Correction run injects `_feedback_correction` payload into run_context
- [ ] Correction run pre-seeded with checkpoint state from target_node_id (fresh run, not seeded — known gap)
- [x] Correction run uses same PipelineSnapshot as original run
- [x] `parent_run_id` links correction run to original run
- [x] Correction run goes through full eval suite before reaching HITL gate again
- [x] Eval failure during correction run → status set to `escalated` (run_post_correction_eval escalates via _escalate_record)
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

<!-- Inbox UI behaviours are documented in feat-frontend-feedback-inbox-ui -->

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

### Generic Exception → 500 guard (added 2026-07-08)
- [x] `create_feedback` — catches Exception → 500
- [x] `list_feedback` — catches Exception → 500
- [x] `list_feedback_inbox` — catches Exception → 500
- [x] `list_eval_proposals` — catches Exception → 500
- [x] `get_feedback` — catches Exception → 500
- [x] `update_feedback_status` — catches Exception → 500
- [x] `detect_eval_gap` — catches Exception → 500
- [x] `get_inbox_item` — catches Exception → 500
- [x] `review_feedback` — catches Exception → 500

### Missing error handling
- [ ] `run_post_correction_eval` exceptions (record not found, wrong status, no linked run) not caught at API level (method not yet wired into run completion lifecycle)
- [x] Date parsing in `list_feedback_inbox`: invalid `date_from`/`date_to` format caught → 422

## Edge Cases

- [x] Concurrent modification during status update returns 409 Conflict
- [x] Invalid status transitions return validation error
- [x] FeedbackRecord is immutable after creation
- [x] Review action on terminal status returns 409 Conflict
- [x] Dismiss on terminated record returns 409 Conflict
- [x] `create_correction_run` on record with no run_id returns 422
- [x] Invalid date format for date range filter returns 422
- [x] HITL gate with `human_only` returns 403 on MCP approve
- [ ] Correction run spawned while pipeline is deleted mid-flight — no graceful handling
- [ ] Network failure during correction run spawn — error propagated but no retry
- [ ] Concurrent correction run spawns from two review actions on same record — no dedup
- [ ] Empty or null rejection reason/output in feedback record — fallback text exists per UI
- [ ] User reviews record while correction run is in-flight — status transition may be blocked
- [ ] Feedback for deleted pipeline — `spawn_correction_run` returns 404

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

- **`run_post_correction_eval` escalates correctly but isn't wired**: The
  method escalates on eval failure via `_escalate_record` (status → `escalated`),
  but is not yet wired into the run completion lifecycle. The escalation logic
  exists but is never triggered automatically in production.

## QA History

### 2026-07-03 — Cross-cutting QA (index 87)
- **Fixed**: Frontend review API calls now use `action` field (was `status`). Backend `ReviewFeedbackRequest` and `FeedbackRecord` gained `annotation` field + migration. Stale Known Gaps corrected. Error Handling section added.

### 2026-07-06 — Cross-cutting QA
- **Fixed**: Backend error handling — date parsing → 422, invalid transition/concurrent mod → 409, spawn exception mapping → 404. Frontend i18n and `formatApiError()` applied. Product map entries updated.

### 2026-07-08 — Cross-cutting QA (improve-architecture index 275)

**CRITICAL fixes applied:**
- Frontend `FeedbackInboxView.vue` field name mismatches: `record.status` → `record.feedback_status`, `record.handler_type` → `record.feedback_handler_type`, `detailMap[id].status` → `detailMap[id].feedback_status`, `rec.status` → `rec.feedback_status` in resolve/trigger handlers. The API returns `feedback_status` and `feedback_handler_type` from `_serialise_record()` — the frontend was referencing nonexistent fields, causing status badges, handler type badges, and conditional "Trigger Correction Run" button to never work. Also removed dead `record.summary` reference (API does not return a `summary` field).
- Added `except Exception → 500` catches (with `except HTTPException: raise` guard) to all 9 feedback routes in `feedback.py` — Python-level errors (TypeError, KeyError, ValueError) from model_validate, dict processing, etc. previously propagated as opaque 500 to CatchAllMiddleware.

**MAJOR fixes applied:**
- Annotation UPDATE query in `review_feedback` route added `FeedbackRecord.organisation_id == principal.organisation_id` filter — on non-Postgres backends (SQLite/MariaDB), the `_inject_tenant_filter` may not inject on explicit `update()` statements, risking cross-org annotation writes.
- Moved `from sqlalchemy import update as sa_update` and `from modulo.db.models.feedback_record import FeedbackRecord` from lazy method-body imports to module level in `feedback.py`.
- Added `logger` to `feedback.py` for structured error logging.

**Product map corrections:**
- Corrected DB Model section — `dismissed` is NOT in the CHECK constraint (actual: `pending, routing, correcting, resolved, escalated`). Removed `dismissed` from the valid status list. The `dismiss` action correctly maps to `resolved` because DB doesn't allow `dismissed`.
- Added Error Handling subsection documenting the new `except Exception → 500` guard on all 9 routes.
- Added 9 new `[x]` behaviour checkboxes for Exception → 500 generic guard coverage on all feedback routes.

### 2026-07-10 — Cross-cutting QA (index 312)

**Product map corrections:**
- Reverted the "dismissed is not valid" claim — migration 0082 added `dismissed` to the CHECK constraint. Updated behaviour checkbox from `[ ]` to `[x]`. Status transitions now include `pending→dismissed` and `escalated→dismissed`.
- Updated "eval failure doesn't escalate" checkbox from `[ ]` to `[x]` — `run_post_correction_eval` DOES escalate via `_escalate_record`. Rephrased Known Gap: escalation logic exists but `run_post_correction_eval` is not yet wired into the run completion lifecycle.
- All i18n keys in FeedbackInboxView.vue verified present in en-US.js.
- Frontend error handling, loading, and empty states confirmed correct.

**Status:** partial (4 known gaps remain — no pagination controls in UI, no eval proposals UI, no status staleness handling, no `ai_correction_with_human_review` accept/reject UI). `dismissed` status gap resolved (migration 0082). Eval escalation gap resolved (run_post_correction_eval escalates, but not yet wired into lifecycle). `formatDate` hardcoded gap resolved (now uses `locale.value` from `useI18n()`). Maxlength validation gap resolved (added `maxlength="2000"` on annotation textarea). Retry button on main list gap resolved (added `<ErrorAlert :on-retry="loadFeedback">`).

### 2026-07-09 — Second-pass QA (frontend docs)

**Documentation drift fixes:**
- Corrected stale gap summary: removed `formatDate` hardcoded (fixed per inbox UI QA), maxlength validation (fixed per inbox UI QA), and retry button on main list (fixed per inbox UI QA) from gap count — 7→4 remaining gaps.
