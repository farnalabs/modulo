---
id: feat-evals-feedback-records
prd: 8.20
delivery-tasks: [task-nv4-feedback-record]
  - backend/tests/bdd/features/eval/feedback_system.feature
code:
  - backend/src/modulo/core/feedback_manager/__init__.py
  - backend/src/modulo/api/routes/feedback.py
depends-on: [feat-core-run-context, feat-evals-eval-engine]
status: partial
---

# Feedback Records

Discovered from 1 completed delivery tasks.

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

## Known Gaps

- BDD feature file (backend/tests/bdd/features/eval/feedback_system.feature) has 5 real scenarios (not a placeholder) — covers create, status transitions, invalid transitions, eval gap detection, and correction run spawning
- detect_eval_gap now returns True when all evals pass against rejected output (gap detected). The API endpoint still hardcodes eval_suite=[] — real pipeline eval suite population not connected yet.
- No correction run checkpoint pre-seeding logic implemented (spawn_correction_run creates a new run but doesn't inherit LangGraph checkpoint state)
- AI correction agent not implemented as a library primitive
- No feedback inbox UI implemented yet
- No eval proposals editor/curation UI
- Correction run does not route back through eval suite automatically (no run_post_correction_eval integration in run completion lifecycle)
- Pipeline-level default_feedback_handler not implemented (default_human hardcoded)
- No reject_routing_conflict validation in pipeline editor
- Eval failure does NOT escalate to "escalated" status (record stays in "correcting" — partial gap per PRD §8.20)
- Library contribution (v2) not started
