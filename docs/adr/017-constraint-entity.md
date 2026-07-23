# ADR 017 — Constraint Entity (First-Class Constraint System)

**Date**: 2026-07-22
**Status**: Draft — deferred to post-MVP

---

## Context

Modulo currently has several ad-hoc constraint-like mechanisms scattered across the codebase:

| Mechanism | Location | Form |
|---|---|---|
| autonomy_level | Pipeline, Snapshot, GraphNode | String enum on the model |
| hitl_gate_config | PipelineEdge | Inline JSON on the edge |
| rate_limit_config | Pipeline | Inline JSON on the pipeline |
| retry_policy | Agent | Inline JSON on the agent |
| evals | Agent, run | Separate EvalDefinition entity |
| condition_expression | PipelineEdge | String on the edge |
| allowed_operations | ConnectorInstance | String list on the connector |
| required_environment_capabilities | Agent | String list on the agent |
| run_context_defaults | Pipeline, Snapshot | Inline JSON |
| token_budget, max_input_length | Agent, GraphNode | Separate integer fields |
| node_timeout_seconds | Pipeline | Integer field |

Each is implemented as a one-off — different storage patterns, different enforcement mechanisms, different UI treatment. None is reusable across contexts. None can be composed. None can be shared between pipelines or agents.

This creates three problems:

1. **Inconsistency**: adding a new constraint type requires bespoke model fields, CRUD endpoints, UI components, and enforcement logic. No pattern to follow.

2. **No reusability**: a no-new-dependencies rule on one agent cannot be applied to another without duplicating it.

3. **No composition**: autonomy + resource + scope constraints are independent. The relationship must be hardcoded.

David Epstein's *Inside the Box: How Constraints Make Us Better* (2026) argues that well-defined constraints improve outcomes by blocking easy answers and forcing novel solutions. Modulo's existing ad-hoc constraints already demonstrate this (worktree isolation, AGENTS.md, schema assignment), but the pattern is implicit. A first-class Constraint entity would make the pattern explicit, intentional, and composable.

**This is explicitly deferred to post-MVP.** The MVP should ship with the existing ad-hoc mechanisms. A unified constraint abstraction adds no new capability — it formalises what already exists. It should be designed when the MVP is validated and the pattern library of actual constraint types is mature enough to generalise from.

---

## Decision

### What a Constraint IS (technical definition)

A **Constraint** is a first-class entity — its own ORM model, DB table, and CRUD API — that represents a typed, evaluable, reusable rule restricting the behaviour space of an agent or pipeline node.

It follows the same architectural pattern as EvalDefinition and SchemaVersion:

- Independent, versioned entity with full CRUD
- Binds to agents or pipeline nodes via polymorphic join
- Snapshotted into PipelineSnapshot at run start
- Enforced at two layers: **prompt injection** (soft) and **execution engine guard** (hard)

### ConstraintType enum

`
SCOPE     — "may only touch files matching *.py"
RESOURCE  — "token_budget <= 4000"
DEPENDENCY — "no new npm packages"
SCHEMA    — "output must conform to schema X"
BEHAVIOUR — "must not call external APIs"
QUALITY   — "ruff score must be 0"
AUTONOMY  — "requires human approval before deploy"
`

### ConstraintSeverity enum

`
HARD  — Blocks execution; engine refuses to run
SOFT  — Injected into prompt as guidance only
EVAL  — Checked post-run via existing eval engine
`

### ORM Model

`python
class Constraint(OrgScoped):
    __tablename__ = "constraints"
    name: str
    description: str
    constraint_type: ConstraintType
    definition_json: dict       # Type-specific payload
    severity: ConstraintSeverity
    failure_message: str | None
    version: int
    active: bool
    source: str | None
    account_id: UUID
`

### Attachment to the graph

Two strategies, complementary:

**Persistent (on Agent model):**
Agent gets constraint_ids: list[UUID] | None referencing Constraint rows. Inherited by every node using that agent.

**Per-instance (on PipelineGraphNode):**
PipelineGraphNode gets constraint_bindings: list[ConstraintBinding] with constraint_id + parameter_overrides + enabled. Frozen into PipelineSnapshot at run start alongside connector_bindings_json and schema_pins_json.

### Enforcement: two-layer design

**Layer 1 — Prompt Injection (soft, always on):**
At snapshot build time, constraints are rendered into the agent's prompt as a structured ## Constraints section. Works immediately without engine changes.

**Layer 2 — Engine Guards (hard, severity-dependent):**
A new @constrained decorator (or extension to @cancellable_node) checks resolved constraints at runtime:
- HARD: raises ConstraintViolationError on violation
- SOFT: prompt-only (already injected)
- EVAL: produces an EvalResult that can fail the run

### Migration path (post-MVP)

| Existing | Replaced by |
|---|---|
| default_autonomy_level | Constraint type autonomy |
| hitl_gate_config | Constraint type autonomy with required_approval |
| retry_policy | Constraint type behaviour or resource |
| rate_limit_config | Constraint type resource |
| allowed_operations | Constraint type scope |
| required_environment_capabilities | Constraint type resource |
| max_input_length / token_budget | Parameter overrides on a resource Constraint |

Existing fields become convenience aliases or are deprecated with a backward-compat shim.

---

## Consequences

### Positive

- **Uniform pattern**: new constraint type = one enum value + one schema + one eval function. No new model fields or CRUD.
- **Reusability**: one constraint applies to any agent, pipeline, or node. Teams build a library over time.
- **Composability**: multiple constraints stack on a single node. HARD + SOFT + EVAL coexist.
- **Auditability**: violations are a first-class event with traceability.
- **Prompt injection always-on**: even without engine enforcement, the soft layer works.

### Negative

- **MVP deferral adds migration cost**: retrofitting 20+ bespoke fields later requires shims and data migration.
- **Abstraction overhead**: for simple cases, full entity is more ceremony than a single field.
- **Prompt injection unverifiable**: soft layer relies on agent compliance.

### Deferral rationale

| Priority | Why |
|---|---|
| Ship working pipelines with ad-hoc constraints | Users get value sooner; patterns emerge from usage |
| Validate PRD feature set | Know which types matter before abstracting |
| Avoid premature generalisation | A unified design now would be wrong |
| Migration is tractable | Each ad-hoc field maps cleanly to a Constraint type |

---

## Related ADRs

- **ADR 003 — Agent Dispatch Model**: established dispatch-only role. Constraints extend the dispatch envelope.
- **ADR 010 — Integration Tier Classification**: tier enum pattern reused here.
- **ADR 015 — Bundle Format V2**: snapshot binding pattern extended with constraint_bindings_json.
