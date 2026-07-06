---
title: Conditional Transitions
description: Route execution and gate HITL interrupts based on evaluation results and state conditions.
---

Conditional transitions provide two routing mechanisms within pipelines: (A) **conditional edges** that route execution to different target nodes based on JMESPath expressions evaluated against pipeline state, and (B) **conditional HITL gating** that skips the gate when a JMESPath condition or eval-reference condition evaluates as falsy.

## Key Concepts

### Conditional Edge Routing

Edges with `type: "conditional"` use a JMESPath `condition_expression` to determine the next node at runtime. Normal edges from the same source serve as fallback targets. See PRD §8.17 for full behaviour specification.

### Conditional HITL Gating

HITL gate configurations support:

- **JMESPath condition**: a free-form `condition` expression evaluated against state — when falsy, the gate is skipped with a `condition_skipped` artifact.
- **Eval-reference condition**: an `eval_condition` with `{eval_name, threshold, operator}` — when the eval score is below the threshold, a `NodeInterrupt` is raised.

## Reference

- [PRD §8.17](https://github.com/farnalabs/modulo/blob/main/docs/prd.md#817-conditional-transitions)
- [Product Map](/docs/product-map/evals/conditional-transitions.md)
