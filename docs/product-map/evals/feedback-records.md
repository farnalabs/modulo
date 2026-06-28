---
id: feat-evals-feedback-records
prd: §8.20
delivery-tasks: [task-nv4-feedback-record]
bdd:
  - backend/tests/bdd/features/eval/feedback_system.feature
code:
  - backend/src/modulo/core/feedback_manager/__init__.py
  - backend/src/modulo/api/routes/feedback.py
depends-on: [task-nv0-run-context-tests, task-nv2-eval-engine]
status: partial
---

# Feedback Records

Discovered from 1 completed delivery tasks.

## Behaviours

### Record Creation & Retrieval
- [ ] FeedbackRecord created on HITL rejection with all required fields (run_id, gate_id, rejected_by, rejection_reason, rejected_output, producing_node_id)
- [ ] FeedbackRecord creation accepts optional producing_agent_id
- [ ] FeedbackRecord creation accepts optional feedback_handler_type (defaults to "human")
- [ ] FeedbackRecord retrieval by ID returns full record
- [ ] FeedbackRecord retrieval returns None/404 for unknown ID
- [ ] Feedback records listable with pagination (page, page_size)
- [ ] Feedback records listable filtered by status
- [ ] Feedback records listable filtered by pipeline_id (via subquery on runs)
- [ ] Feedback records list returns total count when include_total=True
- [ ] Empty list returns total=0 with empty items array

### Status Transitions
- [ ] Valid transitions: pending → routing/correcting/dismissed
- [ ] Valid transitions: routing → escalated/correcting/resolved
- [ ] Valid transitions: correcting → correcting/resolved/escalated
- [ ] Valid transitions: escalated → resolved/dismissed (terminal)
- [ ] Valid transitions: resolved → (none — terminal)
- [ ] Valid transitions: dismissed → (none — terminal)
- [ ] Invalid transitions raise ValueError with descriptive message listing allowed transitions
- [ ] Unknown record returns None on status update
- [ ] RLS enforced on all status transitions (org-scoped)
- [ ] API rejects invalid status string with 422
- [ ] API updates status via PATCH /feedback/{id}/status

### Handler Types & Auto-Correction
- [ ] "human" handler: record stays in pending status (no auto-correction triggered)
- [ ] "ai_correction" handler: auto-transitions to correcting and spawns correction run
- [ ] "ai_correction_with_human_review" handler: auto-transitions to correcting and spawns correction run
- [ ] Pipeline-level default_feedback_handler applies to all HITL gates unless overridden
- [ ] Gate-level feedback_handler overrides pipeline default
- [ ] Validation error when both reject_target and feedback_handler set on same gate (reject_routing_conflict)

### Correction Run Mechanics
- [ ] Correction run creates new LangGraph thread pre-seeded from original checkpoint
- [ ] Correction run inherits original run's pipeline_id, snapshot_id, input_payload
- [ ] Correction run injects _feedback_correction block into input_payload
- [ ] _feedback_correction contains rejection_reason, rejected_output, producing_node_id, is_correction_run
- [ ] Correction run supports run_context_overrides merged into _feedback_correction
- [ ] Correction run sets trigger_type="correction" and parent_run_id
- [ ] Correction run copies original run's input_payload (handles None gracefully)
- [ ] Correction run linked to FeedbackRecord via correction_run_id
- [ ] Link transitions record status to "correcting"
- [ ] spawn_correction_run raises ValueError if FeedbackRecord not found
- [ ] spawn_correction_run raises ValueError if original run not found
- [ ] link_correction_run raises ValueError if record not in a state allowing "correcting" transition

### Post-Correction Evaluation
- [ ] Post-correction eval runs after correction run completes via EvalEngine.standalone_evaluate()
- [ ] ai_correction auto-resolves (status→resolved) on eval pass
- [ ] ai_correction_with_human_review resolves but sets needs_human_review=True on eval pass
- [ ] Eval failure does NOT auto-resolve status (record stays in correcting / is escalated upstream)
- [ ] Evaluation reads output from correction_run.outputs_json
- [ ] run_post_correction_eval raises ValueError if record not found
- [ ] run_post_correction_eval raises ValueError if record not in "correcting" status
- [ ] run_post_correction_eval raises ValueError if no correction_run_id linked
- [ ] run_post_correction_eval raises ValueError if correction run not found in DB

### Eval Gap Detection
- [ ] Gap detection runs pipeline's eval suite retrospectively against rejected output
- [ ] Tag record eval_gap if no existing eval scores the output as failing
- [ ] Uses standalone EvalEngine.evaluate(artifact, eval_suite) outside live LangGraph run
- [ ] detect_eval_gap returns False when no eval_suite provided
- [ ] detect_eval_gap endpoint returns eval_gap boolean
- [ ] detect_eval_gap returns 404 if record not found

### Feedback Inbox & Review
- [ ] Inbox endpoint returns paginated records across all pipelines
- [ ] Inbox filterable by handler_type, status, pipeline_id, date_from, date_to
- [ ] Inbox includes pipeline_name mapping per run
- [ ] Inbox item detail available via GET /feedback/inbox/{id}
- [ ] Inbox item detail includes pipeline name lookup
- [ ] Review action "mark_reviewed" transitions record to resolved (409 on impossible transition)
- [ ] Review action "dismiss" transitions record to dismissed (409 on impossible transition)
- [ ] Review action "create_correction_run" spawns correction run from inbox
- [ ] create_correction_run raises 422 if record has no associated run
- [ ] Invalid review action string returns 422
- [ ] Review on unknown record returns 404

### Eval Proposals
- [ ] Eval proposals endpoint lists records with eval_gap=True and status in [pending, routing]
- [ ] Eval proposals support pagination
- [ ] Empty proposals list returns total=0

### Auth & Security
- [ ] All feedback endpoints require authentication (401/403 on unauthenticated request)
- [ ] RLS org-scoping on all feedback operations (cross-org isolation)
- [ ] Create feedback validates run exists and belongs to user's org (404 if not)
- [ ] API serialises all FeedbackRecord fields in responses

### Integration / Persistence
- [ ] FeedbackRecord persists correctly in real DB session
- [ ] Pagination works end-to-end with real DB
- [ ] Status transitions work end-to-end with real DB (pending→routing→escalated)
- [ ] Correction run linking works with real DB FK constraints
- [ ] Records created with different handler types persist correctly

## Known Gaps
- BDD feature file (backend/tests/bdd/features/eval/feedback_system.feature) is a placeholder with no real scenarios
- detect_eval_gap implementation always returns False (eval_suite=[ ] hardcoded at the endpoint) — no real gap detection wired yet
- No correction run checkpoint pre-seeding logic implemented (spawn_correction_run creates a new run but doesn't inherit LangGraph checkpoint state)
- AI correction agent not implemented as a library primitive
- No feedback inbox UI implemented yet
- No eval proposals editor/curation UI
- Correction run does not route back through eval suite automatically (no run_post_correction_eval integration in run completion lifecycle)
- Pipeline-level default_feedback_handler not implemented (default_human hardcoded)
- No reject_routing_conflict validation in pipeline editor
- Library contribution (v2) not started
