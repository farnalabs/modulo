---
id: feat-core-feedback-correction
prd: 8.20
delivery-tasks:
  - task-nv4-ai-correction-agent
  - task-nv4-correction-run
bdd:
  - backend/tests/bdd/features/eval/feedback_system.feature
  - backend/tests/bdd/features/hitl/feedback_handler.feature
code:
  - backend/src/modulo/core/feedback_manager/
  - backend/src/modulo/db/models/feedback_record.py
  - backend/src/modulo/db/crud/run.py
  - backend/src/modulo/api/routes/feedback.py
depends-on: [feat-evals-feedback-records, feat-evals-feedback-routing]
unit-tests:
  - backend/tests/unit/api/test_feedback_endpoint.py
  - backend/tests/unit/api/test_feedback_programming_error.py
  - backend/tests/unit/core/feedback_manager/test_feedback_manager.py
  - backend/tests/integration/feedback_manager/test_feedback_flow.py
status: partial
---

# Feedback correction — AI correction agent, correction run mechanics, post-correction eval

Correction run spawning, linking, and post-correction evaluation for the Feedback System (8.20).

## Behaviours

### Correction run spawning

- [x] spawn_correction_run fetches FeedbackRecord by ID
- [x] Raises ValueError when FeedbackRecord not found
- [x] Fetches original run from FeedbackRecord.run_id
- [x] Raises ValueError when original run not found
- [x] Creates new run with parent_run_id = original run.id, trigger_type = "correction"
- [x] New run copies pipeline_id and snapshot_id from original run
- [x] New run created_by = record.rejected_by
- [x] _feedback_correction payload injected into new run's input_payload
- [x] _feedback_correction contains: rejection_reason, rejected_output, producing_node_id, is_correction_run
- [x] run_context_overrides merged into _feedback_correction block
- [x] Handles None input_payload on original run gracefully (defaults to empty dict)
- [x] Links correction run to FeedbackRecord via link_correction_run
- [x] Returns new correction run UUID
- [ ] Correction run pre-seeded from original LangGraph checkpoint at target_node_id — creates fresh run, not checkpoint-resumed
- [ ] Correction run goes through full eval suite before reaching HITL gate again — not end-to-end verified

### link_correction_run

- [x] Validates current status allows "correcting" transition
- [x] Sets correction_run_id and transitions feedback_status to "correcting"
- [x] Returns updated FeedbackRecord
- [x] Returns None when record not found
- [x] Raises ValueError when current status does not allow "correcting" transition

### Post-correction eval

- [x] run_post_correction_eval fetches FeedbackRecord and validates correcting status
- [x] Raises ValueError when record not found
- [x] Raises ValueError when record not in "correcting" status
- [x] Raises ValueError when no correction_run_id linked
- [x] Raises ValueError when correction run not found in DB
- [x] Calls EvalEngine.standalone_evaluate on correction run outputs_json
- [x] Returns dict with keys: passed, detail, score, needs_human_review
- [x] ai_correction handler: auto-resolves (status -> "resolved") on eval pass
- [x] manual_correction handler: sets needs_human_review=True (pauses for human validation)
- [x] Returns structured dict on success

### Edge cases

- [x] Error with empty correction_run payload (missing producing_node_id, rejection_reason)
- [x] Double-correction: link_correction_run raises ConcurrentModificationError on re-link
- [ ] Correction run fails to start — linking stays in "correcting" status
- [ ] Post-correction eval fails — correction run succeeded, eval is missing

### Error Handling

- [x] ProgrammingError on DB query returns 501 Not Implemented with migration hint
- [x] FeedbackRecord not found returns 404 (ValueError propagated)
- [x] Original run not found returns ValueError
- [x] FeedbackRecord not in expected state returns ValueError with transition message
- [x] Correction run not found in DB returns ValueError
- [x] Run not found returns 404
- [x] Invalid status value returns 422
- [x] Invalid review action returns 422
- [x] Double-correction guard — link_correction_run allows re-linking with concurrent-status check
- [x] Feedback with no run_id rejects create_correction_run with 422
- [x] Eval engine failure during post-correction eval — error caught, record escalated, returns structured error dict

