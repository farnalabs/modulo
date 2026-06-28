---
id: feat-core-run-context
prd: §8.18
delivery-tasks: [task-nv0-complexity-reviewer, task-nv0-run-context-tests]
bdd:
  - backend/tests/features/triggers/manual.feature
  - backend/tests/features/pipelines/run_sequential.feature
  - backend/tests/features/evals/conditional_hitl.feature
  - backend/tests/features/triggers/webhook_payload_mapping.feature
  - backend/tests/features/errors/retry.feature
  - backend/tests/features/errors/recovery.feature
  - backend/tests/features/mcp/trigger.feature
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
  - backend/tests/unit/pipeline_engine/test_decorator.py
depends-on: [task-nv0-complexity-reviewer]
status: partial
---

# Run Context

## Behaviours

### Seeding
- [x] run_context seeded from PipelineSnapshot.run_context_defaults at run start
- [ ] Trigger run_context_overrides merge over pipeline defaults (later wins)
- [x] Empty defaults produce empty run_context dict
- [x] Pipeline snapshot captures run_context_defaults at snapshot time (not live pipeline)
- [ ] HITL-paused resume uses snapshot's run_context_defaults, not current pipeline defaults

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
- [ ] Write-log preserved across HITL checkpoints
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

### Error states
- [ ] Invalid run_context_overrides type (non-dict) raises validation error
- [ ] Node timeout while writing run_context — context write not persisted
- [x] Non-context-setter violation raises error (PRD specifies silent discard + audit warning — code currently raises) — design tension
- [ ] Graph validation warns on parallel context-setters writing same key (v1)
- [ ] Retry with run_context_overrides — retry uses provided overrides
- [ ] Cancellation-while-waiting for capacity slot transitions to cancelled status

### Security
- [x] Context-setter role evaluated at LangGraph-node level, not agent config level
- [ ] Audit event emitted on context_write_by_non_setter violation
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
- [ ] All unit and BDD tests pass

## Known Gaps

- **Non-context-setter guard strategy mismatch**: PRD §8.18 specifies silent discard + audit_warning event for non-context-setter writes to run_context. Current decorator code raises ContextSetterViolationError instead. The PRD's stated intent is non-breaking behaviour; the code takes a hard-fail approach. Needs resolution: either update PRD to match code (hard error), or update code to match PRD (silent discard).
- **Trigger override merging not wired through executor**: `run_context_overrides` from trigger events are not merged in `_seed_state` — only `snapshot.run_context_defaults` is used. The trigger override code path is not yet connected to the executor.
- **Missing BDD features for context-setter guard, write-log, and autonomy resolution**: Existing BDD tests cover seeding and override merging but not the core read/write enforcement mechanics. The only dedicated run_context BDD feature file (`backend/tests/bdd/features/complexity/complexity_reviewer.feature`) is a placeholder with no scenarios.
- **No template rendering test for run_context**: The product map entry lists `{{ run_context.key }}` access as a behaviour, but there is no test verifying that run_context fields are interpolated in prompt templates.
- **Run inspection UI for context diffs and write-log**: Not yet implemented — frontend run detail view needs to surface run_context state diff per-node and display write-log.
- **Parallel context-setter conflict warning**: PRD specifies a pipeline validation warning when parallel context-setters write the same key (v1). Not yet implemented.
- **Complexity-reviewer end-to-end test**: No integration test verifying the canonical library primitive writes correct fields and downstream agents can consume them.
