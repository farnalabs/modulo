# RFC — Node Metacategories for the Visual Pipeline Builder

**Date**: 2026-06-27
**Status**: Draft — seeking review
**Author**: Duncan / AI agent
**PRD ref**: §8.2 Pipeline Editor — Vue Flow canvas, node/edge data model

---

## 1. Problem

The current pipeline node type system is a flat `Literal["agent", "manual"]`. This conflates two concepts:

- **What the step does** (an AI agent, a human approval, a code execution, etc.)
- **Where the execution boundary lies** (Modulo-owned vs external system / human)

A PR review is currently an `"agent"` node if we drive it with AI, or a `"manual"` node if a human does it. But the same *human activity* could happen inside Modulo (via HITL gate) or outside Modulo (in GitHub, detected via label change). The current type system doesn't express this distinction, which means:

1. The visual canvas doesn't communicate execution boundaries — you can't tell at a glance which steps Modulo owns vs merely observes.
2. New step types (temporal waits, sub-pipeline references, data transforms) don't have a natural home in the type system.
3. The builder UX has no visual vocabulary for signalling what kind of thing a node is — every node currently looks the same apart from colour.

---

## 2. Proposal: Node Metacategories

Introduce a `category` field on pipeline graph nodes, orthogonal to `node_type` (which becomes the *behavioural* axis within a category).

The category is **inferred from the node type**, not chosen directly. When a user drags an "Agent" onto the canvas, its category is `executable`. When they drag a "Wait 30m", its category is `temporal`. The category drives visual styling and validation rules; the user never picks it from a dropdown.

```
node_type  →  determines  →  category  →  determines  →  visual style + validation rules
```

Each category defines:
- What execution boundary it represents
- What `node_type` values are valid within it
- What visual treatment it gets
- What fields on `PipelineGraphNode` are required/optional/forbidden

---

### 2.1 Executable

**Boundary**: Modulo-owned. The runtime orchestrates and executes the work. The human may interact (HITL), but Modulo controls the interaction flow.

| `node_type` | Meaning | In alpha? |
|---|---|---|
| `agent` | LLM-based agent (prompt + schema + model) | Yes — current `"agent"` |
| `human_input` | Human provides output via the HITL review UI | Yes — current `"manual"` node |
| `sandbox` | Code/shell execution in a sandbox (E2B, Docker) | No — ADR 001 phase-7a |
| `hitl_gate` | Human-in-the-loop approval inside Modulo UI | No — currently injected at compile time |
| `transform` | Pure data transformation (map/filter/merge schemas) | No |

**Why `human_input` lives here**: The run pauses inside Modulo's runtime, the human returns to Modulo's UI with their output, and the run continues. The work is human, but the execution boundary is Modulo — we own the pause, the interaction flow, and the continuation.

**Visual**: Solid border. Primary brand colour (sky blue).
**Icon**: Play / robot for `agent`; edit / pencil for `human_input`.

---

### 2.2 Observed

**Boundary**: External system-owned, automated. Some non-Modulo system executes work; Modulo observes the outcome via webhook, polling, or connector state query.

| `node_type` | Meaning | In alpha? |
|---|---|---|
| `ci_run` | External CI pipeline (GitHub Actions, GitLab CI, Jenkins) | No |
| `deploy` | External deployment system | No |
| `external_automation` | Generic — any external automated process Modulo can observe | No |

Observed nodes require a `connector_binding` to the system being observed, plus a `completion_condition` — a JMESPath expression against the observed state that signals completion.

**Visual**: Solid border, distinct colour (teal/green — the "watching" colour).
**Icon**: Eye / satellite dish.

---

### 2.3 Human

**Boundary**: External human activity. A person performs a step in an external tool (GitHub, Jira, Linear, Notion); Modulo detects a state change to know it's done.

| `node_type` | Meaning | In alpha? |
|---|---|---|
| `external_review` | PR review, approval, sign-off in an external tool | No |
| `external_step` | Generic human activity with an expected external state change | No |
| `external_approval` | Explicit approval gate in an external system | No |

Like Observed nodes, Human nodes require a `connector_binding` and a `completion_condition` (e.g., `pr.labels contains 'approved'`, `ticket.status == 'done'`).

**Visual**: **Dashed border** (your instinct — correct). Softer colour than executable. The dash says "this happens outside Modulo."
**Icon**: Person / user.

**Key rule**: A Human node must always have a detectable state change. A pure "human reads something" with no expected external outcome should be a doc annotation, not a pipeline node.

---

### 2.4 Temporal

**Boundary**: Platform-owned. The node does no work — it controls the flow of time.

