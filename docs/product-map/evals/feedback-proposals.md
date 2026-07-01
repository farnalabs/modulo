---
id: feat-evals-feedback-proposals
prd: 8.20
delivery-tasks: [task-nv4-eval-proposals-queue]
bdd: [backend/tests/bdd/features/eval/feedback_system.feature]
code: [backend/src/modulo/core/feedback_manager/, backend/src/modulo/api/routes/feedback.py]
unit-tests: []
depends-on: [feat-evals-eval-definitions, feat-evals-feedback-routing]
status: partial
---

# Feedback Proposals — Eval Suite Growth

Discovered from 1 completed delivery tasks.

## Behaviours

### Eval Gap Detection (8.20 ¶Eval suite growth #1)

- [x] System runs pipeline eval suite against rejected output as standalone evaluation (EvalEngine.evaluate) — `detect_eval_gap()` now executes real eval suite
- [x] FeedbackRecord is tagged `eval_gap=True` when no eval scored the output as failing — logic implemented
- [ ] API endpoint `POST /feedback/{record_id}/detect-gap` triggers gap detection
- [ ] API endpoint returns `eval_gap` boolean in response

### Proposed Eval Generation (8.20 ¶Eval suite growth #2)
- [ ] AI correction agent or eval-proposal agent drafts a new eval case on `eval_gap` — Not implemented
- [ ] Proposed eval uses rejected output as negative example — Not implemented
- [ ] Proposed eval uses rejection reason as rubric — Not implemented
- [ ] Proposed eval suggests eval type (`llm_judge`, `regex`, `json_schema`) — Not implemented

### Eval Proposal Storage & Retrieval
- [ ] Proposed evals land in "Eval proposals" inbox — Partial: `get_eval_proposals()` queries `eval_gap=True` records but no dedicated model
- [ ] API endpoint `GET /feedback/proposals` returns proposals with pagination
- [ ] Proposals filtered by status `pending` or `routing` and `eval_gap=True`
- [ ] Proposals ordered by creation date descending

### Human Curation (8.20 ¶Eval suite growth #3)
- [ ] Human reviews proposed eval in draft eval editor — Not implemented
- [ ] Human edits proposed eval before publishing — Not implemented
- [ ] Human publishes proposed eval — Not implemented
- [ ] Published evals become immediately active for future pipeline runs — Not implemented

### BDD Scenarios
- [ ] BDD feature file exists for feedback system — Placeholder only (7 lines, no scenarios)

### Library Contribution (8.20 ¶Eval suite growth #4, v2)
- [ ] Curated evals can be contributed back to community library — v2, not implemented

## Known Gaps
- **`detect_eval_gap()` now works** — iterates eval suite and returns True if all pass (gap) or False if any fails. Still blocked: the API endpoint hardcodes `eval_suite=[]` so no evals are ever passed to it.
- **No AI correction agent** — PRD 8.20 describes an agent that produces diagnosis + correction proposal + proposed eval case, but no code exists for it.
- **No eval proposal model** — proposals are currently just FeedbackRecords with `eval_gap=True`. No dedicated `EvalProposal` entity with draft fields (negative example, rubric, suggested eval type, publication status).
- **No draft eval editor UI** — PRD 8.20 mentions "Eval proposals queue with draft eval editor" in the feedback inbox UI, but there's no frontend or backend for editing/publishing draft evals.
- **BDD feature is a placeholder** — `feedback_system.feature` has no scenarios. 