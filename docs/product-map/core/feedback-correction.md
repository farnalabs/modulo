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
- [x] InvalidTransitionError from spawn_correction_run returns 409 (state conflict), not 404
- [x] FeedbackRecordNotFoundError from update_status in mark_reviewed/dismiss returns 404, not 500
- [x] CancelledError guarded before except Exception in all eval error paths — prevents silent swallow on shutdown

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
- [x] "dismiss" action uses "resolved" status — aligned with PRD §8.20 status flow
- [x] detect_eval_gap uses single transaction — no stale read risk between two transactions
- [x] _VALID_STATUS_TRANSITIONS aligned with DB CHECK constraint and PRD §8.20 — no "dismissed" orphan state
- [x] run_context_overrides can shadow standard _feedback_correction keys — caller responsibility, documented edge case

## QA History

### 2026-07-12 — Round 3 improve-architecture
- **MAJOR:** Fixed B904 (exception chaining) on all 10 feedback route handlers — `IntegrityError`, `ProgrammingError`, `SQLAlchemyError`, and `Exception` now use `raise ... from exc` pattern
- **CRITICAL:** Applied the "dismiss→resolved" fix that was documented in index 341 but never merged — changed `dismiss` action from `update_status(record_id, "dismissed")` to `update_status(record_id, "resolved")`; removed "dismissed" from `_VALID_STATUS_TRANSITIONS` and from PATCH `valid_statuses`
- **MAJOR:** Corrected stale product map claims — the code still had `"dismissed"` status despite index 341 documenting the fix as completed

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
- CRITICAL — "dismiss" review action used `update_status(record_id, "dismissed")` which was rejected by DB CHECK constraint. The fix from this index was partially applied (DB constraint updated to include "dismissed") but the action was never changed to "resolved" and `_VALID_STATUS_TRANSITIONS` retained "dismissed" — the code and PRD diverged again. Re-applied in index 341.
- MAJOR — `detect_eval_gap` route used two separate `async with session.begin()` blocks. Between the two transactions, the feedback record's status or data could change (stale read risk). Fixed: merged into single transaction block with early-exit 404 for missing record.
- MAJOR — `_VALID_STATUS_TRANSITIONS` included "dismissed" which was inconsistent with DB CHECK constraint. The state machine fix from index 248 did not persist — "dismissed" remained in the transitions. Re-applied in index 341.

### Findings fixed (index 341 — cross-cutting QA)
- CRITICAL — "dismiss" review action used `update_status(record_id, "dismissed")` which conflicts with PRD §8.20 status flow (`pending→routing→correcting→resolved|escalated`, no "dismissed"). Previous fix (index 248) was incomplete — DB constraint was widened to include "dismissed" but the action itself was never changed. Fixed: changed dismiss action to use "resolved", removed "dismissed" from `_VALID_STATUS_TRANSITIONS` everywhere, removed "dismissed" from DB CHECK constraint, removed "dismissed" from `valid_statuses` set in PATCH /status endpoint. Updated tests, frontend, and product map.
- MAJOR — `InvalidTransitionError` raised by `spawn_correction_run` (via `link_correction_run`) was caught as `FeedbackManagerError` parent class in the inner try/except in `review_feedback` and mapped to 404. Since `InvalidTransitionError` is a state conflict (not a "not found" error), the correct response is 409. Fixed: split the inner catch into three clauses — `FeedbackRecordNotFoundError`→404, `(InvalidTransitionError, ConcurrentModificationError)`→409, and `FeedbackManagerError`→404 (for original run not found).
- MAJOR — `FeedbackRecordNotFoundError` from `update_status` in `mark_reviewed`/`dismiss` paths fell through to the catch-all `except Exception`→500 handler in `review_feedback`. If the record was deleted between the SELECT and UPDATE, the caller got a misleading 500 instead of 404. Fixed: added explicit `except FeedbackRecordNotFoundError`→404 before the catch-all.
- MINOR — Added `asyncio.CancelledError: raise` guard before `except Exception` in `detect_eval_gap` eval loop and `run_post_correction_eval` standalone_evaluate call. On Python < 3.12, `CancelledError` inherits from `Exception` and would be caught as a regular error instead of propagating.
- MINOR — `feedback_handler_type` displayed raw in `FeedbackInboxView.vue` (e.g. "ai_correction" instead of "AI Correction"). Fixed: added `handlerTypeLabel()` translation helper with en-US keys for the three handler types.

### Verified behaviours (index 341)
- Verified `link_correction_run` UPDATE WHERE clause includes `correction_run_id.is_(None)` for atomicity — prevents TOCTOU double-correction ✓
- Verified `_escalate_record` path in `run_post_correction_eval` follows the same `session.flush()` at line 542 ✓
- Verified `spawn_correction_run` handles `create_run` failure via transaction rollback — orphan correction runs are rolled back if `link_correction_run` fails ✓
- Verified all `except Exception` blocks have proper logging with `exc_info=True` ✓
- Verified `CancelledError` guard added to both eval error paths ✓
- Verified `run_context_overrides` merge can shadow standard keys (e.g. `producing_node_id`) — documented edge case, not fixed (caller responsibility)

## Known Gaps
- ~~**"dismiss" action used "dismissed" status** — fixed in Round 3 improve-architecture: now uses "resolved" as specified in PRD §8.20~~
- No BDD feature files for the correction error paths — only happy-path BDD scenarios exist
- No integration test for full correction lifecycle: reject → spawn → run → eval → resolve
- Correction run checkpoint pre-seeding is not implemented
- Correction run that starts but fails during execution leaves the FeedbackRecord stuck in "correcting" status — no automatic escalation for runtime failures
- `run_context_overrides` can shadow standard `_feedback_correction` keys (producing_node_id, rejection_reason, rejected_output, is_correction_run) — caller must not set conflicting keys