| `node_type` | Meaning | In alpha? |
|---|---|---|
| `wait_duration` | Pause for N seconds/minutes/hours | No |
| `wait_until` | Pause until a specific datetime | No |
| `rate_limit` | Throttle — ensure minimum gap between runs | No |

Temporal nodes have a `duration_config` field instead of `connector_binding` or `agent_id`:

```python
class DurationConfig(BaseModel):
    type: Literal["duration", "datetime", "cron"]
    value: str  # "30m", "2026-07-01T09:00:00Z", "0 9 * * 1-5"
```

**Restriction**: Temporal nodes must not be graph entry points. A `wait_until` at the start of a pipeline means the run blocks immediately with no computation — confusing UX with no benefit over a cron trigger.

**Visual**: Dotted border (distinct from dashed Human nodes). Circular or pill-shaped node.
**Icon**: Clock / hourglass.

---

### 2.5 Composite

**Boundary**: Modulo-owned, nested. The node represents an entire sub-pipeline, allowing hierarchical composition and reuse.

| `node_type` | Meaning | In alpha? |
|---|---|---|
| `sub_pipeline` | References another pipeline by ID, pinned to a snapshot | No |
| `library_workflow` | References a library workflow primitive (read-only) | No |

Composite nodes require a `pipeline_ref` (UUID of the referenced pipeline) and a snapshot pin — required, not optional — for reproducibility (per ADR 001).

Entry-point resolution is recursive: if pipeline A's first node is a `composite.sub_pipeline` referencing pipeline B, the runtime entry point is pipeline B's first node. The graph validator must handle this chain.

**Visual**: Double border or thicker border. Expand/collapse affordance — clicking drills into the sub-pipeline (preserving viewport state via Vue Router, per ADR 001).
**Icon**: Stack / folder.

---

## 3. Visual Treatment Summary

| Category | Border | Fill | Colour | Icon |
|---|---|---|---|---|
| Executable | Solid | Filled | Sky blue | Play / robot / pencil |
| Observed | Solid | Tinted | Teal / green | Eye |
| Human | **Dashed** | Light | Amber | Person |
| Temporal | **Dotted** | Minimal | Purple | Clock |
| Composite | Double / thick | Filled | Indigo | Stack |

Your dashed-border instinct for Human nodes is confirmed and adopted. Temporal uses dotted to keep it visually distinct.

---

## 4. Data Model

### 4.1 `PipelineGraphNode` (Pydantic — API layer)

```python
class DurationConfig(BaseModel):
    type: Literal["duration", "datetime", "cron"]
    value: str

class PipelineGraphNode(BaseModel):
    id: uuid.UUID
    node_type: str  # category determines which node_types are valid
    position: GraphPosition
    label: str | None = None

    # Shared
    role: str | None = None             # "context_setter" — orthogonal to category
    autonomy_recommendation: str | None = None
    output_schema_id: uuid.UUID | None = None

    # Executable fields
    agent_id: uuid.UUID | None = None   # executable.agent only

    # Observed / Human fields
    connector_binding: ConnectorBinding | None = None
    completion_condition: str | None = None

    # Temporal fields
    duration_config: DurationConfig | None = None

    # Composite fields
    pipeline_ref: uuid.UUID | None = None

    @property
    def category(self) -> str:
        """Inferred from node_type, not stored."""
        return _CATEGORY_MAP[self.node_type]

_CATEGORY_MAP = {
    # Executable
    "agent": "executable",
    "human_input": "executable",
    "sandbox": "executable",
    "hitl_gate": "executable",
    "transform": "executable",
    # Observed
    "ci_run": "observed",
    "deploy": "observed",
    "external_automation": "observed",
    # Human
    "external_review": "human",
    "external_step": "human",
    "external_approval": "human",
    # Temporal
    "wait_duration": "temporal",
    "wait_until": "temporal",
    "rate_limit": "temporal",
    # Composite
    "sub_pipeline": "composite",
    "library_workflow": "composite",
}
```

**Why `category` is computed, not stored**: It's derived from `node_type` via a deterministic map. Storing it separately would let it drift out of sync and adds no value. This also means plugins/extensions only need to register a node type — the category falls out automatically.

**Validation**: A `@model_validator` enforces per-category field requirements (e.g., `agent` requires `agent_id`, `wait_duration` requires `duration_config`, `external_review` requires `connector_binding` and `completion_condition`).

### 4.2 Category-switching UX

When a user changes a node's type (e.g., `agent` → `external_review`), the category changes implicitly. Field requirements shift:

