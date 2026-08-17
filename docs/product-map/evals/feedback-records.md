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
  - backend/tests/unit/api/test_error_handling.py
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

- [x] Valid transitions: pending → routing/correcting/resolved/dismissed
- [x] Valid transitions: routing → escalated/correcting/resolved/dismissed
- [x] Valid transitions: correcting → correcting/resolved/escalated/dismissed
- [x] Valid transitions: escalated → resolved/dismissed
- [x] Valid transitions: resolved → (none — terminal)
- [x] Invalid transitions raise ValueError with descriptive message listing allowed transitions
- [x] Unknown record returns None on status update
- [x] RLS enforced on all status transitions (org-scoped)
- [x] API rejects invalid status string with 422
- [x] API updates status via PATCH /feedback/{id}/status
- [x] Status transitions recorded in audit trail — `feedback.status_changed` (old_status, new_status, action, run_id, gate_id) dispatched from the update-status and review routes
- [x] 404 status update emits no audit event

### Handler Types & Auto-Correction

- [x] "human" handler: record stays in pending status (no auto-correction triggered)
- [x] "ai_correction" handler: auto-transitions to correcting and spawns correction run
- [x] "ai_correction_with_human_review" handler: auto-transitions to correcting and spawns correction run
- [ ] Pipeline-level default_feedback_handler applies to all HITL gates unless overridden
- [ ] Gate-level feedback_handler overrides pipeline default
- [ ] Validation error when both reject_target and feedback_handler set on same gate (reject_routing_conflict)

### Correction Run Mechanics

- [x] Correction run is a fresh run (new LangGraph thread) — PRD 8.20 specifies correction runs are fresh and do not inherit checkpoint state; spawn_correction_run creates a new run via create_run
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
- [x] Feedback record creation recorded in audit trail — `feedback.created` (run_id, gate_id, feedback_handler_type)
- [x] Audit appends are failure-isolated — a broken audit write (log `feedback.audit_append_failed`) never fails the completed create/transition

### Integration / Persistence

- [x] FeedbackRecord persists correctly in real DB session
- [x] Pagination works end-to-end with real DB
- [x] Status transitions work end-to-end with real DB (pending → routing → escalated)
- [x] Correction run linking works with real DB FK constraints
- [x] Records created with different handler types persist correctly

### Error Handling

- [x] All 9 API routes catch `ProgrammingError` and return structured 501 Not Implemented
- [x] `ProgrammingError` test module (`test_error_handling.py`) covers all 9 routes
- [x] FeedbackManager methods raise typed exceptions: `FeedbackRecordNotFoundError`, `InvalidTransitionError`, `ConcurrentModificationError`, `ValidationError`
- [x] Concurrent modification detected via atomic `UPDATE ... WHERE status = expected_status ... RETURNING` (optimistic locking)
- [x] `_rls` decorator wraps every FeedbackManager method — RLS failure is caught, logged, and re-raised, not silently swallowed
- [x] `list_eval_proposals` route runs snapshot/node-name resolution queries INSIDE the `try/except ProgrammingError` block — ProgrammingError → 501 / SQLAlchemyError → 503 guarded (fixed 2026-07-06; 18 error-handling cases added 2026-08-15 in test_error_handling.py)

### Resilience

- [x] Every route uses `async with session.begin()` — transaction-scoped, auto-rollback on exception
- [x] `detect-eval-gap` route has TWO `try/except ProgrammingError` blocks (record fetch + eval suite query, each in its own transaction)
- [x] `FeedbackManager.create_feedback_record` validates required fields (`rejection_reason` non-empty, `feedback_handler_type` in known set) before touching DB
- [x] `_paginate` validates page >= 1 and page_size >= 1 before executing queries
- [x] `spawn_correction_run` handles `None` input_payload gracefully (`dict(original_run.input_payload or {})`)
- [x] `detect_eval_gap` handles `None` eval_engine and empty eval_suite gracefully
- [x] Frontend `FeedbackInboxView.vue` uses bare `${err}` in template literals (6 locations) instead of `formatApiError(err)` — produces `[object Object]` on API error responses

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
- [x] formatApiError fix applied — 14 error handlers use formatApiError(err)
- [x] formatDate uses locale.value (observed via useI18n()) — resolves locale preference correctly

