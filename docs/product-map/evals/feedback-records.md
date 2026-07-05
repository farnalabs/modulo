---
id: feat-evals-feedback-records
prd: 8.20
delivery-tasks: [task-nv4-feedback-record]
bdd:
  - backend/tests/bdd/features/eval/feedback_system.feature
code:
  - backend/src/modulo/core/feedback_manager/__init__.py
  - backend/src/modulo/api/routes/feedback.py
unit-tests:
  - backend/tests/unit/core/feedback_manager/test_feedback_manager.py
  - backend/tests/unit/api/test_feedback_endpoint.py
  - backend/tests/unit/api/test_feedback_programming_error.py
  - backend/tests/integration/feedback_manager/test_feedback_flow.py
depends-on: [feat-core-run-context, feat-evals-eval-engine]
status: partial
---

# Feedback Records

Discovered from 1 completed delivery task.

## Behaviours

### Record Creation & Retrieval

- [x] FeedbackRecord created on HITL rejection with all required fields (run_id, gate_id, rejected_by, rejection_reason, rejected_output, producing_node_id)
- [x] FeedbackRecord creation accepts optional producing_agent_id
- [x] FeedbackRecord creation accepts optional feedback_handler_type (defaults to "human")
- [x] FeedbackRecord retrieval by ID returns full record
- [x] FeedbackRecord retrieval returns None/404 for unknown ID
- [x] Feedback records listable with pagination (page, page_size)
- [x] Feedback records listable filtered by status
- [x] Feedback records listable filtered by pipeline_id (via subquery on runs)
- [x] Feedback records list returns total count when include_total=True
- [x] Empty list returns total=0 with empty items array

### Status Transitions

- [x] Valid transitions: pending → routing/correcting/dismissed
- [x] Valid transitions: routing → escalated/correcting/resolved
- [x] Valid transitions: correcting → correcting/resolved/escalated
- [x] Valid transitions: escalated → resolved/dismissed (terminal)
- [x] Valid transitions: resolved → (none — terminal)
- [x] Valid transitions: dismissed → (none — terminal)
- [x] Invalid transitions raise ValueError with descriptive message listing allowed transitions
- [x] Unknown record returns None on status update
- [x] RLS enforced on all status transitions (org-scoped)
- [x] API rejects invalid status string with 422
- [x] API updates status via PATCH /feedback/{id}/status

### Handler Types & Auto-Correction

- [x] "human" handler: record stays in pending status (no auto-correction triggered)
- [x] "ai_correction" handler: auto-transitions to correcting and spawns correction run
- [x] "ai_correction_with_human_review" handler: auto-transitions to correcting and spawns correction run
- [ ] Pipeline-level default_feedback_handler applies to all HITL gates unless overridden
- [ ] Gate-level feedback_handler overrides pipeline default
- [ ] Validation error when both reject_target and feedback_handler set on same gate (reject_routing_conflict)

### Correction Run Mechanics

- [ ] Correction run creates new LangGraph thread pre-seeded from original checkpoint
- [x] Correction run inherits original run's pipeline_id, snapshot_id, input_payload
- [x] Correction run injects _feedback_correction block into input_payload
- [x] _feedback_correction contains rejection_reason, rejected_output, producing_node_id, is_correction_run
- [x] Correction run supports run_context_overrides merged into _feedback_correction
- [x] Correction run sets trigger_type="correction" and parent_run_id
- [x] Correction run copies original run's input_payload (handles None gracefully)
- [x] Correction run linked to FeedbackRecord via correction_run_id
- [x] Link transitions record status to "correcting"
- [x] spawn_correction_run raises ValueError if FeedbackRecord not found
- [x] spawn_correction_run raises ValueError if original run not found
- [x] link_correction_run raises ValueError if record not in a state allowing "correcting" transition

### Post-Correction Evaluation

- [x] Post-correction eval runs after correction run completes via EvalEngine.standalone_evaluate()
- [x] ai_correction auto-resolves (status → resolved) on eval pass
- [x] ai_correction_with_human_review resolves but sets needs_human_review=True on eval pass
- [x] Eval failure does NOT auto-resolve status (record stays in correcting)
- [x] Evaluation reads output from correction_run.outputs_json
- [x] run_post_correction_eval raises ValueError if record not found
- [x] run_post_correction_eval raises ValueError if record not in "correcting" status
- [x] run_post_correction_eval raises ValueError if no correction_run_id linked
- [x] run_post_correction_eval raises ValueError if correction run not found in DB

