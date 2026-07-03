---
id: feat-evals-feedback-routing
prd: 8.20
delivery-tasks: []
code:
  - backend/src/modulo/core/feedback_manager/__init__.py
  - backend/src/modulo/api/routes/feedback.py
  - backend/src/modulo/db/models/feedback_record.py
bdd:
  - backend/tests/bdd/features/eval/feedback_system.feature
  - backend/tests/bdd/features/hitl/feedback_handler.feature
unit-tests:
  - backend/tests/unit/api/test_feedback_endpoint.py
  - backend/tests/unit/api/test_feedback_programming_error.py
  - backend/tests/unit/core/feedback_manager/test_feedback_manager.py
  - backend/tests/integration/feedback_manager/test_feedback_flow.py
depends-on:
  - feat-eval-engine
  - feat-pipeline-runs
status: partial
---

# Evals Feedback Routing

Every human rejection of a pipeline output is treated as structured signal.
The feedback system ingests rejection data, detects gaps in the eval suite,
spawns correction runs, and routes results back through automated or
human-in-the-loop resolution.

## Eval Gap Detection

- [x] `detect_eval_gap()` runs pipeline eval suite against rejected output via EvalEngine.standalone_evaluate()
- [x] Returns `True` when all evals pass (meaning no existing eval caught the failure — there's a gap)
- [x] Returns `False` when any eval fails (the existing suite already covers this failure mode)
- [x] Skips gap detection when no eval suite is provided
- [x] `POST /feedback/{record_id}/detect-gap` API endpoint exposes gap detection
- [ ] Eval gap detection auto-triggers on feedback creation (not yet wired)
- [ ] AI agent drafts proposed eval cases from detected gaps
- [ ] Proposed eval cases can be reviewed, edited, and published to active eval suite

## Eval Proposals Queue

- [x] `GET /feedback/proposals` lists records with `eval_gap=True` and status in `pending`/`routing`
- [x] Paginated response with `page`, `page_size`, `total`
- [x] Returns producing node name resolved from pipeline snapshot graph JSON
- [ ] No frontend UI for reviewing/publishing eval proposals
- [ ] No mechanism to promote a detected gap into an active eval definition

## Post-Correction Eval

- [x] `run_post_correction_eval()` evaluates correction run output via EvalEngine.standalone_evaluate()
- [x] For `ai_correction` handler: auto-resolves (transitions to `resolved`) when eval passes
- [x] For `ai_correction_with_human_review` handler: resolves + sets `needs_human_review=True`
- [x] Returns `passed`, `detail`, `score`, `needs_human_review` dict
- [x] Validates record exists, is in `correcting` status, and has linked correction run
- [x] Raises `ValueError` with specific message for each validation failure
- [ ] Post-correction eval runs automatically on correction run completion (no hook wired yet)

## Correction Run Mechanics

- [x] `spawn_correction_run()` creates a new run with `parent_run_id` linked to original
- [x] Copies original run's `pipeline_id`, `snapshot_id`, and `input_payload`
- [x] Injects `_feedback_correction` block into input payload with rejection metadata
- [x] Injected block includes: `rejection_reason`, `rejected_output`, `producing_node_id`, `is_correction_run`
- [x] Supports optional `run_context_overrides` for extending the correction block
- [x] `link_correction_run()` atomically sets `correction_run_id` + transitions to `correcting` status
- [x] Concurrent-safe: uses `FOR UPDATE` pattern on status updates via WHERE status match + returning
- [x] Raises `ValueError` if feedback record or original run not found
- [x] Auto-triggers correction run for `ai_correction` and `ai_correction_with_human_review` handlers on `create_feedback_record()`
- [ ] No LangGraph checkpoint seeding — correction runs start fresh, not pre-seeded
- [ ] No AI correction agent library primitive exists to produce diagnosis + correction proposal

## Status State Machine

- [x] Valid states: `pending`, `routing`, `correcting`, `resolved`, `escalated`, `dismissed`
- [x] DB-level CHECK constraint enforces valid status values
- [x] Valid transitions:
  - `pending` → `routing`, `correcting`, `dismissed`
  - `routing` → `escalated`, `correcting`, `resolved`
  - `correcting` → `correcting`, `resolved`, `escalated`
  - `escalated` → `resolved`, `dismissed`
  - `resolved` → terminal
  - `dismissed` → terminal
- [x] Invalid transitions raise `ValueError` with descriptive message listing allowed transitions
- [x] Concurrent transition detection: `WHERE feedback_status == current_status` pattern detects stale updates
- [x] UPDATE ... RETURNING pattern ensures atomic read-after-write on status changes
- [x] `PATCH /feedback/{record_id}/status` validates status in allowed set before delegation
- [x] Review endpoint actions: `mark_reviewed` (→resolved), `dismiss` (→dismissed), `create_correction_run` (→correcting)

## API Endpoints

- [x] `POST /api/v1/runs/{run_id}/feedback` — create feedback record (201)
- [x] `GET /api/v1/feedback` — list feedbacks with status/pipeline filter, paginated
- [x] `GET /api/v1/feedback/{record_id}` — get single feedback record
- [x] `PATCH /api/v1/feedback/{record_id}/status` — update feedback status with validation
- [x] `POST /api/v1/feedback/{record_id}/detect-gap` — run eval gap detection
- [x] `GET /api/v1/feedback/inbox` — inbox with filters (handler_type, status, pipeline_id, date range)
- [x] `GET /api/v1/feedback/inbox/{record_id}` — inbox item detail with pipeline name
- [x] `POST /api/v1/feedback/inbox/{record_id}/review` — review actions (mark_reviewed, dismiss, create_correction_run)
- [x] `GET /api/v1/feedback/proposals` — eval proposals queue
- [x] All endpoints return serialised response via `_serialise_record()` helper
- [x] Inbox endpoint resolves pipeline names via Run → Pipeline join
- [x] Proposals endpoint resolves producing node names from pipeline snapshot graph JSON
- [ ] No `reject_routing_conflict` validation — no gate-level check for setting both `reject_target` and `feedback_handler`

## Error Handling / ProgrammingError Catches

- [x] Every API route wraps DB operations in `try/except ProgrammingError`
- [x] Returns 501 Not Implemented with descriptive migration message
- [x] 404 when feedback record or run not found
- [x] 409 Conflict on concurrent status transitions
- [x] 422 for invalid status values or review actions
- [x] FeedbackManager methods raise `ValueError` with specific messages (not generic exceptions)

## Concurrency Safety

- [x] Status update uses `WHERE status == expected_current` to detect concurrent modifications
- [x] `UPDATE ... RETURNING` returns `None` when WHERE clause matches no rows (concurrent change detected)
- [x] `link_correction_run()` uses same WHERE status check pattern
- [x] Error messages include record ID and expected/actual status for debugging
- [x] RLS is enforced on every manager method via `@_rls` decorator calling `set_rls_org()`
- [x] All queries scope to `organisation_id`

## DB Model (`FeedbackRecord`)

- [x] Fields: `organisation_id`, `run_id`, `gate_id`, `account_id`, `rejection_reason`, `rejected_output`
- [x] Fields: `producing_node_id`, `producing_agent_id`, `feedback_status`, `feedback_handler_type`
- [x] Fields: `correction_run_id`, `eval_gap`, `needs_human_review`
- [x] CHECK constraint on `feedback_status`: `pending`, `routing`, `correcting`, `resolved`, `escalated`
- [x] CHECK constraint on `feedback_handler_type`: `human`, `ai_correction`, `ai_correction_with_human_review`
- [x] Foreign keys: `run_id` → runs.id (CASCADE), `account_id` → accounts.id (RESTRICT), `correction_run_id` → runs.id (SET NULL), `producing_agent_id` → agents.id (SET NULL)

## Known Gaps

- **AI correction agent primitive** — no agent exists to produce diagnosis + correction proposal. `spawn_correction_run()` creates a run but the executor has no built-in correction agent logic.
- **Correction run checkpoint seeding** — runs are fresh, not pre-seeded from a LangGraph checkpoint of the original run. The correction run re-executes from scratch rather than branching from the point of failure.
- **`reject_routing_conflict` validation** — no gate-level validation catches the case where both `reject_target` and `feedback_handler` are set on the same gate.
- **Proposed eval generation** — `eval_gap` triggers detection but no AI agent drafts proposed eval cases. The proposals queue exists but has no mechanism to promote a gap into an active eval definition.
- **Eval proposals inbox UI** — only API endpoint exists, no frontend for reviewing, editing, or publishing proposals.
- **Frontend views** — no frontend exists for any feedback system views (inbox, detail, review). The system is backend-only (API + tests).
- **Auto-trigger detection gap** — eval gap detection is not automatically triggered on feedback creation; it requires an explicit API call to `POST /feedback/{record_id}/detect-gap`.
- **Post-correction eval auto-trigger** — no hook calls `run_post_correction_eval()` automatically when a correction run completes.
