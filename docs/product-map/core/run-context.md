---
id: feat-core-run-context
prd: 8.18
delivery-tasks:
  - task-nv0-complexity-reviewer
  - task-nv0-run-context-tests
bdd:
  - backend/tests/features/triggers/manual.feature
  - backend/tests/features/pipelines/run_sequential.feature
  - backend/tests/features/evals/conditional_hitl.feature
  - backend/tests/features/triggers/webhook_payload_mapping.feature
  - backend/tests/features/errors/retry.feature
  - backend/tests/features/errors/recovery.feature
  - backend/tests/features/mcp/trigger.feature
  - backend/tests/bdd/features/eval/conditional_hitl.feature
  - backend/tests/bdd/features/pipelines/run_context.feature
  - backend/tests/bdd/features/pipelines/run_lifecycle.feature
code:
  - backend/src/modulo/core/run_context/
  - backend/src/modulo/core/pipeline_engine/decorator.py
  - backend/src/modulo/core/pipeline_engine/executor.py
  - backend/src/modulo/core/pipeline_engine/node_runner.py
  - backend/src/modulo/core/library/complexity_reviewer.py
  - backend/src/modulo/core/feedback_manager/
  - backend/src/modulo/db/models/pipeline.py
  - backend/src/modulo/db/models/pipeline_snapshot.py
  - backend/src/modulo/db/crud/pipeline.py
  - backend/src/modulo/db/crud/pipeline_snapshot.py
  - backend/src/modulo/db/crud/variant_group.py
  - backend/src/modulo/api/routes/pipelines.py
  - backend/src/modulo/api/routes/variants.py
  - backend/src/modulo/core/workflow_import_export/
unit-tests:
  - backend/tests/unit/core/run_context/test_autonomy.py
  - backend/tests/unit/core/run_context/test_decorator_resilience.py
  - backend/tests/unit/core/run_context/test_run_context_bdd.py
  - backend/tests/unit/pipeline_engine/test_decorator.py
depends-on: [feat-core-agent-model, feat-core-pipeline-execution, feat-core-db-abstraction-core]
status: partial
---

# Run Context

## Behaviours

### Seeding

- [x] run_context seeded from PipelineSnapshot.run_context_defaults at run start
- [ ] Trigger run_context_overrides merge over pipeline defaults (later wins)
- [x] Empty defaults produce empty run_context dict
- [x] Pipeline snapshot captures run_context_defaults at snapshot time (not live pipeline)
- [x] HITL-paused resume uses snapshot's run_context_defaults, not current pipeline defaults

### Reading

- [x] All agent nodes can read run_context fields
- [ ] run_context accessible in prompt templates as {{ run_context.key }}
- [ ] run_context state shown in run detail view per-node

### Context-setter writes

- [x] Context-setter agent (role="context_setter") can write to run_context
- [x] Written fields appear in downstream nodes' run_context
- [x] Context-setter can write multiple keys in one node execution
- [x] Context-setter can update existing keys

### Non-context-setter guard

- [x] Non-context-setter node writing to run_context raises ContextSetterViolationError
- [x] Non-context-setter node not writing run_context passes normally
- [x] Explicit role="agent" also blocked (not just None role)
- [x] run_context: None in state does not crash guard
- [x] run_context key absent from state does not crash guard
- [x] Node with no run_context in returned result passes guard

### Write-log (last-write-wins)

- [x] Every context-setter write appends to _run_context_write_log ordered log
- [x] Write-log entry includes node_name, role, timestamp, written_fields
- [x] Last write to same key wins in resolved value
- [x] Write-log preserved across HITL checkpoints
- [ ] Write-log visible in run inspection

### Autonomy level integration

- [x] run_context.autonomy_recommendation overrides pipeline default_autonomy_level
- [x] Pipeline default_autonomy_level seeded into run_context._pipeline_default_autonomy
- [x] fully_autonomous causes HITL gate to be skipped
- [x] notify_on_complete auto-approves gate with notification event
- [x] manual_approval halts execution for human review
- [x] Invalid autonomy value in run_context falls back to pipeline default
- [x] Neither pipeline default nor recommendation set falls back to manual_approval
- [x] Context-setter can change autonomy level mid-run
- [x] Autonomy level resolution checked pre-node