- **On type change**: The client strips fields that are invalid for the new type *before* sending the update. If the user hasn't configured the required fields yet, the node enters a warning state (yellow border, tooltip: "Needs connector binding") but is still saved — validation runs at pipeline-save time, not per-edit.
- **Blocked transitions**: Some type changes don't make sense (e.g., `agent` → `wait_duration` loses all agent config). These are allowed but show a confirmation: "Changing to a wait step will clear the agent binding. Continue?"
- **Frontend pattern**: Selecting a node type auto-populates the category. The user never sees "category" as a form field.

### 4.3 `role` interaction with category

The `role` field (`"context_setter"`) is only meaningful for Executable nodes that produce output. For Observed, Human, Temporal, and Composite nodes, `role` is silently ignored (no-op). The graph validator warns if `role` is set on a non-Executable node.

---

## 5. Runtime Implications

The `build_graph_from_json` function routes on `node_type` via the category:

```python
match _CATEGORY_MAP[node_def["node_type"]]:
    case "executable":
        match node_def["node_type"]:
            case "agent":       make_agent_fn(...)
            case "human_input": make_human_input_fn(...)
            case "sandbox":     make_sandbox_fn(...)
            case "hitl_gate":   make_hitl_gate_fn(...)
            case "transform":   make_transform_fn(...)
    case "observed" | "human":
        # Same runtime: suspend until completion_condition is met
        make_external_wait_fn(node_def, timeout=...)
    case "temporal":
        make_temporal_fn(node_def)
    case "composite":
        make_composite_fn(node_def)
```

Observed and Human share a runtime implementation: suspend the run until an expected state change is detected (webhook, polling, or connector query). The visual distinction matters for communication, not execution.

---

## 6. Plugin / Extension System

Plugins register new `node_type` values by adding to `_CATEGORY_MAP` at registration time:

```python
# In a plugin's startup hook
register_node_type("my_plugin.custom_action", category="executable")
```

The plugin also provides:
- A Vue Flow component template (for rendering on the canvas)
- A form component (for the properties sidebar)
- A runtime function factory (for `build_graph_from_json`)

This maps to the existing plugin system pattern (ConnectorType registration, ModelBackend registration) and keeps the node type system extensible without Modulo core changes.

---

## 7. Snapshot Compatibility

Since there are no existing Modulo deployments, no migration is needed. All new pipelines use `node_type`-based categories from day one.

Snapshots store `graph_json` as a dict — they naturally capture whatever `node_type` was set. If a snapshot is restored, the category is derived from the node type at read time. No backward-compat shim needed.

---

## 8. Copy-to-Adapt Wizard Interaction

The `CopyToAdaptWizard` remaps connector bindings on import. It must handle:
- **Observed / Human nodes**: Their `connector_binding` may need remapping — same as current behaviour.
- **Composite nodes**: Their `pipeline_ref` may reference a pipeline that doesn't exist in the target org. The wizard should flag these as "needs manual reassignment" rather than silently dropping them.

This is an extension of existing wizard logic, not a new component.

---

## 9. Open Questions

1. **Should `node_type` remain a free string or become a per-category enum?** Free string allows plugin extensibility without schema changes. Per-category `Literal` gives validation and IDE support. Proposal: per-category `Literal` at the API layer, with a registration-based escape hatch for plugins.

2. **Where does `output_schema_id` fit?** Currently on `PipelineGraphNode` for manual nodes. Under this proposal, `human_input` (Executable) should use it to validate the human's input. `external_review` (Human) produces the observed state as output — does that need schema validation? Proposal: `output_schema_id` remains on the node and applies to any node type that produces a schema-validated artifact. Observed and Human nodes can optionally validate the observed state against it.

3. **Should category be exposed in the UI at all?** Currently it's computed from node_type. But the colour-coding relies on it — the canvas renders differently based on category. If a plugin registers a new node type, how does the frontend know what colour and border style to apply? It reads the category (via the API) and applies the corresponding style. The user never picks a category, but the frontend uses it extensively.

4. **How does the frontend present type selection?** Proposal: a single "Add Node" menu organised by category. Sections like:

   ```
   ── Executable ──
   Agent       AI agent with prompt + model
   Human Input Step where human provides output
   Sandbox     Code/shell execution
   Transform   Data transform (map/filter/merge)

   ── Human ──
   External Review   PR review, approval in external tool
   External Step      Human activity with detectable state change

   ── Observed ──
   CI Run      External CI pipeline
   Deploy      External deployment

   ── Temporal ──
   Wait         Pause for a duration
   Schedule     Wait until a specific time

   ── Composite ──
   Sub-pipeline   Reference another pipeline
   ```

   The visual style hint is already present (the section header colour/border matches the category).