### Resilience

- [x] Correction run with no output — escalated to human review with structured error response
- [x] Eval engine failure in run_post_correction_eval — caught, logged, record escalated, caller receives structured fallback
- [x] ConcurrentModificationError on status transition — retry-safe, expected/actual status logged
- [x] RLS setup failure — logged with org_id and method name, re-raised
- [x] Empty eval_suite during gap detection — logged warning, true returned (assumes gap)
- [x] Malformed eval_def in gap detection — logged warning, skipped gracefully
- [x] Correction run on feedback with no run_id — rejected with 422 before DB access
- [x] All 9 API routes wrapped in except ProgrammingError for migrations-not-run safety
- [x] All DB queries in get_inbox_item run inside session.begin() with RLS context — fixed cross-tenant leak
- [x] "dismiss" action uses "resolved" status — no longer rejected by DB CHECK constraint
- [x] detect_eval_gap uses single transaction — no stale read risk between two transactions
- [x] _VALID_STATUS_TRANSITIONS aligned with DB CHECK constraint and PRD §8.20 — no "dismissed" orphan state

## QA History

### Findings fixed (index 75)
- Added ProgrammingError→501 catch to all 9 feedback API route handlers
- Added 9 unit tests for ProgrammingError handling (test_feedback_programming_error.py)
- Updated frontmatter: unit-tests populated with 4 real test file refs
- Removed stale known gap: "No BDD feature files for correction flow" (feedback_system.feature exists)
- Added Error Handling section with 8 behaviour checkboxes
- Added feedback-routing.md dependency noted as stub gap

### Findings fixed (index 76 — cross-cutting verification)
- Verified all 9 ProgrammingError→501 catches present in feedback.py (confirmed from code)
- Checked eval engine failure item: code catches Exception in run_post_correction_eval, escalates record, returns structured dict — marked [x]
- Checked double-correction guard: link_correction_run raises ConcurrentModificationError — marked [x], removed stale Known Gap
- Added Resilience section with 8 verified behaviour checkboxes
- Created website docs stub at feedback/correction.md
- Known Gap "No guard against double-correction" removed (now guarded)

### Findings fixed (index 248 — cross-cutting QA)
- CRITICAL — `get_inbox_item` route ran pipeline name queries (Run + Pipeline) outside `session.begin()` transaction block, after `SET LOCAL app.organisation_id` had expired. On Postgres, `set_config(..., true)` is transaction-scoped, so subsequent queries lacked RLS context — cross-tenant data leak. Fixed: moved all DB queries inside the transaction block.
- CRITICAL — "dismiss" review action used `update_status(record_id, "dismissed")` which was rejected by DB CHECK constraint `ck_feedback_records_status` (only allows 'pending', 'routing', 'correcting', 'resolved', 'escalated'). The `_VALID_STATUS_TRANSITIONS` state machine included "dismissed" but the DB constraint didn't — every dismiss action silently failed with a DB error. Fixed: changed "dismiss" action to set status "resolved" (same terminal state as "mark_reviewed"), removed "dismissed" from `_VALID_STATUS_TRANSITIONS`, added "resolved" to pending transitions.
- MAJOR — `detect_eval_gap` route used two separate `async with session.begin()` blocks. Between the two transactions, the feedback record's status or data could change (stale read risk). Fixed: merged into single transaction block with early-exit 404 for missing record.
- MAJOR — `_VALID_STATUS_TRANSITIONS` included "dismissed" which was inconsistent with DB CHECK constraint. Removed "dismissed" from state machine entirely — the "dismiss" action now resolves to "resolved". State machine is now fully aligned with DB constraint and PRD §8.20 spec (pending→routing→correcting→resolved|escalated).

## Known Gaps

- No BDD feature files for the correction error paths — only happy-path BDD scenarios exist
- No integration test for full correction lifecycle: reject → spawn → run → eval → resolve
- No frontend UI for viewing correction runs linked to a feedback record
- Correction run checkpoint pre-seeding is not implemented
- Frontend `FeedbackInboxView.vue` not reviewed for i18n or error handling
