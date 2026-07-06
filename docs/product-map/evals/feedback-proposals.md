---
id: feat-evals-feedback-proposals
prd: 8.20
delivery-tasks: [task-nv4-eval-proposals-queue]
bdd: [backend/tests/bdd/features/eval/feedback_system.feature]
code: [backend/src/modulo/core/feedback_manager/, backend/src/modulo/api/routes/feedback.py]
unit-tests:
  - backend/tests/unit/core/feedback_manager/test_feedback_manager.py
  - backend/tests/unit/api/test_feedback_endpoint.py
  - backend/tests/unit/api/test_feedback_programming_error.py
  - backend/tests/unit/api/test_feedback_sqlalchemy_error.py
  - backend/tests/integration/feedback_manager/test_feedback_flow.py
depends-on: [feat-evals-eval-definitions, feat-evals-feedback-routing]
status: partial
---

# Feedback Proposals — Eval Suite Growth

Discovered from 1 completed delivery task.

## Behaviours

### Eval Gap Detection (8.20 ¶Eval suite growth #1)

- [x] System runs pipeline eval suite against rejected output as standalone evaluation (EvalEngine.evaluate) — `detect_eval_gap()` now executes real eval suite
- [x] FeedbackRecord is tagged `eval_gap=True` when no eval scored the output as failing — logic implemented
- [x] API endpoint `POST /feedback/{record_id}/detect-gap` triggers gap detection
- [x] API endpoint returns `eval_gap` boolean in response

### Proposed Eval Generation (8.20 ¶Eval suite growth #2)
- [ ] AI correction agent or eval-proposal agent drafts a new eval case on `eval_gap` — Not implemented
- [ ] Proposed eval uses rejected output as negative example — Not implemented
- [ ] Proposed eval uses rejection reason as rubric — Not implemented
- [ ] Proposed eval suggests eval type (`llm_judge`, `regex`, `json_schema`) — Not implemented

### Eval Proposal Storage & Retrieval
- [x] Proposed evals land in "Eval proposals" inbox — Partial: `get_eval_proposals()` queries `eval_gap=True` records but no dedicated model
- [x] API endpoint `GET /feedback/proposals` returns proposals with pagination
- [x] Proposals filtered by status `pending` or `routing` and `eval_gap=True`
- [x] Proposals ordered by creation date descending

### Human Curation (8.20 ¶Eval suite growth #3)
- [ ] Human reviews proposed eval in draft eval editor — Not implemented
- [ ] Human edits proposed eval before publishing — Not implemented
- [ ] Human publishes proposed eval — Not implemented
- [ ] Published evals become immediately active for future pipeline runs — Not implemented

### BDD Scenarios
- [ ] BDD feature file exists for feedback system — 7 real scenarios exist (not a placeholder), but eval proposal scenarios are not covered

### Library Contribution (8.20 ¶Eval suite growth #4, v2)
- [ ] Curated evals can be contributed back to community library — v2, not implemented

### Error Handling

- [x] ProgrammingError on all feedback API routes returns 501 with migration hint
- [x] SQLAlchemyError on all feedback API routes returns 503 with retry hint
- [x] Missing FeedbackRecord on detect-gap returns 404
- [x] Missing run_id on correction run creation returns 422
- [x] Invalid status transition returns 409 (InvalidTransitionError / ConcurrentModificationError)
- [x] Invalid status value on PATCH /status returns 422
- [x] Invalid review action returns 422
- [x] Concurrent modification guard on status transitions — UPDATE ... WHERE ... RETURNING pattern
- [x] Eval engine failure in detect_eval_gap — caught, logged, eval_gap=False returned
- [x] Empty eval_suite in detect_eval_gap — warning logged, returns True (gap assumed)
- [x] Node name resolution in proposals endpoint guarded by ProgrammingError→501 + SQLAlchemyError→503

### Edge Cases

- [x] Empty eval suite: detect_eval_gap returns True (gap assumed) with warning
- [x] No run_id on FeedbackRecord: detect-gap skips eval suite entirely (no pipeline evals to check)
- [x] Malformed eval_def in eval_suite: logged and skipped, does not crash detection
- [x] Multiple proposals with same run_id: node name resolution handles deduplication via dict
- [x] No run_ids in proposal set: node name resolution short-circuits, returns empty map
- [ ] Double gap detection on same record: eval_gap stays True after first run (idempotent read)
- [ ] Proposals endpoint with eval_gap=True but wrong status (e.g. "resolved"): excluded by filter
- [ ] Pipeline with 0 eval definitions returns empty eval_suite → gap assumed

### Resilience

- [x] EvalEngine.evaluate() failure in detect_eval_gap — caught, logged, iterates to next eval_def
- [x] Missing eval_suite — warning logged, True returned (gap assumed)
- [x] Run deleted between proposal list and node name resolution — no crash, node name simply absent
- [x] Snapshot graph_json missing or None — no crash, node name simply absent
- [x] Session context maintained across supplementary queries in proposals endpoint
- [x] All DB queries wrapped in ProgrammingError + SQLAlchemyError try/except

## Known Gaps
- **`detect_eval_gap()` now works** — iterates eval suite and returns True if all pass (gap) or False if any fails. The API endpoint now fetches eval definitions from the pipeline instead of passing `eval_suite=[]`.
- **No AI correction agent** — PRD 8.20 describes an agent that produces diagnosis + correction proposal + proposed eval case, but no code exists for it.
- **No eval proposal model** — proposals are currently just FeedbackRecords with `eval_gap=True`. No dedicated `EvalProposal` entity with draft fields (negative example, rubric, suggested eval type, publication status).
- **No draft eval editor UI** — PRD 8.20 mentions "Eval proposals queue with draft eval editor" in the feedback inbox UI, but there's no frontend or backend for editing/publishing draft evals.
- **BDD feature** — `feedback_system.feature` has 7 real scenarios (create, status transitions, invalid transitions, eval gap detection, correction run spawning) but none cover eval proposal generation or publication.

## QA History

### 2026-07-06 — Cross-cutting QA (improve-architecture index 228)
- Fixed CRITICAL — `list_eval_proposals` node name resolution queries ran outside `session.begin()` and try/except — now inside the same transaction with ProgrammingError + SQLAlchemyError guards
- Fixed CRITICAL — all 10 feedback routes added `except SQLAlchemyError → 503` (previously only caught ProgrammingError, allowing connection/deadlock failures to propagate as 500)
- Fixed MAJOR — corrected 4 stale `[ ]` → `[x]` behaviours in product map (detect-gap endpoint, proposals endpoint, proposals filtering, proposals ordering)
- Added Error Handling section (11 checkboxes: 11 [x]), Edge Cases section (8 checkboxes: 5 [x] + 3 [ ]), Resilience section (6 checkboxes: 6 [x]) to product map
- Added SQLAlchemyError → 503 test file (test_feedback_sqlalchemy_error.py) with 22 tests covering 10 routes
- Status remains `partial` (AI correction agent, eval proposal model, draft eval editor, BDD proposals scenarios all still gaps)