---
id: feat-evals-feedback-loop
prd: 8.20
delivery-tasks: [task-nv4-feedback-loop-auto]
bdd:
  - backend/tests/bdd/features/eval/feedback_system.feature
  - backend/tests/bdd/features/personas/priya-platform-engineer.feature
code:
  - backend/src/modulo/core/feedback_manager/
  - backend/src/modulo/core/eval_engine/
  - backend/src/modulo/core/pipeline_engine/executor.py
  - backend/src/modulo/db/models/feedback_record.py
  - backend/src/modulo/db/crud/run.py
unit-tests:
  - backend/tests/unit/core/feedback_manager/test_feedback_manager.py
  - backend/tests/unit/api/test_feedback_endpoint.py
  - backend/tests/unit/api/test_error_handling.py
  - backend/tests/integration/feedback_manager/test_feedback_flow.py
depends-on: [feat-core-feedback-correction, feat-evals-feedback-proposals, feat-frontend-feedback-inbox-ui]
status: partial
---

# Feedback System � feedback loop, eval gap detection, eval suite growth

The Feedback System treats every human rejection as structured signal. Handles FeedbackRecord lifecycle, three handler types, correction run spawning, eval gap detection, and eval proposal curation.

## Behaviours

### FeedbackRecord entity
- [x] Every HITL rejection produces a FeedbackRecord with run_id, gate_id, rejected_by, rejection_reason, rejected_output, producing_node_id, producing_agent_id
- [x] FeedbackRecord is immutable after creation; correction loop produces new runs, does not modify original
- [x] Status lifecycle: pending -> routing -> correcting -> resolved | escalated | dismissed
- [x] Invalid status transitions raise ValueError with descriptive message
- [x] Correction run linked via correction_run_id field
- [x] Organisation-scoped RLS enforced on all FeedbackRecord operations
  - [x] FeedbackRecord status transitions are audited — `feedback.status_changed` dispatched from the `PATCH /feedback/{id}/status` and `POST /feedback/inbox/{id}/review` routes (old_status, new_status, action, run_id, gate_id, correction_run_id), written in a fresh transaction after the primary op commits and failure-isolated (broken audit append never fails the transition)

### Feedback routing
- [ ] Pipeline-level default_feedback_handler field is defined on Pipeline model but NEVER consumed � create API hardcodes "human", no runtime code reads the field
- [ ] Gate-level feedback_handler field does not exist on HitlGateConfig (only reject_target exists) � gate-level override is unimplemented
- [x] Three handler types: human, ai_correction, ai_correction_with_human_review
- [x] Create FeedbackRecord with type "human" � status stays "pending", no auto-correction
- [x] Create FeedbackRecord with type "ai_correction" � auto-transitions to "correcting", spawns correction run
- [x] Create FeedbackRecord with type "ai_correction_with_human_review" � auto-transitions to "correcting", spawns correction run
- [ ] Setting both feedback_handler and reject_target on same gate raises validation error (reject_routing_conflict) � PRD spec not implemented
- [ ] feedback_handler supersedes reject_target when both are set � PRD spec not fully implemented

### FeedbackRecord CRUD
- [x] create_feedback_record creates record with correct organisation_id, status, and handler type
- [x] get_feedback_records returns paginated results with total count
- [x] get_feedback_records filters by feedback_status and pipeline_id
- [x] get_feedback_record returns single record by ID
- [x] get_feedback_record returns None when record not found
- [x] update_status validates transitions against _VALID_STATUS_TRANSITIONS state machine
- [x] update_status returns None when record not found
- [x] link_correction_run validates status allows "correcting" transition before linking
- [x] link_correction_run updates correction_run_id and transitions status