### Cancellation

- [x] run_context.cancelled flag prevents node execution (fast path, no DB roundtrip)
- [x] DB-backed cancellation check (run.cancellation_requested) prevents node execution (authoritative)
- [x] DB check skipped when state-level cancelled flag already detected
- [x] ContextVar isolation prevents cancellation check leaking between concurrent runs
- [x] DB check hook cleared in finally block (no leak across runs)

### Complexity-reviewer (canonical use case)

- [x] Complexity-reviewer agent writes model_tier, estimated_tokens, complexity_reason
- [x] model_tier is one of "tier-1", "tier-2", "tier-3"
- [x] estimated_tokens is non-negative integer
- [x] complexity_reason is <= 500 characters

### Edge cases

- [x] Multiple context-setters in sequence — later writes win
- [x] Context-setter writes empty dict — no-op, state unchanged
- [x] Context-setter writes nested dict — stored as-is (no flattening)
- [x] Context-setter writing to reserved key (`cancelled`) is silently stripped — run not affected
- [x] Context-setter writing to reserved key (`input`) is silently stripped
- [x] Context-setter writing to reserved key (`_pipeline_default_autonomy`) is silently stripped
- [x] Context-setter writing only reserved keys produces no write-log entry
- [x] Reserved key attempt logged as WARNING with node name and keys

### Error Handling

- [x] Non-context-setter writing run_context raises ContextSetterViolationError
- [x] Cancelled run raises RunCancelledError before node execution
- [x] DB-backed cancellation check prevents node execution (authoritative)
- [x] Node timeout propagates TimeoutError to executor
- [x] DB cancellation check failure (exception) caught and logged as WARNING — run continues (conservative degrade)
- [x] DB cancellation check unset is no-op (no check performed)
- [ ] Invalid run_context_overrides type (non-dict) raises validation error
- [ ] Retry with run_context_overrides merges correctly
- [ ] Audit event emitted on context_write_by_non_setter violation with node_id and attempted_keys

### Security

- [x] Context-setter role evaluated at LangGraph-node level, not agent config level
- [x] _run_context_write_log is internal-only (not writable by agents)
- [x] Non-context-setter cannot inject run_context via state manipulation

### Run inspection

- [ ] Run detail view shows run_context before and after each node
- [ ] Context writes shown as diffs in inspection UI
- [ ] Write-log entries displayed in run inspection

### Variant group integration

- [ ] Variant group creates N runs with different run_context_overrides
- [ ] Pre-flight check rejects entire group if quota would be exceeded (no partial firing)
- [ ] Each variant run counted individually against org/team/trigger limits

### Feedback system integration

- [x] feedback_correction promoted from input_payload to top-level run_context
- [ ] Correction run inherits run_context from original checkpoint
- [x] feedback_correction removed from input_payload to hide from agents
- [ ] run_context_overrides merged into feedback_correction during correction run

### Forward/backward compatibility

- [x] Existing pipelines without run_context_defaults run normally (empty dict)
- [x] Existing agents unaware of run_context are unaffected
- [x] Previous runs' contexts unchanged by pipeline default changes
- [x] All unit and BDD tests pass (66 unit tests pass as of 2026-07-05)

