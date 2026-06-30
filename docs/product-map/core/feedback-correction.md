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
- [ ] Double-correction: submitting a second correction for same record returns error or idempotent?
- [ ] Correction run fails to start — linking stays in "correcting" status
- [ ] Post-correction eval fails — correction run succeeded, eval is missing

### Error handling

- [x] FeedbackRecord not found returns ValueError
- [x] Original run not found returns ValueError
- [x] FeedbackRecord not in expected state returns ValueError with transition message
- [x] Correction run not found in DB returns ValueError
- [ ] Eval engine failure during post-correction eval — error propagates or falls back?

## Known Gaps

- No BDD feature files exist for the correction flow — only backend unit tests
- No integration test for full correction lifecycle: reject → spawn → run → eval → resolve
- No guard against double-correction on same feedback record
- No frontend UI for viewing correction runs linked to a feedback record
- Correction run checkpoint pre-seeding is not implemented