## QA History

### 2026-08-15 — sweep D (final verification pass)
- Verified the 3 unchecked behaviours (pipeline-level default_feedback_handler, gate-level feedback_handler override, reject_routing_conflict) remain genuine gaps. PRD §8.20 (docs/prd.md:1905) explicitly documents that `default_feedback_handler` is a DB column not yet consumed at runtime, and that `hitl_gate_config` has no typed `feedback_handler` sub-field. `reject_routing_conflict` (PRD §8.20 reject_target note, docs/prd.md:1232) is not implemented — no validation rejects setting both `reject_target` and `feedback_handler` on the same gate because the gate config has no `feedback_handler` field to validate against. All three documented in Known Gaps. Status: partial (3 known gaps remain in this section).

### 2026-08-15 — improve-architecture (feedback audit events)
- **RESOLVED the "No audit events recorded for FeedbackRecord status transitions" gap** (tracked in `evals/feedback-loop.md`). `api/routes/feedback.py` now dispatches `feedback.created` (create route) and `feedback.status_changed` (update-status + review routes) via a new `_append_feedback_audit_event()` helper — written in a fresh post-commit transaction with RLS re-established (SET LOCAL reverts on COMMIT) and failure-isolated so a broken append never fails the completed operation (api_keys/teams gold pattern).
- `update_feedback_status` now fetches the record first for `old_status` (and returns a clean 404 for a missing record before `update_status`, previously an uncaught `FeedbackRecordNotFoundError` → 500).
- Review route audits all three actions: `mark_reviewed`/`dismiss` → resolved, `create_correction_run` → correcting (payload includes the action and `correction_run_id`).
- **Tests** — 7 new endpoint unit tests (create-emits + failure isolation; update-status emits full payload + failure isolation + 404-no-emit; review mark_reviewed-emits + create-correction-run-emits + failure isolation). 30/30 `test_feedback_endpoint.py`, 2633/2633 `tests/unit/api/` pass; ruff check + format clean; mypy --strict clean.

### 2026-08-15 — Coverage-completion (FAR-233)
- **Fixed (PRD compliance)**: the `dismiss` review action now sets status to `dismissed` (PRD 8.20 terminal state). `_VALID_STATUS_TRANSITIONS`, `PATCH /feedback/{id}/status` valid-status set, and review-endpoint action mapping updated to match the DB CHECK constraint (which always allowed `dismissed`). Covered by `test_dismisses_feedback`, `test_mark_reviewed_uses_resolved_status`, `test_accepts_dismissed_status`, and manager transition tests.
- **Fixed (bug)**: `detect_eval_gap` treated real `EvalDefinition` objects as malformed and skipped the whole suite, always returning `True` (gap) when the pipeline had eval definitions. The guard now accepts `EvalDefinition`-shaped objects; covered by `test_uses_real_eval_engine_standalone_path`.
- **Added**: 18 ProgrammingError→501 / SQLAlchemyError→503 cases for all 9 feedback routes in `test_error_handling.py` (the file previously claimed this coverage but had none). `list_eval_proposals` node-name resolution is confirmed inside the try/except block.
- **Removed stale gaps**: checkpoint pre-seeding (not a PRD requirement — correction runs are fresh by design), eval-failure-does-not-escalate (escalation is implemented and tested), the failing `test_detects_eval_gap` note (the test passes with a `run_id=None` mock record that skips the eval-suite query).

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
- AI correction agent not implemented as a library primitive
- Feedback inbox UI exists (FeedbackInboxView.vue) but save-annotation and mark-resolved buttons both call `action: 'mark_reviewed'` — no endpoint exists to save annotation without transitioning status
- No eval proposals editor/curation UI
- Correction run does not route back through eval suite automatically (no run_post_correction_eval integration in run completion lifecycle; the method exists but is not called by the run completion lifecycle)
- Pipeline-level default_feedback_handler not implemented (default_human hardcoded)
- No gate-level feedback_handler override — the `hitl_gate_config` JSON column on pipeline edges has no typed `feedback_handler` sub-field (verified 2026-08-15, sweep D)
- No reject_routing_conflict validation in pipeline editor
- Library contribution (v2) not started
- Website docs: no page exists at Website/modulo-website/src/docs/ for PRD §8.20 Feedback Records — create stub