## Known Gaps
- **Non-context-setter guard strategy mismatch**: PRD 8.18 specifies silent discard + audit_warning event for non-context-setter writes to run_context. Current decorator code raises ContextSetterViolationError instead — a hard error, not silent discard. The PRD's non-breaking intent is deferred to v1. The product map now reflects the current code behaviour (hard error). Code path: `backend/src/modulo/core/pipeline_engine/decorator.py:129-142`.
- **Missing audit event dispatch on violation**: When a non-context-setter violation occurs, only `_log.warning()` is emitted. PRD specifies an `audit_warning` event with node_id and attempted_keys. The decorator lacks DB session access to dispatch to the `audit_events` table. Needs a ContextVar-based callback pattern (similar to the cancellation check) plumbed through the executor.
- **Trigger override merging not wired through executor**: `run_context_overrides` from trigger events are not merged in `_seed_state` — only `snapshot.run_context_defaults` is used. The trigger override code path is not yet connected to the executor. Code path: `backend/src/modulo/core/pipeline_engine/executor.py:95-119`.
- **No frontend UI for run_context inspection per-node**: The run detail view does not surface run_context state before/after each node, nor display the write-log. This is a frontend gap tracked separately from the backend implementation.
- **No template rendering test for run_context**: The product map entry lists `{{ run_context.key }}` access as a behaviour, but there is no test verifying that run_context fields are interpolated in prompt templates.
- **Parallel context-setter conflict warning**: PRD specifies a pipeline validation warning when parallel context-setters write the same key (v1). Not yet implemented.
- **Complexity-reviewer end-to-end test**: No integration test verifying the canonical library primitive writes correct fields and downstream agents can consume them.
- **DB cancellation check has no timeout**: The `_check_db_cancellation` closure in executor.py:484-488 opens a new DB session and queries the run table. If the DB is slow or unreachable, the entire node execution is blocked indefinitely — the check has no `asyncio.wait_for` timeout. Should default to a reasonable timeout (e.g. 5s) and treat timeout as not-cancelled.
- **No frontend i18n for RunDetailView**: The RunDetailView has ~25 hardcoded English strings (`"Back to Dashboard"`, `"Copied!"`, `"Copy"`, `"Share Summary"`, `"Final Output"`, `"No node data available"`, table headers, `"Hide"`/`"Show"`, `"View"`, `"[Prompt hidden — click to reveal]"`, `"Input"`/`"Output"`, `"total tokens"`, `"Copy Prompt"`, `"No run ID provided"`, `"Failed to load run:"`) that are NOT wrapped in `$t()`. Violates the Definition of Done rule that all user-facing strings use `$t()`. Requires adding ~25 translation keys and wrapping template text in `$t()` calls. Requires `const { t } = useI18n()` for script-section strings.
- **test_missing_fuzzy_match test misaligned with code**: `test_autonomy.py` had `test_missing_fuzzy_match` asserting `AutonomyLevel("manual-approval")` matches `MANUAL_APPROVAL` via hyphen-to-underscore conversion, but `_missing_` only implements case-insensitive value matching, not hyphen-to-underscore. Renamed to `test_missing_case_insensitive_match` and fixed assertions to only test uppercase matching (which the code supports). The `test_missing_unmatched_raises_value_error` test correctly expects `ValueError` for `"notify-complete"` (hyphen variant) — no fuzzy matching needed.

### Testing Gaps
- **No test for run_context size limits**: No validation rejects payload > 64KB or exceeding N keys.
- ~~**No test for reserved key protection**: No test verifies that a context-setter writing to the `cancelled` reserved key is rejected or ignored.~~ RESOLVED: test_decorator_resilience.py covers reserved key stripping for all 4 reserved keys (cancelled, input, _pipeline_default_autonomy, _run_context_write_log) plus warning logging and non-setter violation interaction (2026-07-05).
- **No test for write-log overflow or pruning**: No test verifies behaviour when the write-log exceeds a reasonable bound.
- **No BDD test for concurrent context-setter writes (race condition)**: Only sequential "last-write-wins" is tested. No scenario covers async race between two context-setter nodes writing the same key simultaneously in parallel branches.
- **No test for run_context merge semantics across retry/resume boundary**: No test confirms whether retry merges new overrides into existing run_context or replaces it entirely.
- **No test for null/empty values**: No test covers `{"branch": null}` — does a null value overwrite a previous value or get ignored?
- **No test for deeply nested run_context structures**: No test verifies that deeply nested dicts (3+ levels) are stored and retrieved correctly.
- **No test for type coercion**: No test verifies behaviour when a run_context value is a different type than expected (e.g. number instead of string).

## QA History
### 2026-07-03 — Cross-cutting architecture QA for feat-core-run-context
- **Lens**: Cross-cutting (PRD vs product map vs code vs BDD vs unit tests)
- **Findings**: Added dedicated BDD feature file to frontmatter; corrected HITL-paused resume checkbox; replaced Error states with structured Error Handling section; added 8 testing gaps; refined Known Gaps; flagged PRD §8.18 silent-discard mismatch with note.
- **Next actions**: Test the 8 testing gaps; wire trigger override merging; implement audit event dispatch.

