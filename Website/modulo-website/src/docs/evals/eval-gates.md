---
title: Eval Gates
description: Conditional HITL gating, eval-before-interrupt, and post-run suite-level threshold enforcement for pipeline nodes.
---

Eval gates extend HITL (Human-In-The-Loop) gates with automated quality evaluation. Before a pipeline pauses for human review, node-scoped eval definitions are evaluated against the node's output. Block-level failures prevent the interrupt entirely and transition the run to `eval_failed`. After a run completes, eval suites with `pass_threshold` can downgrade a `complete` run to `failed` if aggregate scores fall below the threshold.

## Key Concepts

### Conditional HITL Gating

HITL gate configurations support two independent gating mechanisms:

- **JMESPath condition**: a free-form `condition` expression evaluated against pipeline state at runtime. When the result is falsy, the gate is skipped entirely with a `condition_skipped` artifact — no interrupt is raised and no eval is evaluated.
- **Eval-reference condition**: an `eval_condition` with `{eval_name, threshold, operator}` that references the result of an eval definition evaluated against the upstream node's output. When the eval's score compared to the threshold (using the specified operator like `lt`, `gt`, `eq`) produces `true`, the gate proceeds to autonomy checks and may fire an interrupt.

Both mechanisms are evaluated on the gate's first visit only. On resume (when the human has already decided), the `_hitl_decision` check takes priority and neither the condition nor the evals are re-evaluated.

### Eval-Before-Interrupt

Node-scoped eval definitions can be attached to the upstream node of a HITL gate. These are evaluated after the JMESPath condition check but before any autonomy or interrupt logic:

1. Each eval definition is evaluated against the node's output (the full LangGraph state dict).
2. Results are logged and persisted to the `eval_results` table.
3. If any eval with `failure_behaviour="block"` fails, an `EvalBlockedError` is raised instead of a `NodeInterrupt`. The run transitions to `eval_failed` with `error_code="eval_blocked"`.
4. Evals with `failure_behaviour="warn"` that fail log a warning but do not block — the gate proceeds normally.

Because block failures propagate as exceptions, remaining evals after a block failure are not evaluated. This is by design — a blocked run stops immediately.

### Autonomy Integration

The gate's decision to interrupt defers to the effective autonomy level from `run_context`:

| Autonomy level | Behaviour |
|---|---|
| `manual_approval` (default) | Gate fires a `NodeInterrupt` after condition/eval checks pass. |
| `notify_on_complete` | Gate auto-approves without interrupt. An artifact records the notification. |
| `fully_autonomous` | Gate is silently skipped — no interrupt, no artifact. |
| `human_only` flag | Overrides all autonomy levels — always interrupts. |

### Suite-Level Post-Run Threshold Checks

After a run completes successfully, the executor loads eval definitions with `suite_id` and `pass_threshold`. For each suite:

1. All eval results belonging to that suite for the current run are loaded from the `eval_results` table.
2. An `aggregate_score` is computed as `passed_evals / total_evals` (1.0 for empty suites).
3. If `aggregate_score < pass_threshold`, an `EvalSuiteBlockedError` is raised.
4. The run status transitions from `complete` to `failed` with `error_code="eval_suite_blocked"`.
5. An audit event is recorded with the suite ID, score, and threshold.

Suites without a `pass_threshold` are never checked — they return aggregate results but never transition the run.

## Reference

- [PRD §8.17](https://github.com/farnalabs/modulo/blob/main/docs/prd.md#817-conditional-transitions)
- [Product Map](/docs/product-map/evals/eval-gates.md)
- [Conditional Transitions](/docs/evals/conditional-transitions.md)
- [Eval Editor](/docs/evals/eval-editor.md)
