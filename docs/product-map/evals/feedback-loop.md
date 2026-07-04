---
id: feat-evals-feedback-loop
prd: 8.20
delivery-tasks: [task-nv4-feedback-loop-auto]
bdd:
  - backend/tests/bdd/features/eval/feedback_system.feature
  - backend/tests/features/personas/priya-platform-engineer.feature
code:
  - backend/src/modulo/core/feedback_manager/
  - backend/src/modulo/core/eval_engine/
  - backend/src/modulo/core/pipeline_engine/executor.py
  - backend/src/modulo/db/models/feedback_record.py
  - backend/src/modulo/db/crud/run.py
unit-tests:
  - backend/tests/unit/core/feedback_manager/test_feedback_manager.py
  - backend/tests/unit/api/test_feedback_endpoint.py
  - backend/tests/unit/api/test_feedback_programming_error.py
  - backend/tests/integration/feedback_manager/test_feedback_flow.py
depends-on: [feat-core-feedback-correction, feat-evals-feedback-proposals, feat-frontend-feedback-inbox-ui]
status: partial
---

# Feedback System — feedback loop, eval gap detection, eval suite growth

The Feedback System treats every human rejection as structured signal. Handles FeedbackRecord lifecycle, three handler types, correction run spawning, eval gap detection, and eval proposal curation.

## Behaviours

### FeedbackRecord entity
- [x] Every HITL rejection produces a FeedbackRecord with run_id, gate_id, rejected_by, rejection_reason, rejected_output, producing_node_id, producing_agent_id
- [x] FeedbackRecord is immutable after creation; correction loop produces new runs, does not modify original
- [x] Status lifecycle: pending -> routing -> correcting -> resolved | escalated | dismissed
- [x] Invalid status transitions raise ValueError with descriptive message
- [x] Correction run linked via correction_run_id field
- [x] Organisation-scoped RLS enforced on all FeedbackRecord operations
- [ ] FeedbackRecord status transitions are audited (no audit trail yet)

### Feedback routing
- [x] Pipeline-level default_feedback_handler field (default: "human")
- [x] Gate-level feedback_handler field overrides pipeline default
- [x] Three handler types: human, ai_correction, ai_correction_with_human_review
- [x] Create FeedbackRecord with type "human" — status stays "pending", no auto-correction
- [x] Create FeedbackRecord with type "ai_correction" — auto-transitions to "correcting", spawns correction run
- [x] Create FeedbackRecord with type "ai_correction_with_human_review" — auto-transitions to "correcting", spawns correction run
- [ ] Setting both feedback_handler and reject_target on same gate raises validation error (reject_routing_conflict) — PRD spec not implemented
- [ ] feedback_handler supersedes reject_target when both are set — PRD spec not fully implemented

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
- [ ] Feedback inbox UI surface (first-class UI page with filters) — not implemented
- [ ] Annotation UI for human handler type (add notes, trigger correction run manually) — not implemented
- [ ] Correction proposal display with accept/reject for ai_correction_with_human_review — not implemented

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
- [ ] Correction run uses same PipelineSnapshot as original run — not verified
- [ ] Correction run pre-seeded from original LangGraph checkpoint at target_node_id — not implemented (creates fresh run)

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
- [ ] When evals fail, correction run marked eval_failed and FeedbackRecord escalated (escalated not implemented in run_post_correction_eval)
- [ ] Correction run goes through full eval suite before reaching HITL gate again — not end-to-end verified

### Eval gap detection
- [x] detect_eval_gap returns False when no eval suite provided
- [x] detect_eval_gap runs EvalEngine.evaluate against rejected output — iterates eval suite, returns True if all pass (gap detected)
- [x] FeedbackRecord tagged eval_gap when no eval scored output as failing — return value is the gap boolean
- [ ] Standalone evaluate path is first-class interface — exists at code level but untested via gap detection

### Eval proposals
- [x] get_eval_proposals returns paginated FeedbackRecords with eval_gap = True and status in [pending, routing]
- [ ] AI correction agent (or dedicated eval-proposal agent) drafts new eval case for eval_gap records — not implemented
- [ ] Eval proposals queue UI with draft eval editor — not implemented
- [ ] Human reviews, edits, and publishes proposed evals — not implemented
- [ ] Published evals immediately active for future runs — not implemented
- [ ] Library contribution (v2): curated evals contributed to community library — deferred to v2

### Persona scenario
- [x] Priya scenario: HITL rejection -> FeedbackRecord created -> eval suite proposed for expansion -> human reviews and approves new eval case — scenario written, steps stubbed only

### Security & concurrency
- [x] Organisation-scoped RLS enforced via _rls decorator on all public methods
- [ ] Concurrent status transitions not guarded (no advisory lock or optimistic locking)
- [ ] Input validation on rejection_reason length — not enforced
- [ ] Input validation on rejected_output size — not enforced

## Known Gaps
- BDD feature file (feedback_system.feature) has 7 real scenarios (not a placeholder) — covers create, status transitions, invalid transitions, gap detection, and correction run spawning
- detect_eval_gap logic is implemented — now returns True when all evals pass (gap) and False when any fails. API endpoint now fetches eval definitions from the pipeline (no longer passes eval_suite=[])
- Correction run mechanics create a fresh run rather than seeding from original LangGraph checkpoint
- No feedback_handler supersedes reject_target enforcement at validation/gate level
- No audit events recorded for FeedbackRecord status transitions
- Feedback inbox UI exists (FeedbackInboxView.vue), but no annotation or eval proposal curation UI