### Feedback inbox
- [x] get_feedback_records_inbox returns paginated records with pipeline name enrichment
- [x] get_feedback_records_inbox filters by handler_type, status, pipeline_id, date range
- [x] get_feedback_records_inbox returns empty result set when no records match
- [x] Feedback inbox UI surface (first-class UI page with filters, expand/collapse, detail panel) � FeedbackInboxView.vue
- [x] Annotation UI for human handler type (add notes via textarea, save via review endpoint) � implemented in FeedbackInboxView.vue
- [ ] Correction proposal accept/reject for ai_correction_with_human_review � display exists (correction_proposal section) but accept/reject actions not implemented

### Correction run spawning
- [x] spawn_correction_run fetches FeedbackRecord and original run
- [x] Raises ValueError when FeedbackRecord not found
- [x] Raises ValueError when original run not found
- [x] Creates new run with parent_run_id = original run, trigger_type = "correction"
- [x] New run copies pipeline_id and snapshot_id from original
- [x] _feedback_correction payload injected into input_payload with rejection_reason, rejected_output, producing_node_id, is_correction_run
- [x] run_context_overrides merged into _feedback_correction block
- [x] Handles None input_payload on original run gracefully
- [x] Links correction run to FeedbackRecord via link_correction_run
- [x] Returns new correction run UUID
- [x] Correction run uses same PipelineSnapshot as original run � spawn_correction_run copies original_run.snapshot_id
- [x] Correction run is a fresh run, NOT pre-seeded from the original LangGraph checkpoint — PRD 8.20 explicitly specifies fresh runs ("Correction runs are fresh runs — they do not inherit checkpoint state"), and the code creates a new run; no checkpoint-resume path exists or is required

### Post-correction eval
- [x] run_post_correction_eval fetches record and validates correcting status
- [x] Raises ValueError when record not found
- [x] Raises ValueError when record not in "correcting" status
- [x] Raises ValueError when no correction run linked
- [x] Raises ValueError when correction run not found
- [x] Calls EvalEngine.standalone_evaluate on correction run output
- [x] ai_correction: auto-resolves on eval pass (status -> "resolved"), needs_human_review = False
- [x] ai_correction_with_human_review: resolves on eval pass with needs_human_review = True
- [x] Does not resolve when eval fails; needs_human_review remains False
- [x] When evals fail, FeedbackRecord is escalated to the human feedback inbox — run_post_correction_eval calls _escalate_record (status -> "escalated"); the correction run itself uses the standard run lifecycle (PRD 8.20: no separate eval_failed status on FeedbackRecords)
- [ ] Correction run goes through full eval suite before reaching HITL gate again � not end-to-end verified

### Eval gap detection
- [x] detect_eval_gap returns False when no eval suite provided
- [x] detect_eval_gap runs EvalEngine.evaluate against rejected output � iterates eval suite, returns True if all pass (gap detected)
- [x] FeedbackRecord tagged eval_gap when no eval scored output as failing � return value is the gap boolean
- [x] Standalone evaluate path is a first-class interface — detect_eval_gap exercises the real EvalEngine.evaluate() against the frozen rejected output (covered by test_uses_real_eval_engine_standalone_path)

