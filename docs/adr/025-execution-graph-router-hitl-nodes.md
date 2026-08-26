# ADR 025 — Execution Graph Router & HITL Nodes (FAR-402 P1 / FAR-415)

**Status:** Accepted
**Date:** 2026-08-24
**T-shirt:** M
**Parent design:** FAR-402 execution-graph composition ergonomics
**Supersedes / relates to:** §3 F2, §4 A/C/D, §10 of `docs/design/execution-graph-composition.md` (PR #1895)

## Context

The execution graph already supports powerful branching (conditional JMESPath
edges, LLM routing, `loop` edges compiled only in `graph_json`), HITL (an
**edge** property realized as a synthetic `hitl_gate_*` node at compile time),
and a `manual` node (human *output*). These capabilities are buried in edge
strings and synthetic nodes, so they are hard to author and visually
inconsistent. FAR-402 P1 ("Ergonomics", low-risk, reuses existing machinery)
promotes them to first-class, authorable nodes:

- **Router node** — a visual decision node with ordered `{guard (JMESPath),
  target}` rules, first-match-wins, plus an explicit `default` rule.
- **HITL node** — a draggable human-in-the-loop gate (the legacy edge-gate
  HITL, promoted to a node; edge-gate HITL remains compile-supported).
- **Taxonomy reconciliation** — `loop` edge type promoted to the API schema
  (it was already compiled), `connector` kept internal-only, node/edge types
  kept as snake_case string values (no new Python `Enum`).

## Decision

### Router node (F2-A)

- `router` added to `PipelineGraphNode.node_type` (API Literal) and to the
  compile path in `graph_cache.build_graph_from_json`.
- `make_router_node_fn` (in `node_runner.py`) evaluates ordered rules against
  state via the **shared JMESPath evaluator** `evaluate_jmespath_condition`
  (truthiness = `bool(...)`, preserving existing behaviour). An explicit
  `default` rule maps to its target; LLM classifier mode (`mode:"classifier"`)
  matches the `state["_llm_next_node"]` label to a rule `label`.
- **Router lowers onto the existing conditional-edge compile path**: the same
  JMESPath evaluator powers conditional edges, loop counters, HITL gate
  conditions, and polling triggers — one truthiness rule across the engine.
- **Compile-time default-rule enforcement** (typed `RouterConfigError` in
  `graph_cache`): a *new* Router node without a `default` rule is a validation
  error. Existing conditional-edge graphs (no default) remain valid
  (backward-compat) — only new Router nodes require it.
- **Runtime no-match** (no rule matches, no default): raises
  `RouterNoMatchError`; the executor catches it and terminalizes the run with
  the new `router_no_match` terminal status.

### HITL node (F2-D)

- `hitl` added to `node_type` and compiled in `graph_cache`: the node produces
  output like a normal (agent/connector/manual) node, and its `hitl_config` is
  injected onto each outgoing edge, flowing through the **identical** legacy
  synthetic-gate path (`edge.hitl_gate_config` → `make_hitl_gate_fn`).
- A **compile-equivalence** test asserts a `hitl` node produces the same
  compiled graph as the legacy edge-gate. HITL fields map 1:1
  (`required_team_id`, `overdue_threshold_minutes`, `eval_condition`,
  `condition`, `human_only`, `gate_id`).
- `manual` node retained as the non-gating human-output step.

### Taxonomy (F2)

- `loop` edge type added to the API regex (`pipelines.py`), `VALID_EDGE_TYPES`
  (`workflow_import_export`), and already handled by `graph_validator`
  (`_SKIPPED_EDGE_TYPES` / `_check_loop_edges`). `connector` is NOT added to
  the composite sub-node allow-list (would wrongly permit it as a composite
  sub-node → runtime crash); the API Literal correctly omits it.

### `router_no_match` terminal status (§10, 12 sites)

Added across all 12 status-definition sites: migration `ck_runs_status`
(expected-def + create, 0110) and a new migration `0150_add_router_no_match_status`;
ORM `CheckConstraint`; `RUN_STATUS_WHITELIST`; `TERMINAL_STATUSES`;
`_FAILURE_BUCKET_STATUSES` / `_FAILURE_REASON_STATUSES`; `classify.py`
(explicit excluded/notify bucket); inline SQL `completed_at` setters; lifecycle
advancing sets; frontend `runStatuses.ts` / `filters.ts` / `runUtils.ts` /
`analytics.ts` / `schema.ts` (`AnalyticsStatus`); `PROBE_TERMINAL_STATUSES`;
executor catch. Frontend uses a `?? "unknown"`-style fallback so an
unrecognized status never crashes the UI. The single-registry refactor (§10
recommendation) is deferred — the 12 sites were updated directly.

## Consequences

- Router and HITL are now authorable as visual nodes reusing existing engine
  machinery — no new executor code paths.
- `router_no_match` is a terminal, non-failure status (classified `excluded` /
  notify), so a Router with no match ends the run cleanly rather than as an
  unclassified `failed`.
- Edge-gate HITL graphs remain valid; auto-conversion of edge-gate HITL to
  `hitl` nodes during backfill is **out of scope** for this change (follow-up).
- Deploy sequencing (design doc Appendix B O1/O2): the constraint change is
  additive and shipped via migration; frontend `?? fallback` defends against a
  transient backend/frontend status desync.