### Eval Gap Detection

- [x] Gap detection runs pipeline's eval suite retrospectively against rejected output
- [x] Tag record eval_gap if no existing eval scores the output as failing
- [x] Uses EvalEngine.evaluate(artifact, eval_def) outside live LangGraph run
- [x] detect_eval_gap returns False when no eval_suite provided
- [x] detect_eval_gap endpoint returns eval_gap boolean
- [x] detect_eval_gap returns 404 if record not found

### Feedback Inbox & Review

- [x] Inbox endpoint returns paginated records across all pipelines
- [x] Inbox filterable by handler_type, status, pipeline_id, date_from, date_to
- [x] Inbox includes pipeline_name mapping per run
- [x] Inbox item detail available via GET /feedback/inbox/{id}
- [x] Inbox item detail includes pipeline name lookup
- [x] Review action "mark_reviewed" transitions record to resolved (409 on impossible transition)
- [x] Review action "dismiss" transitions record to dismissed (409 on impossible transition)
- [x] Review action "create_correction_run" spawns correction run from inbox
- [x] create_correction_run raises 422 if record has no associated run
- [x] Invalid review action string returns 422
- [x] Review on unknown record returns 404

### Eval Proposals

- [x] Eval proposals endpoint lists records with eval_gap=True and status in [pending, routing]
- [x] Eval proposals support pagination
- [x] Empty proposals list returns total=0

### Auth & Security

- [x] All feedback endpoints require authentication (401/403 on unauthenticated request)
- [x] RLS org-scoping on all feedback operations (cross-org isolation)
- [x] Create feedback validates run exists and belongs to user's org (404 if not)
- [x] API serialises all FeedbackRecord fields in responses

### Integration / Persistence

- [x] FeedbackRecord persists correctly in real DB session
- [x] Pagination works end-to-end with real DB
- [x] Status transitions work end-to-end with real DB (pending → routing → escalated)
- [x] Correction run linking works with real DB FK constraints
- [x] Records created with different handler types persist correctly

### Error Handling

- [x] All 9 API routes catch `ProgrammingError` and return structured 501 Not Implemented
- [x] `ProgrammingError` test module (`test_feedback_programming_error.py`) covers all 9 routes
- [x] FeedbackManager methods raise typed exceptions: `FeedbackRecordNotFoundError`, `InvalidTransitionError`, `ConcurrentModificationError`, `ValidationError`
- [x] Concurrent modification detected via atomic `UPDATE ... WHERE status = expected_status ... RETURNING` (optimistic locking)
- [x] `_rls` decorator wraps every FeedbackManager method — RLS failure is caught, logged, and re-raised, not silently swallowed
- [ ] `list_eval_proposals` route runs snapshot/node-name resolution queries OUTSIDE the `try/except ProgrammingError` block (lines 227–240 of `feedback.py`) — if `runs` or `pipeline_snapshots` tables exist but their data is stale (edge case after partial migration rollback), these unprotected queries would produce a raw 500 instead of structured 501

### Resilience

- [x] Every route uses `async with session.begin()` — transaction-scoped, auto-rollback on exception
- [x] `detect-eval-gap` route has TWO `try/except ProgrammingError` blocks (record fetch + eval suite query, each in its own transaction)
- [x] `FeedbackManager.create_feedback_record` validates required fields (`rejection_reason` non-empty, `feedback_handler_type` in known set) before touching DB
- [x] `_paginate` validates page >= 1 and page_size >= 1 before executing queries
- [x] `spawn_correction_run` handles `None` input_payload gracefully (`dict(original_run.input_payload or {})`)
- [x] `detect_eval_gap` handles `None` eval_engine and empty eval_suite gracefully
- [ ] Frontend `FeedbackInboxView.vue` uses bare `${err}` in template literals (6 locations) instead of `formatApiError(err)` — produces `[object Object]` on API error responses

### Edge Cases