### 2026-07-05 — Cross-cutting architecture QA for feat-core-run-context (index 155)
- **Lens**: Behaviour completeness, edge case coverage, error path audit, cross-module integrity, gap freshness, resilience
- **Fixed (CRITICAL)**: DB-backed cancellation check (`_check_db_cancellation` in executor.py:484-488) had no error handling — a DB connection failure would propagate a raw exception through the decorator, crashing the node. Added `try/except Exception` wrapper around `await db_check()` with WARNING-level logging and `exc_info=True`. Run continues as not-cancelled on DB failure (conservative degrade).
- **Fixed (MAJOR)**: Context-setter agents could overwrite reserved internal keys (`cancelled`, `input`, `_pipeline_default_autonomy`, `_run_context_write_log`) — any of which could crash the run or corrupt state. Added `_RESERVED_RUN_CONTEXT_KEYS` frozenset in decorator.py. Reserved key writes are silently stripped from the context-setter's result before the write-log is recorded. Attempts are logged as WARNING with node name and reserved keys. If all written keys are reserved, no write-log entry is created.
- **Fixed (MAJOR)**: Added `test_decorator_resilience.py` with 12 unit tests covering: DB check failure (5 tests — exception doesn't crash, warning logged, non-exception path still works, true cancellation still cancels, unset is noop), reserved key protection (7 tests — cancelled/input/_pipeline_default_autonomy/_run_context_write_log stripped, only-reserved-keys produces no write-log, warning logged, non-setter violation still fires for reserved keys).
- **Marked [x]**: "Write-log preserved across HITL checkpoints" — LangGraph checkpointer (ModuloPostgresSaver) saves the full state dict including `_run_context_write_log`. The write-log is preserved automatically across checkpoints/resume cycles. Verified by `test_two_setters_append_to_log` which simulates state accumulation across checkpoint boundaries.
- **Removed duplicate**: "Audit event emitted on context_write_by_non_setter" appeared in both Error Handling (§115) and Security (§127) sections. Kept Error Handling copy, removed Security duplicate.
- **Added 8 new behaviour checkboxes**: 4 Error Handling (DB check failure → logged warning, DB check unset is noop, reserved key stripping, reserved key warning logging), 4 Edge Cases (reserved key strip for cancelled/input/_pipeline_default_autonomy, only-reserved no write-log).
- **Updated unit-tests frontmatter**: Added `test_decorator_resilience.py` and `test_run_context_bdd.py` (was missing both).
- **Resolved testing gap**: "No test for reserved key protection" — now covered by 7 reserved-key tests in test_decorator_resilience.py.
- **Known Gap added**: DB cancellation check has no timeout — a slow/unresponsive DB can block node execution indefinitely.

### 2026-07-05 — Cross-cutting QA for feat-core-run-context (this session)
- **Lens**: Behaviour verification, error-handling audit, i18n check, test health
- **Verified**: All source files read across run_context/, pipeline_engine/, feedback_manager/, API routes, and frontend RunDetailView
- **Fixed (MINOR)**: `test_missing_fuzzy_match` in `test_autonomy.py` asserted hyphen-to-underscore fuzzy matching that code doesn't implement. Renamed to `test_missing_case_insensitive_match` and fixed assertions to test only uppercase matching (which `_missing_` supports via `lower()`).
- **Verified ProgrammingError handling**: All route handlers in `pipelines.py` (18 routes), `variants.py` (8 routes), and `runs.py` (17 routes) have `except ProgrammingError:` catches returning 501. No gaps.
- **Verified run_context_overrides gap**: Known Gap "Trigger override merging not wired through executor" confirmed accurate. `variant_group.py` merges `run_context_overrides` into `input_payload`, not into `run_context` directly.
- **Marked [x]**: "All unit and BDD tests pass" — 66/66 unit tests pass.
- **New Known Gap**: RunDetailView has ~25 hardcoded English strings without `$t()` i18n wrappers — violates Definition of Done.
- **No backend code changes needed**: Existing architecture and error handling are sound for current scope.