### Eval proposals
- [x] get_eval_proposals returns paginated FeedbackRecords with eval_gap = True and status in [pending, routing]
- [ ] AI correction agent (or dedicated eval-proposal agent) drafts new eval case for eval_gap records � not implemented
- [ ] Eval proposals queue UI with draft eval editor � not implemented
- [x] Human publishes proposed evals � backend publish endpoint creates a live EvalDefinition (`POST /feedback/proposals/{record_id}/publish`, PRD §8.20 ¶Eval suite growth #3); draft-eval review/edit editor UI remains a gap
- [x] Published evals immediately active for future runs � the published EvalDefinition is node+pipeline scoped and loaded by the run-time eval loader (`executor.py` filters `node_id.isnot(None)`) and by gap detection, so it fires on the next run of that pipeline
- [ ] Library contribution (v2): curated evals contributed to community library � deferred to v2

### Persona scenario
- [x] Priya scenario: HITL rejection -> FeedbackRecord created -> eval suite proposed for expansion -> human reviews and approves new eval case � scenario written, steps stubbed only

### Security & concurrency
- [x] Organisation-scoped RLS enforced via _rls decorator on all public methods
- [x] Feedback record creation audited — `feedback.created` dispatched from the create route (run_id, gate_id, feedback_handler_type), resource_type `feedback_record`
- [x] Feedback status transitions audited — `feedback.status_changed` dispatched from the update-status and review routes (old_status, new_status, action, run_id, gate_id, correction_run_id)
- [x] Audit append is failure-isolated — `asyncio.CancelledError` re-raised, any other audit failure logged (`feedback.audit_append_failed`) and never blocks the completed operation (api_keys/teams gold pattern)
- [x] 404 transitions emit no audit event
- [x] Concurrent status transitions guarded via optimistic locking — update_status/link_correction_run use UPDATE ... WHERE status = expected ... RETURNING and raise ConcurrentModificationError on stale writes
- [x] Input validation on rejection_reason length — max 5000 characters (create_feedback_record), covered by tests
- [x] Input validation on rejected_output size — max 100KB when serialized, covered by tests

### Error Handling
- [x] detect_eval_gap wraps eval_engine.evaluate() in a try/except � eval engine exceptions are caught and logged, iteration continues; does NOT propagate as 500s
- [x] run_post_correction_eval raises specific exceptions: FeedbackRecordNotFoundError, InvalidTransitionError for each precondition violation
- [x] run_post_correction_eval catches exceptions from engine.standalone_evaluate() � calls _escalate_record on eval crash (fixed in earlier QA sweep)
- [x] spawn_correction_run raises FeedbackRecordNotFoundError for missing record AND FeedbackManagerError for missing run
- [x] All 10 feedback API routes have ProgrammingError � 501 mapping, tested in test_error_handling.py
- [x] Create feedback validates empty rejection_reason (ValidationError) and unknown handler_type (ValidationError)
- [x] update_status validates transitions via _VALID_STATUS_TRANSITIONS with descriptive error message
- [x] HITL reject path (/hitl/{gate_id}/reject) does NOT create a FeedbackRecord � feedback records are created exclusively via the explicit POST /runs/{run_id}/feedback endpoint
- [x] Review feedback endpoint catches FeedbackRecordNotFoundError ? 404, InvalidTransitionError/ConcurrentModificationError ? 409, FeedbackManagerError ? 404 (base class catch � not ValueError)
- [ ] Review feedback endpoint base-class FeedbackManagerError ? 404 is too broad � could mask unexpected subclasses; should be narrowed

### Resilience
- [ ] ConcurrentModificationError on update_status/link_correction_run has no automatic retry � caller must catch and retry
- [x] spawn_correction_run checks whether a correction run already exists before spawning � raises ConcurrentModificationError if correction_run_id is set (fixed in QA index 342)
- [ ] No advisory lock on FeedbackRecord row � optimistic locking via WHERE clause exists on update_status and link_correction_run, but no advisory lock for cross-session coordination
- [ ] detect_eval_gap iterates eval_suite sequentially � no timeout or circuit-breaker for slow evals
- [ ] RLS decorator on all FeedbackManager methods catches/sets before each call � no caching, incurs per-call DB round-trip

### Edge Cases
- [x] rejection_reason is length-validated � max 5000 characters (fixed in QA index 342)
- [x] rejected_output size is validated � max 100KB when serialized (fixed in QA index 342)
- [ ] producing_node_id is not format-validated � accepts any string
- [ ] Empty rejected_output {} is accepted without semantic validation
- [ ] FeedbackRecord created for a deleted run (CASCADE deletes the record too) � no soft-delete or archival
- [ ] Correction run spawned for a deleted pipeline � pipeline_id points to a non-existent pipeline
- [ ] Gate_id is limited to 255 chars but is validated by the route param, not the model

## Known Gaps
- BDD feature file (feedback_system.feature) has 7 real scenarios � covers create, status transitions, invalid transitions, gap detection, and correction run spawning
- [Removed 2026-08-15] Correction-run checkpoint seeding was listed as a gap, but PRD 8.20 explicitly specifies correction runs as fresh runs — this is the intended design, not a gap
- No feedback_handler supersedes reject_target enforcement at validation/gate level
- ~~No audit events recorded for FeedbackRecord status transitions~~ **RESOLVED (2026-08-15)**: `feedback.status_changed` is dispatched from the update-status and review routes (fresh post-commit transaction, RLS re-established, failure-isolated), plus `feedback.created` on record creation. Both documented in `core/audit-trail.md` implemented-event list.
- No eval proposal curation editor UI (draft eval editor, review/edit) — the queue view exists (EvalProposalsQueueView.vue) and the backend publish endpoint (`POST /feedback/proposals/{record_id}/publish`, added 2026-08-15) now creates a live EvalDefinition, but the frontend draft editor and the "Publish" button wiring remain gaps
- Review endpoint catches FeedbackManagerError (base class) as 404 � too broad, should be narrowed to specific subclasses
- run_post_correction_eval escalates the FeedbackRecord on eval failure (implemented); it is not yet wired into the run completion lifecycle so the escalation never auto-triggers in production
- No advisory lock cross-session coordination on FeedbackRecord rows
- producing_node_id not format-validated
- CASCADE deletion of FeedbackRecord when parent run is deleted

## QA History

### 2026-08-15 — Coverage-completion (FAR-232/233, partial-evals-b)
- **Implemented (PRD §8.20 ¶Eval suite growth #3)**: `POST /api/v1/feedback/proposals/{record_id}/publish` — human curation's publish step. Creates a node-scoped `EvalDefinition` on the proposal record's pipeline (node resolved from explicit `node_id`, else `producing_node_id` parsed as UUID, else matched against the run snapshot's graph nodes by id/name/label), transitions the record to `resolved`, and appends a `feedback.proposal_published` audit event. Published evals are immediately active because the run-time eval loader (`executor.py` filters `node_id.isnot(None)`) and gap detection fetch definitions by `pipeline_id` at run time. 9 new endpoint unit tests + 2 new ProgrammingError→501/SQLAlchemyError→503 cases in `test_error_handling.py` (feedback routes now 10).
- **Marked `[x]` (implemented + tested)**: "Human publishes proposed evals" and "Published evals immediately active for future runs". The AI-correction-agent draft-generation step, the draft-eval editor UI, and library contribution (v2) remain genuine gaps.
- **Verified (still gaps, kept `[ ]`)**: pipeline/gate-level `default_feedback_handler` consumption (PRD 8.20 explicitly documents runtime does not use it yet), gate-level `feedback_handler` sub-field, `reject_routing_conflict`, correction-proposal accept/reject UI, post-correction eval auto-wiring into the run-completion lifecycle, review-endpoint broad `FeedbackManagerError → 404`, advisory-lock/retry/sequential-eval gaps, and the listed edge cases.

### 2026-08-15 — improve-architecture (feedback audit gaps)
- **RESOLVED the "No audit events recorded for FeedbackRecord status transitions" known gap** (`api/routes/feedback.py`).
- New `_append_feedback_audit_event()` helper appends in a fresh transaction after the primary operation commits (RLS re-established — SET LOCAL reverts on COMMIT), failure-isolated per the api_keys/teams gold pattern (`asyncio.CancelledError: raise` + broad `except Exception` → logged warning, never fails the completed op).
- **`feedback.created`** — the create route appends it (payload `run_id`/`gate_id`/`feedback_handler_type`, record id as `resource_id`, actor = creating user).
- **`feedback.status_changed`** — the update-status route appends it with `old_status` (captured from a pre-update `get_feedback_record` fetch — this also turns a missing record into a clean 404 before `update_status` runs instead of the previously-uncaught `FeedbackRecordNotFoundError` → 500) and the review route appends it for all three actions (`mark_reviewed`/`dismiss` → resolved, `create_correction_run` → correcting, with the action and `correction_run_id` in the payload). A 404 emits nothing.
- **Tests** — 7 new endpoint unit tests in `test_feedback_endpoint.py`: create-emits + create audit-failure isolation, update-status emits full payload + audit-failure isolation + 404-no-emit, review mark_reviewed emits + create_correction_run emits (payload incl. correction_run_id) + review audit-failure isolation.
- Updated product map `evals/feedback-loop.md` (3 behaviours `[ ]`→`[x]`, Known Gap → RESOLVED, QA History) + `evals/feedback-records.md` (Status Transitions + Auth & Security behaviours) + `core/audit-trail.md` (2 implemented-event entries). Verification: 30/30 `test_feedback_endpoint.py`, 129 feedback_manager + error-handling unit tests, 2633/2633 `tests/unit/api/`, 83 route-introspection + audit-logger tests, ruff check + format clean, mypy --strict clean. Status: partial (correction-run lifecycle auto-transitions — link_correction_run/_escalate_record — are not individually audited; only the user-facing routes that trigger them).

### 2026-08-15 — Coverage-completion (FAR-233)
- **Fixed (bug)**: `detect_eval_gap` skipped every real `EvalDefinition` object as "malformed", so gap detection always returned `True` when the pipeline had eval definitions. The malformed guard now accepts `EvalDefinition`-shaped objects; covered by `test_uses_real_eval_engine_standalone_path`.
- **Fixed (PRD compliance)**: the `dismiss` review action now sets status to `dismissed` (PRD 8.20 terminal state) instead of `resolved`; `_VALID_STATUS_TRANSITIONS` and `PATCH /status` accept `dismissed` (`pending`/`escalated` -> `dismissed`, terminal). Covered by new unit + endpoint tests. The earlier "dismiss -> resolved" QA decisions misread the PRD, which explicitly includes `dismissed`.
- **Marked `[x]` (verified)**: optimistic locking exists on status transitions; rejection_reason length and rejected_output size are enforced; standalone EvalEngine.evaluate path is exercised via gap detection.
- **Corrected**: "checkpoint pre-seeding" checkboxes were not PRD requirements — PRD 8.20 specifies fresh correction runs; the code already matches. Eval-failure escalation (record -> `escalated`) is implemented; the correction run uses the standard run lifecycle.
- **Remaining gaps**: audit trail for status transitions, pipeline/gate default_feedback_handler consumption, reject_routing_conflict, accept/reject UI for ai_correction_with_human_review, run_post_correction_eval lifecycle wiring, eval proposal curation.

### QA index 342 (2026-07-09) � Cross-cutting QA sweep
- **Fixed:** spawn_correction_run now checks correction_run_id before spawning � prevents leaked runs (raises ConcurrentModificationError).
- **Fixed:** create_feedback_record validates rejection_reason length (max 5000 chars) and rejected_output serialized size (max 100KB).
- **Verified:** Feedback inbox UI exists at FeedbackInboxView.vue with filters, expand/collapse, annotations, trigger correction, dismiss, mark resolved.
- **Verified:** run_post_correction_eval catches exceptions from engine.standalone_evaluate() and escalates the record.
- **Verified:** detect_eval_gap catches eval engine exceptions and continues iteration.
- **Verified:** update_status and link_correction_run use optimistic locking via WHERE clause with ConcurrentModificationError.
- **Verified:** spawn_correction_run raises FeedbackManagerError (not ValueError) for missing original run.
- **Remaining gaps:** Audit trail integration, pipeline/gate-level feedback_handler routing, eval proposal curation UI, correction run checkpoint seeding, concurrent advisory locking.
