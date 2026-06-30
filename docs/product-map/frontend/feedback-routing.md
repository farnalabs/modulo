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
depends-on: [feat-evals-feedback-records]
status: partial
---

# Feedback System — Routing

## Behaviours

### FeedbackRecord lifecycle

- [x] HITL rejection creates FeedbackRecord with status `pending`
- [x] FeedbackRecord stores rejection reason, rejected output, producing node/agent
- [x] Status state machine enforces valid transitions: pending → routing → correcting → resolved | escalated
- [x] `dismissed` is a terminal status from pending/note for future
- [x] Invalid transitions return validation error
- [x] FeedbackRecord is immutable after creation

### Handler types and routing strategy

- [x] Pipeline `default_feedback_handler` cascades to all HITL gates unless overridden
- [x] Gate-level `feedback_handler` overrides pipeline default
- [x] `human` handler: FeedbackRecord surfaced in inbox for manual review
- [x] `ai_correction` handler: auto-spawns correction run, auto-resolves if evals pass
- [x] `ai_correction_with_human_review` handler: auto-spawns correction run, marks `needs_human_review`
- [ ] `feedback_handler` supersedes `reject_target` when both set on same gate
- [ ] Setting both `feedback_handler` and `reject_target` is a validation error (`reject_routing_conflict`)

### Correction run mechanics

- [x] Correction run spawning linked to FeedbackRecord via `correction_run_id`
- [x] Correction run injects `_feedback_correction` payload into run_context
- [ ] Correction run pre-seeded with checkpoint state from target_node_id (fresh run, not seeded — known gap)
- [x] Correction run uses same PipelineSnapshot as original run
- [x] `parent_run_id` links correction run to original run
- [x] Correction run goes through full eval suite before reaching HITL gate again
- [x] Eval failure during correction run → status set to `escalated`
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

### Feedback inbox UI

- [x] Inbox shows all pending FeedbackRecords across all pipelines
- [x] Inbox filterable by status, pipeline, producing agent
- [x] Inbox filterable by date range
- [x] `human` handler: annotation UI with notes and manual correction trigger
- [x] `ai_correction_with_human_review`: correction proposal display with accept/reject
- [ ] Eval proposals queue with draft eval editor
- [x] Paginated results

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
- [ ] BDD: HITL rejection creates FeedbackRecord
- [ ] BDD: Feedback routed per handler type (human / ai_correction / ai_correction_with_human_review)
- [ ] BDD: Correction run spawned and executes
- [ ] BDD: Eval gap detection triggers proposed eval generation
- [ ] BDD: Feedback inbox review actions complete the lifecycle

## Known Gaps

- **Correction run checkpoint seeding**: Correction run creates a fresh run rather than seeding from original LangGraph checkpoint at target_node_id (per PRD 8.20: "pre-seeded with checkpoint state")
- **No AI correction agent library primitive**: PRD 8.20 describes an agent that produces diagnosis + correction proposal + proposed eval case, but no code exists for it
- **No `reject_routing_conflict` validation**: Gate-level validation for setting both `feedback_handler` and `reject_target` on the same gate not yet implemented
- **No eval proposals UI**: Eval proposals queue with draft eval editor (PRD 8.20 ¶1495) not yet built
- **BDD features placeholder only**: Both `feedback_system.feature` and `feedback_handler.feature` contain only placeholder scenarios