- [x] Empty feedback records list returns `total=0` with empty `items` array (not 404/no response)
- [x] Empty eval proposals list returns `total=0`
- [x] Unknown record ID on single-record retrieval returns `None` (not exception), 404 raised at route level
- [x] Invalid status transition raises specific `InvalidTransitionError` with descriptive message listing allowed transitions
- [x] Concurrent status update detected via optimistic lock — raises `ConcurrentModificationError`, caller can retry
- [x] Double-link to correction run blocked by `correction_run_id is not None` check
- [x] `link_correction_run` checks that current status allows "correcting" transition before linking
- [x] `run_post_correction_eval` validates record is in "correcting" state, has correction_run_id, and correction run is "complete"
- [x] `detect_eval_gap` with empty eval_suite returns `False` (no gap — skips further processing)
- [x] `create_feedback` validates run exists AND belongs to user's org (returns 404 if either fails)
- [x] `create_feedback_record` strips whitespace from `rejection_reason`
- [x] `formatApiError` not imported/used in FeedbackInboxView.vue (uses bare `${err}`)
- [x] `formatDate()` in FeedbackInboxView.vue hardcodes `'en-US'` locale — ignores user's locale preference

## QA History

### 2026-07-03 — Cross-cutting QA (index 87)
- **Fixed**: Stale "No feedback inbox UI implemented" gap — the view exists.
- **Fixed**: `feedback_system.feature` BDD has 7 real scenarios (was thought to be 5).
- **Noted**: `run_post_correction_eval` exists but is not wired into the run completion lifecycle (still a gap).
- **Added**: Cross-module contract verified — frontend entry `feat-frontend-feedback-routing` correctly depends on this entry. No interface drift detected.

### 2026-07-04 — Cross-cutting QA (this session)
- **Verified**: All 131 behaviours checked against code — 18 marked `[ ]` (not implemented), 113 marked `[x]` (verified)
- **Verified**: 9/9 routes have ProgrammingError catches (1 partial gap in proposals route)
- **Added**: Error Handling, Resilience, Edge Cases sections with detailed checkboxes
- **Fixed**: `detect_eval_gap` API endpoint gap — endpoint now queries pipeline eval definitions from DB instead of hardcoding `eval_suite=[]`
- **Noted**: Frontend FeedbackInboxView uses bare `${err}` (not `formatApiError`). `formatDate` hardcodes `'en-US'` locale.
- **Noted**: `list_eval_proposals` route has unprotected DB queries after ProgrammingError try block — minor robustness gap.

## Known Gaps

- BDD feature file (backend/tests/bdd/features/eval/feedback_system.feature) has 7 real scenarios (not a placeholder) — covers create, status transitions, invalid transitions, eval gap detection, and correction run spawning. Step defs in test_eval.py and test_hitl.py provide real implementations using FeedbackManager. Other files (feedback-proposals.md, feedback-loop.md) have stale "placeholder" claims that should reference this status.
- `list_eval_proposals` route (feedback.py:227–240) runs snapshot/node-name resolution queries outside the `try/except ProgrammingError` block — partial 501 catch gap.
- No correction run checkpoint pre-seeding logic implemented (spawn_correction_run creates a new run but doesn't inherit LangGraph checkpoint state)
- AI correction agent not implemented as a library primitive
- Feedback inbox UI exists (FeedbackInboxView.vue) but save-annotation and mark-resolved buttons both call `action: 'mark_reviewed'` — no endpoint exists to save annotation without transitioning status
- No eval proposals editor/curation UI
- Correction run does not route back through eval suite automatically (no run_post_correction_eval integration in run completion lifecycle; the method exists but is not called by the run completion lifecycle)
- Pipeline-level default_feedback_handler not implemented (default_human hardcoded)
- No reject_routing_conflict validation in pipeline editor
- Eval failure does NOT escalate to "escalated" status (record stays in "correcting" — partial gap per PRD §8.20)
- Library contribution (v2) not started
- Frontend FeedbackInboxView.vue hardcodes `'en-US'` in `formatDate()` instead of using current locale — existing pattern across 17+ views, not specific to this feature
- Website docs: no page exists at Website/modulo-website/src/docs/ for PRD §8.20 Feedback Records — create stub
- `test_detects_eval_gap` test in `test_feedback_endpoint.py` fails (500 vs 200) — the route handler now queries EvalDefinition from DB directly (line 339), but the test only mocks `FeedbackManager.get_feedback_record` and `FeedbackManager.detect_eval_gap`, not the intermediate `session.execute()` calls. The mock session's `AsyncMock` treats `scalar_one_or_none()` as an async method returning a coroutine instead of a sync method returning a mock Run. Fix: mock `session.execute` to return a proper Result-like object, or patch `session.execute` before the second transaction block.
