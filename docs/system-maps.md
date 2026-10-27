# Delivery System Maps Specification

**Status:** Draft
**Target Milestone:** Beta
**Category:** Product & Technical Architecture Specification
**Related:** `docs/architecture.md`, `docs/core-principles.md`

---

## 1. Product Thesis & Positioning

### 1.1 What Delivery System Maps Is
Delivery System Maps provides the operational model for an organization's governed agentic delivery system. In external product communication, the feature is named Delivery System Maps. In the product user interface and navigation, it is named System Maps.

System Maps represents the end-to-end software delivery topography: executable Modulo pipelines, external CI and verification capabilities, manual human engineering gates, intermediate queues, governance controls, and autonomous improvement loops. It displays flow rates, queue latencies, capacity constraints, bottlenecks, failure concentrations, and telemetry coverage across the whole delivery architecture.

### 1.2 Category Positioning
Modulo is an Agentic Delivery Governance platform. For developer experience and platform engineering teams, Modulo functions as a Graph Engineering Platform.

Modulo governs the execution of autonomous pipelines, enforces quality and safety gates, records immutable audit evidence, and coordinates human-in-the-loop decisions. System Maps provides the system-level visibility required to inspect, operate, and tune that governed delivery network.

### 1.3 Core Value Proposition
The core value proposition of System Maps is: operate and improve your governed delivery system.

Engineering organizations adopting autonomous agents do not suffer from a shortage of code generation. They suffer from systemic instability: unmonitored queues, silent handoff failures, uneven agent capacity, runaway error loops, and blind spots where human review bottlenecks throughput. System Maps makes the delivery system observable as an engineered graph.

Modulo cultivates the forest rather than growing the individual tree. Traditional software engineering tools focus on tracking single units of work through linear column states. Modulo focuses on the health, capacity, safety, and continuous improvement of the delivery machinery itself.

### 1.4 Explicit Rejection of Issue and Project Management
System Maps is not an issue tracker, a ticketing system, or an agile project management board. Modulo explicitly rejects replicating Jira, Linear, or GitHub Project boards.

Issue trackers model the state of human and task assignments. They organize work around tickets, assignees, sprints, story points, and estimates. Modeling delivery as ticket transitions creates three failure modes for agentic SDLC platforms:
1. It encourages building complex project management features (epics, backlogs, sprint planning) that distract from core execution governance.
2. It collapses the distinction between system machinery and work items, treating a pipeline like a status column rather than an executable distributed workflow.
3. It creates data sync friction with external systems of record where human teams already track commitments.

In System Maps, work items (such as pull requests, issues, and ticket keys) are optional drill-down evidence. They never serve as the primary organizing entity. The primary organizing entities are capabilities, queues, controls, and flows.

---

## 2. Conceptual Boundary & System Hierarchy

### 2.1 The Four-Layer System Hierarchy
Modulo separates delivery architecture into four distinct layers:

```
+-----------------------------------------------------------------------+
| 1. System Map                                                         |
| Operational architecture: capabilities, handoffs, queues, controls,   |
| bottlenecks, capacity, and autonomous improvement loops.              |
+-----------------------------------------------------------------------+
                                   |
                                   v
+-----------------------------------------------------------------------+
| 2. Pipeline                                                           |
| Executable machinery: directed acyclic graph of agent nodes, schemas, |
| connector bindings, triggers, and deterministic gates.               |
+-----------------------------------------------------------------------+
                                   |
                                   v
+-----------------------------------------------------------------------+
| 3. Run                                                                |
| Immutable execution evidence: frozen snapshot, per-node outputs,      |
| audit trail, wall-clock duration, token cost, and exit states.        |
+-----------------------------------------------------------------------+
                                   |
                                   v
+-----------------------------------------------------------------------+
| 4. Work Item (Optional Evidence)                                      |
| Traceability reference: external pull request, issue key, or commit   |
| attached to a run as non-authoritative contextual metadata.           |
+-----------------------------------------------------------------------+
```

### 2.2 Boundary Comparison Matrix

| Dimension | System Map | Pipeline | Run | Analytics (`run_daily_facts`) | Issue Tracker (Linear / Jira) |
|---|---|---|---|---|---|
| **Primary Unit** | Delivery topology and capabilities | Executable workflow graph | Immutable execution instance | Daily fact aggregates by dimension | Task, ticket, or story |
| **Lifecycle** | Draft, published, archived revisions | Created, committed, updated | Pending, running, terminalized | Appended upon run terminalization | Backlog, todo, in-progress, done |
| **Execution State** | Non-executable topology | Executable specification | Executing or completed container | Historical data warehouse record | Workflow state label |
| **Attribution** | Top-level system container | System node reference | Pinned snapshot node reference | Frozen snapshot attribution | External reference key |
| **Audit Role** | Structural architecture record | Machinery definition | Execution and gate evidence | Aggregated trend evidence | Customer operational record |
| **Failure Mode** | Topological bottleneck or blind spot | Node crash or eval gate rejection | Process timeout or API error | Missing fact or data loss | Unassigned or blocked task |

---

## 3. Domain Model & Relational Schema

### 3.1 Persistence Rules
System Maps adheres to four strict database design rules:
1. **No mutable JSON counters:** Live counters, throughput sums, and work-in-progress gauges are never written into JSON columns on the map row. Aggregates are derived dynamically from indexed terminal facts and active execution states.
2. **Immutable published revisions:** When a map is published, its content is saved to an immutable revision row. In-flight runs reference an exact revision id, ensuring historical system topology is never mutated by subsequent map edits.
3. **Draft isolation:** A draft revision can be edited, validated, and previewed without affecting active system overview dashboards or live pipeline run attributions.
4. **Relational graph projections:** Map nodes and relationships are projected into queryable relational rows during publish. This enables SQL joins, foreign key integrity checks, and fast attribution lookups without parsing JSON blobs on every read.

**Tenant integrity has two explicit enforcement boundaries.** Every System Map row carries `organisation_id`. Foreign keys between the new System Map tables use composite keys where both tables own the organisation column, so a revision can reference only its map and a relationship can reference only nodes in its revision. References to existing Modulo resources use their existing primary-key foreign keys, then the service transaction validates that the referenced resource belongs to the current organisation and, where applicable, the permitted team scope under PostgreSQL row-level security (RLS). The service performs those checks while holding the relevant map or revision lock, before inserting or publishing the graph.

`accounts` is a global table and does not gain an `organisation_id` in this design. Account ownership and audit columns therefore use the existing `account_id` foreign key only. At write time, the service checks that the account is an active member of the current organisation before accepting it as `account_id` or `updated_by`; the transaction rejects an account that lacks membership. This is an application-level membership rule, not an invented account composite key.

Node keys remain stable within a revision for import/export and human-readable diffs. The relationship and loop-config tables enforce those key references in SQL using composite foreign keys because those new tables own both `revision_id` and `organisation_id`. `external_ref` and values inside `config_json` are application-level data: they do not identify Modulo rows and cannot provide a relational tenant boundary.

### 3.2 Relational Entities

```
+---------------------+         1:N         +-----------------------------+
|    system_maps      |-------------------->|    system_map_revisions     |
+---------------------+                     +-----------------------------+
| id (UUID, PK)       |                     | id (UUID, PK)               |
| organisation_id     |                     | organisation_id             |
| name (VARCHAR)      |                     | map_id (UUID, FK)           |
|                     |                     | revision_number (INT)       |
| published_rev_id    |--+                  | status (draft/published)    |
| owner_team_id (FK)  |  |                  | nodes_json (JSONB)          |
| visibility (ENUM)   |  |                  | edges_json (JSONB)          |
| deleted_at          |  |                  | layout_json (JSONB)         |
+---------------------+  |                  +-----------------------------+
                         |                                 |
                         +---------------------------------+
                                         |
                       +-----------------+-----------------+
                       | 1:N                               | 1:N
                       v                                   v
        +-----------------------------+     +-----------------------------+
        |      system_map_nodes       |     |  system_map_relationships   |
        +-----------------------------+     +-----------------------------+
        | id (UUID, PK)               |     | id (UUID, PK)               |
        | revision_id (UUID, FK)      |     | revision_id (UUID, FK)      |
        | node_key (VARCHAR)          |     | relationship_key (VARCHAR)  |
        | node_type (ENUM)            |     | source_node_key (VARCHAR)   |
        | name (VARCHAR)              |     | target_node_key (VARCHAR)   |
        | pipeline_id (UUID, FK, opt) |     | relationship_type (ENUM)    |
        | connector_instance_id (UUID, FK, opt) | | config_json (JSONB)      |
        | owner_team_id (UUID, FK, opt) |      |                              |
        | eval_id (UUID, FK, opt)     |      |                              |
        | external_ref (VARCHAR, opt) |     |                              |
        | capacity_limit (INT, opt)   |     +-----------------------------+
        | config_json (JSONB)         |
        +-----------------------------+
                       |
                       | 1:1 (if loop)
                       v
        +-----------------------------+
        |   system_map_loop_configs   |
        +-----------------------------+
        | id (UUID, PK)               |
        | revision_id (UUID, FK)      |
        | node_key (VARCHAR, FK)      |
        | target_signal (VARCHAR)     |
        | observation_window_seconds  |
        | action_pipeline_id (FK)     |
        | budget_limit_usd (NUMERIC)  |
        | cooldown_seconds (INT)      |
        | outcome_metric (VARCHAR)    |
        +-----------------------------+
```

### 3.3 Database Table Definitions

```sql
-- Existing tables are not changed to add composite keys as a prerequisite.
-- New System Map tables use composite keys only between tables defined here.
-- Foreign keys to existing resources use their current primary keys; the service
-- transaction validates organisation and team scope before each binding write.
-- `accounts` remains global and is checked through organisation membership.

-- Core System Map table
CREATE TABLE system_maps (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organisation_id UUID NOT NULL REFERENCES organisations(id) ON DELETE RESTRICT,
    name VARCHAR(255) NOT NULL,
    description TEXT,
    visibility VARCHAR(10) NOT NULL DEFAULT 'org',
    owner_team_id UUID,
    published_revision_id UUID,
    is_default BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    deleted_at TIMESTAMPTZ,
    account_id UUID NOT NULL REFERENCES accounts(id) ON DELETE RESTRICT,
    updated_by UUID REFERENCES accounts(id) ON DELETE RESTRICT,
    CONSTRAINT uq_system_maps_id_org UNIQUE (id, organisation_id),
    CONSTRAINT fk_system_maps_owner_team FOREIGN KEY (owner_team_id)
        REFERENCES teams(id) ON DELETE RESTRICT,
    CONSTRAINT ck_system_maps_visibility CHECK (visibility IN ('org', 'team')),
    CONSTRAINT ck_system_maps_team_owner CHECK (visibility = 'org' OR owner_team_id IS NOT NULL)
);

CREATE UNIQUE INDEX uq_system_maps_org_name ON system_maps(organisation_id, name)
WHERE deleted_at IS NULL;

CREATE UNIQUE INDEX uq_system_maps_org_default ON system_maps(organisation_id)
WHERE is_default = TRUE AND deleted_at IS NULL;

-- Immutable Revision table
CREATE TABLE system_map_revisions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organisation_id UUID NOT NULL REFERENCES organisations(id) ON DELETE RESTRICT,
    map_id UUID NOT NULL,
    revision_number INT NOT NULL,
    status VARCHAR(20) NOT NULL DEFAULT 'draft',
    nodes_json JSONB NOT NULL DEFAULT '[]'::jsonb,
    edges_json JSONB NOT NULL DEFAULT '[]'::jsonb,
    layout_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    change_summary TEXT,
    published_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    account_id UUID NOT NULL REFERENCES accounts(id) ON DELETE RESTRICT,
    CONSTRAINT uq_system_map_revisions_id_org UNIQUE (id, organisation_id),
    CONSTRAINT fk_system_map_revisions_map_org FOREIGN KEY (map_id, organisation_id)
        REFERENCES system_maps(id, organisation_id) ON DELETE CASCADE,
    CONSTRAINT ck_system_map_revisions_status CHECK (status IN ('draft', 'published', 'archived')),
    CONSTRAINT ck_system_map_revisions_number CHECK (revision_number > 0),
    CONSTRAINT uq_system_map_revisions_map_num UNIQUE (map_id, revision_number)
);

ALTER TABLE system_maps ADD CONSTRAINT fk_system_maps_published_rev_org
FOREIGN KEY (published_revision_id, organisation_id)
REFERENCES system_map_revisions(id, organisation_id) ON DELETE RESTRICT;

-- Published revisions are immutable and cannot be deleted while selected by a
-- map. Clear the pointer explicitly by publishing another revision or by an
-- administrative map-retirement operation before deleting the map itself. The
-- RESTRICT action is intentional: it never tries to NULL organisation_id.

-- Relational projection of nodes for fast joins and indexing
CREATE TABLE system_map_nodes (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organisation_id UUID NOT NULL REFERENCES organisations(id) ON DELETE RESTRICT,
    revision_id UUID NOT NULL,
    node_key VARCHAR(128) NOT NULL,
    node_type VARCHAR(32) NOT NULL,
    name VARCHAR(255) NOT NULL,
    description TEXT,
    pipeline_id UUID REFERENCES pipelines(id) ON DELETE RESTRICT,
    connector_instance_id UUID REFERENCES connector_instances(id) ON DELETE RESTRICT,
    owner_team_id UUID REFERENCES teams(id) ON DELETE RESTRICT,
    eval_id UUID REFERENCES evals(id) ON DELETE RESTRICT,
    external_ref VARCHAR(255),
    capacity_limit INT,
    config_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_system_map_nodes_id_org UNIQUE (id, organisation_id),
    CONSTRAINT uq_system_map_nodes_rev_key_org UNIQUE (revision_id, node_key, organisation_id),
    CONSTRAINT fk_system_map_nodes_revision_org FOREIGN KEY (revision_id, organisation_id)
        REFERENCES system_map_revisions(id, organisation_id) ON DELETE CASCADE,
    CONSTRAINT ck_system_map_nodes_type CHECK (
        node_type IN (
            'pipeline',
            'external_capability',
            'manual_capability',
            'queue',
            'control',
            'improvement_loop',
            'system_boundary'
        )
    )
);

CREATE INDEX ix_system_map_nodes_pipeline ON system_map_nodes(organisation_id, pipeline_id)
WHERE pipeline_id IS NOT NULL;

-- Relational projection of edges
CREATE TABLE system_map_relationships (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organisation_id UUID NOT NULL REFERENCES organisations(id) ON DELETE RESTRICT,
    revision_id UUID NOT NULL,
    relationship_key VARCHAR(128) NOT NULL,
    source_node_key VARCHAR(128) NOT NULL,
    target_node_key VARCHAR(128) NOT NULL,
    relationship_type VARCHAR(32) NOT NULL,
    config_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_system_map_relationships_id_org UNIQUE (id, organisation_id),
    CONSTRAINT uq_system_map_rel_rev_key_org UNIQUE (revision_id, relationship_key, organisation_id),
    CONSTRAINT fk_system_map_rel_revision_org FOREIGN KEY (revision_id, organisation_id)
        REFERENCES system_map_revisions(id, organisation_id) ON DELETE CASCADE,
    CONSTRAINT fk_system_map_rel_source_org FOREIGN KEY (revision_id, source_node_key, organisation_id)
        REFERENCES system_map_nodes(revision_id, node_key, organisation_id) ON DELETE CASCADE,
    CONSTRAINT fk_system_map_rel_target_org FOREIGN KEY (revision_id, target_node_key, organisation_id)
        REFERENCES system_map_nodes(revision_id, node_key, organisation_id) ON DELETE CASCADE,
    CONSTRAINT ck_system_map_rel_type CHECK (
        relationship_type IN (
            'flow',
            'depends_on',
            'controls',
            'feeds_back_to',
            'contains'
        )
    )
);

CREATE INDEX ix_system_map_rel_source ON system_map_relationships(organisation_id, revision_id, source_node_key);
CREATE INDEX ix_system_map_rel_target ON system_map_relationships(organisation_id, revision_id, target_node_key);

-- First-class configuration table for improvement loops
CREATE TABLE system_map_loop_configs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organisation_id UUID NOT NULL REFERENCES organisations(id) ON DELETE RESTRICT,
    revision_id UUID NOT NULL,
    node_key VARCHAR(128) NOT NULL,
    target_signal VARCHAR(255) NOT NULL,
    observation_window_seconds INT NOT NULL,
    action_pipeline_id UUID NOT NULL REFERENCES pipelines(id) ON DELETE RESTRICT,
    budget_limit_usd NUMERIC(10, 4) NOT NULL,
    cooldown_seconds INT NOT NULL,
    outcome_metric VARCHAR(255) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_system_map_loop_configs_id_org UNIQUE (id, organisation_id),
    CONSTRAINT fk_system_map_loop_revision_org FOREIGN KEY (revision_id, organisation_id)
        REFERENCES system_map_revisions(id, organisation_id) ON DELETE CASCADE,
    CONSTRAINT fk_system_map_loop_node_org FOREIGN KEY (revision_id, node_key, organisation_id)
        REFERENCES system_map_nodes(revision_id, node_key, organisation_id) ON DELETE CASCADE,
    CONSTRAINT ck_system_map_loop_obs_window CHECK (observation_window_seconds >= 60),
    CONSTRAINT ck_system_map_loop_budget CHECK (budget_limit_usd > 0),
    CONSTRAINT ck_system_map_loop_cooldown CHECK (cooldown_seconds >= 0),
    CONSTRAINT uq_system_map_loop_rev_node_org UNIQUE (revision_id, node_key, organisation_id)
);
```

### 3.4 Constraint and Binding Contract

The initial node binding contract is deliberately relational and small. A node may bind to at most one `pipeline_id`, `connector_instance_id`, `owner_team_id`, or `eval_id`; each populated binding uses the existing primary-key foreign key for that resource. In declarative authoring, `pipeline_ref` is the node's top-level pipeline binding reference and `owner_team_ref` is the map's top-level owner-team binding reference. These authoring references resolve to the relational `pipeline_id` and `owner_team_id` projections before save or publish. Node-kind-specific properties belong in `config_json`, including the improvement-loop `config_json.action_pipeline_ref` reference that resolves to the loop projection's `action_pipeline_id`. The service transaction must then validate that each resolved resource belongs to the current organisation and, for team-scoped maps, that the team is permitted by the map's visibility and row-level security scope. `external_ref` remains the only provider-owned identifier. Guardrail and human-approval configurations are not relational System Map bindings in this revision: controls describe those concepts through `control_type` and validated `config_json` only.

`account_id` and `updated_by` use the existing account foreign key. Before inserting or publishing a map or revision, the service verifies that each account is an active member of the current organisation. The account table remains global and receives no organisation column or composite foreign key.

The initial relationship vocabulary targets map nodes only. `owned_by`, `measured_by`, and `capacity_limited_by` are therefore removed from the first implementation: team ownership is represented by `owner_team_id`, metrics are selected by the node's metric configuration, and capacity is represented by `capacity_limit` or a queue node. This avoids relationships whose target entity has no typed row or tenant-safe key.

The DDL and service transaction divide tenant enforcement as follows:

- `system_maps`, revisions, nodes, relationships, and loop configurations each store a non-null `organisation_id` and reference `organisations(id)`.
- Composite keys are created only on new tables where both columns are owned by this schema: `(id, organisation_id)` for row identity and `(revision_id, node_key, organisation_id)` for node identity within a revision.
- A revision, published-revision pointer, relationship endpoint, or loop configuration can therefore reference only rows from the same new-schema organisation and revision.
- A pipeline, connector, evaluation, or team binding uses that existing table's primary-key foreign key. The service transaction performs the same-organisation and permitted-team checks under RLS/team scope before writing or publishing. These checks are mandatory validation boundaries; they are not inferred from a composite key that the existing table does not provide.
- Account bindings use the existing `account_id` foreign key. The service transaction checks active membership in the current organisation for `account_id` and `updated_by`.
- The graph validator still enforces semantic rules such as allowed relationship types, required node properties, and flow acyclicity.

The migration is executable in this order: create the new tables and their organisation-owned composite keys; add ordinary foreign keys to existing resource primary keys; add the indexes used by the service's organisation and team validation queries; deploy the service transaction checks; backfill or publish only after those checks are active; then add attribution columns and terminalization writes. The migration does not alter `accounts`, and it does not require new composite unique keys on teams, pipelines, connectors, or evaluations. References stored only inside JSON, such as `external_ref` or a connector-specific configuration value, remain application-level because they do not identify a Modulo row.

---

## 4. Graph Semantics & Node / Relationship Typology

### 4.1 Node Typology

| Node Type | Purpose | Primary Properties | Modulo Binding |
|---|---|---|---|
| `pipeline` | Represents an automated, executable Modulo pipeline. | `pipeline_ref`, `execution.max_concurrent_runs`, `execution.timeout_seconds` | `pipeline_ref` resolves to the relational `pipeline_id` binding. |
| `external_capability` | Represents work executed outside Modulo (e.g. GitHub Actions, ArgoCD, Jenkins, security scanner). | `provider`, `external_ref`, `execution_mode` | Optional `connector_instance_id` binding for status telemetry. |
| `manual_capability` | Represents human engineering work (e.g. architectural spikes, design review, manual security audit). | `owner_role`, `service_level.sla_hours` | `owner_team_ref` resolves to the relational `owner_team_id` binding. |
| `queue` | Represents an intermediate buffer or backlog holding items between capabilities. | `queue_type`, `capacity_limit`, `drain_rate_per_hour` | Capacity is represented directly by `capacity_limit`; no separate slot entity is referenced. |
| `control` | Represents a deterministic evaluation gate, safety guardrail, or human approval barrier. | `control_type` (gate / guardrail / approval), `pass_threshold`, `fail_action` | Optional `eval_id` binding; guardrail and human-in-the-loop (HITL) references are outside the initial relational binding contract. |
| `improvement_loop` | Represents an autonomous sidecar pipeline monitoring system signals and deploying incremental fixes. | Loop configuration (signal, window, action, budget, cooldown, metric) | Bound to `system_map_loop_configs` and an action pipeline. |
| `system_boundary` | Demarcates an organizational boundary, environment trust zone, or compliance perimeter. | `boundary_type` (network / team / environment), `compliance_scope` | Container grouping via `contains` relationships. |

### 4.2 Node Property Contract

`system_map_nodes.config_json` is a bounded authoring document, not an untyped escape hatch. The service validates it against the node-kind schema before save and publish. Unknown top-level properties are rejected, values are type-checked, strings have a maximum length of 255 characters, durations are positive integers in seconds, rates and thresholds are non-negative numbers. Stable identity, display, and resource-binding references explicitly listed in this contract may remain top-level node fields; every node-kind-specific property belongs under `config_json`, including `config_json.action_pipeline_ref` for improvement loops. Authoring references resolve to typed relational binding columns before save or publish. The relational columns are the queryable projection; `config_json` stores authoring metadata and node-kind properties that are needed to render or validate the map, while resolved relational fields provide joins.

The initial schemas are:

| Node kind | Required `config_json` paths | Optional paths and rules | Queryable projection |
|---|---|---|---|
| `pipeline` | `execution.max_concurrent_runs` (integer, 1 or greater), `execution.timeout_seconds` (integer, 1 or greater) | `display.metric` (enum: `throughput`, `latency`, `failure_rate`, `capacity`) | `pipeline_id`, `capacity_limit`, `name`, `node_type` |
| `external_capability` | `provider` (string), `execution_mode` (enum: `webhook`, `poll`, `push`, `manual`) | `telemetry.status_path` (string, max 255), `telemetry.latency_path` (string, max 255) | `connector_instance_id`, `external_ref`, `name`, `node_type` |
| `manual_capability` | `owner_role` (string), `service_level.sla_hours` (number, greater than 0) | `escalation.channel` (enum: `email`, `chat`, `ticket`, `none`) | `owner_team_id`, `name`, `node_type` |
| `queue` | `queue_type` (enum: `work`, `artifact`, `approval`, `signal`) | `capacity_limit` (integer, 1 or greater), `drain_rate_per_hour` (number, 0 or greater) | `capacity_limit`, `name`, `node_type` |
| `control` | `control_type` (enum: `gate`, `guardrail`, `approval`), `fail_action` (enum: `block`, `warn`, `route_to_human`) | `pass_threshold` (number from 0 through 1) is required when `control_type` is `gate` or `guardrail`; absent for `approval` | `eval_id`, `name`, `node_type` |
| `improvement_loop` | `target_signal`, `observation_window_seconds`, `config_json.action_pipeline_ref`, `budget_limit_usd`, `cooldown_seconds`, `outcome_metric` | No additional properties in the initial contract | `system_map_loop_configs.action_pipeline_id`, `name`, `node_type` |
| `system_boundary` | `boundary_type` (enum: `network`, `team`, `environment`) | `compliance_scope` (string, max 255) | `name`, `node_type` |

The loop fields are duplicated into `system_map_loop_configs` at publish time, where the relational checks enforce required numeric bounds. The authoring JSON remains the source for draft editing; the loop table is the published, queryable projection. Other node kinds use the typed columns and validated JSON together. A future property requires a schema version change and an explicit validator update.

### 4.3 Relationship Typology

The first implementation exposes only relationships whose endpoints are rows in `system_map_nodes`. Ownership, measurement, and capacity limits remain node properties or bindings, so `owned_by`, `measured_by`, and `capacity_limited_by` are intentionally outside the initial relationship enum.

| Relationship Type | Direction | Semantic Meaning | Validation Rule |
|---|---|---|---|
| `flow` | Source -> Target | Primary delivery flow of code, artifacts, or deliverables. | Must connect non-loop nodes; must be acyclic. |
| `depends_on` | Dependent -> Prerequisite | Operational or architectural dependency. | Source capability cannot operate without target. |
| `controls` | Controller -> Controlled | Governs execution, gating, or admission into a capability. | Source must be `control` or `improvement_loop`. |
| `feeds_back_to` | Source -> Target | Asynchronous diagnostic signal or metric emission. | Target is an upstream queue, signal buffer, or loop. |
| `contains` | Boundary -> Node | Hierarchical enclosure inside a subsystem or perimeter. | Source must be `system_boundary`; ownership, metrics, and capacity use the node columns and bindings described in §3.4. |

### 4.4 Loop Semantics & Acyclic Delivery Flow
A core failure mode of naive delivery graphs is introducing arbitrary executable cycles into the pipeline flow. When a pipeline failure redirects an artifact back to an earlier pipeline without structural limits, the system risks infinite execution loops, unbounded cloud compute spend, and cascading queue collapse.

System Maps enforces strict loop semantics:
1. **The delivery flow is acyclic:** Every relationship of type `flow` belongs to the primary delivery flow. The directed subgraph formed by all `flow` edges must be a Directed Acyclic Graph (DAG). Any draft containing a cycle among `flow` edges is rejected by the graph validator at save and publish time.
2. **Improvement loops are modeled as control sidecars:** An `improvement_loop` is never placed as an inline node on a `flow` edge. It sits adjacent to the flow as an autonomous observer. It reads signals via `feeds_back_to` or metric streams, and acts upon capabilities or queues through `controls` or asynchronous trigger actions.
3. **The Six-Parameter Improvement Loop Contract:** Every `improvement_loop` node must define exactly six control parameters:
   - **Target Signal (`target_signal`):** The quantifiable operational symptom or defect to monitor (e.g. `failing_test_ratio`, `uncovered_architecture_gaps`, `critical_security_findings`).
   - **Observation Window (`observation_window_seconds`):** The time duration over which signal data is collected before deciding to act (e.g. 3600 seconds, 86400 seconds).
   - **Action Pipeline (`config_json.action_pipeline_ref`):** The authoring reference for the executable Modulo pipeline dispatched to investigate and apply a remediation (e.g. Improve Architecture, Improve Security). It resolves to the relational `action_pipeline_id` binding in the published loop configuration.
   - **Budget Limit (`budget_limit_usd`):** Maximum model and compute spend allocated to this loop per calendar month.
   - **Cooldown Window (`cooldown_seconds`):** Minimum quiet period required after a remediation run before the loop is permitted to fire again, preventing rapid-fire oscillatory commits.
   - **Outcome Metric (`outcome_metric`):** The verifiable metric used to confirm whether the remediation succeeded or failed (e.g. `delta_vulnerabilities`, `delta_test_failures`).

---

## 5. Data Contracts, Attribution & Provenance

### 5.1 Snapshot Attribution Freezing (Beta implementation contract)
A pipeline run must not guess its position in the organizational delivery system at runtime. At run initialization, the trigger engine will evaluate the organization's published System Map and resolve the matching `pipeline` node, if one exists.

This is **Beta implementation work**, not a claim about fields that exist in the current `Run` or `PipelineSnapshot` models. The proposed frozen attribution block is the following object, stored in the immutable `PipelineSnapshot` for the run:

```json
{
  "system_map_id": "UUID or null",
  "system_map_revision_id": "UUID or null",
  "system_map_node_id": "UUID or null",
  "system_map_node_key": "string or null"
}
```

Contract:

- Every field is nullable. An unmapped run stores `null` for all four fields.
- A mapped run stores all four fields: the parent System Map UUID, the published revision UUID active at trigger time, the projected node UUID, and the node key (1-128 characters).
- The trigger engine writes the block once, when it creates the run snapshot, before execution begins. It does not resolve the current map again during execution or terminalization.
- The block is immutable for the lifetime of the run. Editing, republishing, or archiving a System Map cannot change an existing run's attribution.
- A future relational projection may also store the same values on `Run`, but `PipelineSnapshot` is the source of truth for the frozen attribution contract. The implementation must not read a live `system_map_nodes` row to reconstruct historical attribution.

### 5.2 Attribution Storage in `run_daily_facts` (Beta implementation contract)
Analytics and System Overview queries must survive raw run retention without joining the `runs` table. This is also **Beta implementation work**. The durable historical identity retained in `run_daily_facts` is the three-value tuple `system_map_id`, `system_map_revision_id`, and `system_map_node_key`. The projected `system_map_node_id` remains part of the run or snapshot source of truth when needed, but is not copied into the facts table or required for fact completeness. A `node_key` is revision-local by design: it remains meaningful after the physical node row, topology projection, or archived revision is no longer available, while the map and revision IDs identify the exact historical namespace in which that key was used.

The proposed `run_daily_facts` projection is:

```sql
-- Proposed Beta columns. These are not present in the current schema.
ALTER TABLE run_daily_facts
ADD COLUMN system_map_id UUID NULL,
ADD COLUMN system_map_revision_id UUID NULL,
ADD COLUMN system_map_node_key VARCHAR(128) NULL;

ALTER TABLE run_daily_facts
ADD CONSTRAINT ck_run_daily_facts_system_map_attribution_complete CHECK (
    (system_map_id IS NULL AND system_map_revision_id IS NULL AND system_map_node_key IS NULL)
    OR
    (system_map_id IS NOT NULL AND system_map_revision_id IS NOT NULL
     AND system_map_node_key IS NOT NULL
     AND length(system_map_node_key) BETWEEN 1 AND 128)
);

CREATE INDEX ix_run_daily_facts_system_map_node
ON run_daily_facts(organisation_id, system_map_id, system_map_node_key, run_date);
```

At terminalization, the fact writer copies the three retained identity values directly from the run's immutable `PipelineSnapshot` into the fact row. It does not join the current System Map, look up a replacement node, or infer attribution from a work item. The nullable tuple is either complete or entirely null. The fact row is therefore a denormalized historical record that remains queryable after raw runs or map revisions are purged.

### 5.3 Provenance Taxonomy
Every metric or evidence item displayed on a System Map node carries exactly one provenance value:

| Value | Meaning | Example UI label |
|---|---|---|
| `observed` | Modulo directly recorded the external event or response, or directly measured the runtime fact. | **Observed** - run completed at 14:03; connector received HTTP 200. |
| `reported` | An agent, external system, connector payload, webhook, or operator asserted a claim that Modulo has not independently verified. | **Reported** - GitHub says the pull request merged. |
| `derived` | Modulo calculated the value deterministically from observed facts. | **Derived** - p95 latency calculated from terminal run facts. |
| `unknown` | The value is unavailable, contradictory, stale beyond its validity window, or otherwise cannot be classified reliably. | **Unknown** - source unavailable or conflicting. |

Receipt and content have separate provenance. Modulo may **observe** that a self-report envelope arrived, including its sender, timestamp, signature status, and raw receipt metadata. The claim inside that envelope remains **reported** unless Modulo independently records the underlying event. The UI therefore renders separate badges rather than upgrading the claim: for example, `Envelope observed` beside `Claim reported`, with `Derived` shown only for calculations based on observed facts. An unavailable or contradictory source is labelled `Unknown`, never silently treated as zero or observed.

These labels prevent self-reported external data from being mistaken for Modulo's own audit evidence. The evidence drawer and aggregate cards use the same four labels, with a tooltip containing the source and calculation path.


### 5.4 Honest Reporting of Missing Data
When a pipeline or execution path is not mapped to a System Map, or when an external step lacks telemetry:
- Modulo never coerces missing metrics to zero.
- Unmapped runs are grouped under an explicit `Unattributed Flow` category.
- Nodes lacking active telemetry instrumentation display an `Unobserved` status chip.
- Dashboard totals separate observed throughput from unattributed throughput.

---

## 6. Worked Example: Modulo's Self-Governing Improvement Loops

### 6.1 The Reference Delivery System
Modulo's own engineering system serves as the primary real-world implementation of an agentic delivery system governed by System Maps.

The primary delivery flow consists of:
1. Feature Specification (`manual_capability`)
 2. Governed implementation (`pipeline`)
 3. Pre-change quality gate (`control`)
 4. Review and security gate (`pipeline` + `control`)
 5. Release admission verification (`pipeline`)
 6. Production deployment (`external_capability`)


### 6.2 The Six Autonomous Improvement Loops
Modulo operates six autonomous sidecar improvement loops. Each loop acts on a specific vulnerability, defect, or drift vector without blocking the forward delivery flow:

```
[ Primary Flow ]
Feature Spec ---> Governed Implement ---> Quality Gate ---> Review ---> Release Admission ---> Deploy
                         ^                  ^                  ^               ^
                         |                  |                  |               |
[ Improvement Loops ]    |                  |                  |               |
Security Review ----------+------------------+------------------+               |
Database Care -----------------------------+                                  |
Architecture Review ----+------------------+------------------+               |
Test Care --------------+------------------+------------------+---------------+
Interface Review -------+------------------+
Documentation Care -----+
```

#### Loop 1: Improve Security
- **Target Signal:** Security scan findings, dependency advisories, and permission boundary warnings.
- **Observation Window:** 14,400 seconds (4 hours).
- **Action:** A governed remediation workflow reviews findings, groups them by risk, applies fixes with review controls, and submits the changes for approval.
- **Budget Limit:** $150.00 USD / month.
- **Cooldown Window:** 7,200 seconds (2 hours).
- **Outcome Metric:** Net decrease in unresolved static security findings.

#### Loop 2: Improve Database
- **Target Signal:** Unindexed foreign keys, migration revision collisions, soft-delete bypasses, and un-optimized table scans.
- **Observation Window:** 28,800 seconds (8 hours).
- **Action:** A governed database-maintenance workflow inspects schema definitions and migrations, writes forward-only changes, and validates constraints.
- **Budget Limit:** $100.00 USD / month.
- **Cooldown Window:** 14,400 seconds (4 hours).
- **Outcome Metric:** Number of migration hygiene violations resolved.

#### Loop 3: Improve Architecture
- **Target Signal:** Uncovered documented behaviors, untested boundary branches, or drifted feature specifications.
- **Observation Window:** 43,200 seconds (12 hours).
- **Action:** A governed architecture-review workflow compares the documented behavior inventory with implementation and adds regression coverage for unverified behavior.
- **Budget Limit:** $200.00 USD / month.
- **Cooldown Window:** 14,400 seconds (4 hours).
- **Outcome Metric:** Percentage of product map behaviors covered by automated tests.

#### Loop 4: Improve Tests
- **Target Signal:** Test execution duration creep, stale fixtures, flaky test runs, and structural test-quality findings.
- **Observation Window:** 21,600 seconds (6 hours).
- **Action:** A governed test-maintenance workflow reviews test quality, removes misleading mocks, refactors brittle assertions, and normalizes test structure.
- **Budget Limit:** $150.00 USD / month.
- **Cooldown Window:** 7,200 seconds (2 hours).
- **Outcome Metric:** Test suite runtime reduction and zero test-style findings.

#### Loop 5: Improve UX
- **Target Signal:** Accessibility violations, responsive layout shifts, and deviations from the product's interface standards.
- **Observation Window:** 43,200 seconds (12 hours).
- **Action:** A governed interface-review workflow audits views against accessibility and interaction standards and submits reviewed interface changes.
- **Budget Limit:** $120.00 USD / month.
- **Cooldown Window:** 14,400 seconds (4 hours).
- **Outcome Metric:** Conformance score on frontend routes.

#### Loop 6: Improve Documentation
- **Target Signal:** Stale references, drifted command parameters, unverified documentation claims, and broken links.
- **Observation Window:** 86,400 seconds (24 hours).
- **Action:** A governed documentation workflow checks claims against the product and updates the documentation when it finds drift.
- **Budget Limit:** $80.00 USD / month.
- **Cooldown Window:** 21,600 seconds (6 hours).
- **Outcome Metric:** Zero unresolved documentation-validity findings.

---

## 7. User Experience, Navigation & First Login

### 7.1 Primary Navigation & First Login
System Maps is a first-class operational view.

1. **Navigation Entry:** System Maps is positioned in the primary navigation under the **BUILD** group, directly accessible at `/system-maps`.
2. **Default Home View:** When an organization has a published System Map marked as default (`is_default = TRUE`), the root route `/` automatically renders the **System Overview** dashboard instead of a generic run list.
3. **Empty / First-Run State:** If no System Map has been published, `/` renders a guided onboarding layout offering three options:
   - Select an opinionated system template (e.g. "Agentic PR Delivery with Autonomous Review").
   - Import an existing System Map specification via YAML or JSON.
   - Continue to the raw Pipelines management view.

### 7.2 Visual Language & Canvas Layout
The System Map view uses a layered, directional canvas:
- **Left-to-Right Delivery Flow:** The primary `flow` edges render from left to right using a modified Sugiyama hierarchical layout.
- **Top and Bottom Sidecars:** Improvement loops and external signal monitors render in dedicated sidecar channels above and below the main flow line, clearly differentiating primary delivery from background control loops.
- **Capacity Heatmaps:** Queue nodes and pipeline nodes display visual load indicators based on active concurrency versus configured capacity limits. A queue operating near its limit shifts from neutral slate to amber.
- **Bottleneck Indicators:** Nodes where average queue wait time exceeds execution duration display an animated bottleneck marker.

### 7.3 Node Inspector & Evidence Drawer
Clicking any node on the System Map opens the **Node Inspector** drawer on the right side of the screen:
- **Header:** Node name, type badge, ownership team, and live health status.
- **Operational Metrics (Aggregate-First):**
  - **Work in Progress (WIP):** Count of active runs or items currently in this node.
  - **Throughput:** Terminal runs per hour / day over the selected time window (24h, 7d, 30d).
  - **Latency Profile:** p50, p90, and p99 queue wait and execution duration.
  - **Failure / Gate Rejection Rate:** Percentage of terminal runs ending in failure or rejected by control gates.
  - **Capacity Utilization:** Active concurrency divided by maximum concurrency.
- **Evidence Tab:**
  - Recent run history with status chips, trigger types, and execution origins.
  - Drill-down links to raw run execution views (`/runs/:id`).
  - Optional work-item references (PR numbers, commit SHAs, issue keys) associated with runs passing through this node.
  - Provenance badges (`observed`, `derived`, `reported`) on each listed data item.

---

## 8. Authoring, Revision Lifecycle & GitOps

### 8.1 Revision Lifecycle State Machine

```
               +-------------+
               |    Draft    | <-----------------+
               +-------------+                   |
                      |                          |
                      | Publish (validates DAG)  | Edit from Published
                      v                          |
               +-------------+                   |
               |  Published  | ------------------+
               +-------------+
                      |
                      | Superseded by new revision
                      v
               +-------------+
               |  Archived   |
               +-------------+
```

### 8.2 Authoring Rules & Validation
Before a draft revision can transition to `published`, it must pass five strict graph validation checks:
1. **Connectivity:** Every node (except explicit `system_boundary` nodes) must have at least one incoming or outgoing relationship.
2. **Acyclic Delivery Flow:** The directed graph of all `flow` relationships must contain zero cycles.
3. **Control Integrity:** A `controls` relationship must originate from a `control` or `improvement_loop` node.
4. **Loop Completeness:** Every `improvement_loop` node must carry a valid configuration in `system_map_loop_configs` with all six parameters specified.
5. **Binding Existence:** All referenced `pipeline_id` values must resolve to active, non-deleted pipelines in the same organization.

### 8.3 Visual and Structured Diffing
When viewing revisions or editing a draft:
- The UI provides a side-by-side or overlay diff showing added nodes (green), removed nodes (red), and modified configurations (amber).
- A structured text diff summarizes topology changes:
  ```yaml
  nodes:
    added:
      - queue_review_backlog
    removed: []
    modified:
      governed_implementation:
        capacity_limit: "5 -> 10"
  relationships:
    added:
      - "flow: queue_review_backlog -> pipeline_pr_reviewer"
  ```

### 8.4 Rollback Semantics
An organization can roll back to a prior published revision at any time. Rolling back:
1. Creates a new published revision whose content is identical to the target historical revision.
2. Increments the `revision_number`.
3. Points `system_maps.published_revision_id` to the newly minted revision.
4. Leaves historical fact records and past run snapshots untouched.

### 8.5 Declarative GitOps Authoring (`modulo apply`)
System Maps is planned as a Beta extension to the declarative `modulo apply` engine. The current engine accepts the `api_version: modulo.dev/v1` envelope with an `entities:` mapping. The `system_maps` entity kind shown below is therefore a valid current declarative shape, but it is not yet a supported apply target: Beta work must add the engine and schema support before `modulo apply` can create or update System Maps. Do not treat this example as evidence that the current engine already accepts `system_maps`.

```yaml
api_version: modulo.dev/v1
entities:
  system_maps:
    - name: "Core SDLC Delivery"
      description: "The governed delivery system."
      visibility: org
      owner_team_ref: "Product Architecture"
      nodes:
        - key: spec_triage
          name: "Specification Triage"
          type: manual_capability
          config_json:
            owner_role: "Product Architect"
            service_level:
              sla_hours: 24
        - key: governed_implementation
          name: "Governed Implementation"
          type: pipeline
          pipeline_ref: "Governed Implementation"
          config_json:
            execution:
              max_concurrent_runs: 8
              timeout_seconds: 1800
        - key: gate_lint_test
          name: "Lint & Architecture Gate"
          type: control
          config_json:
            control_type: gate
            fail_action: block
            pass_threshold: 1.0
        - key: loop_security
          name: "Improve Security Loop"
          type: improvement_loop
          config_json:
            action_pipeline_ref: "Improve Security"
            target_signal: "security_vulnerability_count"
            observation_window_seconds: 14400
            budget_limit_usd: 150.00
            cooldown_seconds: 7200
            outcome_metric: "delta_vulnerabilities"
      relationships:
        - key: flow_spec_to_implementation
          type: flow
          source: spec_triage
          target: governed_implementation
        - key: flow_implementation_to_gate
          type: flow
          source: governed_implementation
          target: gate_lint_test
        - key: ctrl_security_to_implementation
          type: controls
          source: loop_security
          target: governed_implementation
```

#### Declarative reference resolution

When Beta adds `system_maps` support, `modulo apply` will resolve its authoring references before it writes or publishes relational projections. The intended mapping is:

| YAML authoring field | Resolved relational projection | Accepted reference | Resolution rule |
|---|---|---|---|
| `owner_team_ref` | `owner_team_id` | Team UUID or exact team name | Resolve within the current organisation; the team must be active and permitted by the map's visibility. |
| `pipeline_ref` | `pipeline_id` | Pipeline UUID or exact pipeline name | Resolve an active, non-deleted pipeline within the current organisation. |
| `config_json.action_pipeline_ref` | `action_pipeline_id` | Pipeline UUID or exact pipeline name | Resolve an active, non-deleted action pipeline within the current organisation. |

UUIDs are matched by ID. Names are matched exactly after the documented case-insensitive comparison; surrounding whitespace is not significant, but partial names, aliases, and cross-organisation names are not accepted. A name that matches zero rows fails the apply. A name that matches more than one row also fails the apply with an ambiguity error and requires the author to use the UUID form. A UUID from another organisation, a deleted resource, or a resource outside the permitted team scope fails validation. Apply is atomic: it does not publish a partially resolved graph.

For an `improvement_loop`, `config_json.action_pipeline_ref` is the only YAML source for the action binding. After resolution, `action_pipeline_id` is the validated pipeline binding used when the loop dispatches work. It is not a display-only copy and must never be inferred from `pipeline_ref`, the node name, or the current pipeline catalog after publication. The published loop configuration retains the resolved binding in its immutable revision.

---

## 9. Migration & Deprecation Strategy for Lifecycle Maps

### 9.1 Evaluation of Legacy Lifecycle Maps
The legacy `feat-lifecycle-maps` implementation focused on tracking individual work-item journeys through sequential status columns. This resulted in low adoption because users treated it as a weak copy of Linear boards.

Several underlying technical components remain sound and will be retained.

### 9.2 What Is Retained
1. **Layered Auto-Layout Algorithm:** The existing layered graph layout engine cleanly positions nodes and computes routing paths. This algorithm is retained and extended to support top and bottom sidecar channels.
2. **Deterministic Work-Item Key Derivation:** The existing canonical work-item reference normalizer reliably collapses equivalent external pull request, issue, and commit references. This logic is preserved in the Evidence Drawer for optional drill-down lookups.
3. **Cursor-Based Fact Pagination:** The existing keyset pagination patterns are reused for the Node Inspector evidence tables.
4. **Concurrency Locking Patterns:** The existing row-locking safeguards that prevent concurrent revision save collisions are preserved.

### 9.3 What Is Deprecated and Discarded
1. **Mutable Stage State Columns:** The `lifecycle_map_stages` table which stored mutable stage position and status lists is retired. Replaced by `system_map_nodes` projections.
2. **Ticket Journey Boards:** The column-based work-item card UI is discarded.
3. **Stage Graduation RPCs:** The endpoints that manually advanced work items between stages (`/graduate`) are removed. Modulo tracks executable pipeline facts, not manual board state.

### 9.4 Database Migration Plan
1. A new migration creates `system_maps`, `system_map_revisions`, `system_map_nodes`, `system_map_relationships`, and `system_map_loop_configs`.
2. Existing `lifecycle_maps` tables remain untouched during the first release of System Maps. The existing feature remains available according to its current product configuration.
3. In Beta, an opt-in migration script transforms legacy `lifecycle_maps` content into initial `system_maps` draft revisions, converting legacy stages into `pipeline` or `manual_capability` nodes.
4. The deprecated `lifecycle_maps` tables are formally dropped at GA.

---

## 10. Staged Beta Delivery Plan

### Beta 1: System Overview Read Model & Static Projection
- **Scope:**
  - Database migration creating `system_maps`, `system_map_revisions`, `system_map_nodes`, and `system_map_relationships`.
  - Snapshot attribution freezing in `create_run`.
  - Attribution column additions to `run_daily_facts`.
  - Read-only System Overview UI displaying published system topography.
  - Node Inspector showing aggregated throughput, latency, and failure metrics derived from `run_daily_facts`.
- **Exit Criteria:** A predefined system map renders live operational metrics without modifying running pipelines.

### Beta 2: Visual Authoring, Revision Lifecycle & Draft/Publish
- **Scope:**
  - Interactive canvas editor supporting drag-and-drop node placement and edge connection.
  - Node configuration dialogs for all seven node types.
  - Graph validation engine enforcing DAG delivery flow and control constraints.
  - Draft, publish, diff, and rollback revision workflow.
  - `modulo apply` declarative YAML support for System Maps.
- **Exit Criteria:** Platform engineers can create, validate, publish, and update custom system maps via UI and CLI.

### Beta 3: Improvement Loop Sidecars and Autonomous Feedback Metrics
- **Scope:**
  - Implementation of `system_map_loop_configs` relational storage and validation.
  - Dedicated sidecar rendering channels for improvement loops on the canvas.
  - Signal monitoring service connecting target metrics to loop action triggers.
  - Cooldown and budget enforcement for improvement loops.
  - Loop outcome reporting (measuring signal before and after remediation runs).
- **Exit Criteria:** Modulo's six improvement loops run with visible budget, cooldown, and outcome tracking on the map.

### Beta 4: Operational Recommendations, Bottleneck Detection & Autonomy Attestation
- **Scope:**
  - Automated bottleneck detection algorithms flagging unbalanced queues and high wait times.
  - Starvation and capacity overload alerts.
  - Autonomy level aggregation: calculating the percentage of delivery stages operating without human intervention.
  - Exportable System Governance Report for compliance and executive review.
- **Exit Criteria:** System Maps proactively highlights delivery bottlenecks and generates autonomy compliance attestations.

---

## 11. Non-Goals, Risks & Mitigations, and Open Decisions

### 11.1 Explicit Non-Goals
1. **Modulo is not an Issue Tracker:** Modulo will not implement ticket creation forms, sprint backlogs, kanban stage transitions, or user story point estimation.
2. **Modulo is not an Arbitrary Cycle Engine:** Modulo will not allow cycles in the primary delivery flow. Looping workflows must be modeled as bounded control sidecars.
3. **Modulo is not an APM or Distributed Tracing Vendor:** Modulo monitors pipeline runs, handoffs, and SDLC capabilities. It does not replace Datadog, Dynatrace, or OpenTelemetry application performance monitoring in production runtime services.
4. **Modulo does not Manage Developer Time:** Modulo measures pipeline throughput, queue latency, and autonomous loop outcomes. It does not track individual developer velocity, hours, or personal activity.

### 11.2 Risks & Mitigations

| Risk | Consequence | Mitigation |
|---|---|---|
| **Query Contention on Live Dashboard** | Frequent polling of System Maps dashboard overloads the PostgreSQL database. | Query analytics facts (`run_daily_facts`) and cached active run counts; never run unindexed full-table scans over raw `runs` or `audit_events`. |
| **Graph Clutter & Sprawl** | Large enterprises model hundreds of nodes, turning the canvas into an illegible tangle. | Enforce hierarchical `system_boundary` containment; support drill-down sub-maps; provide collapsed subsystem nodes. |
| **False-Positive Bottleneck Alerts** | Normal batch cadences (e.g. nightly builds) trigger spurious bottleneck warnings. | Require minimum sample sizes and observation windows before flagging a queue as stalled or bottlenecked. |
| **Runaway Improvement Loops** | An autonomous improvement loop spins out of control, generating dozens of conflicting PRs. | Hard DB-enforced constraints: mandatory cooldown windows, monthly spend caps, and maximum concurrent runs per loop. |
| **Data Sync Drift with Issue Trackers** | Work-item keys displayed in the Evidence Drawer drift out of sync with external trackers. | Work items are non-authoritative drill-down references; Modulo never writes back ticket status to external trackers. |

### 11.3 Open Architecture & Product Decisions

#### Decision 1: Default Landing Page Behavior
- **Tension:** Should `/` automatically navigate to System Maps for all users once a map is published, or should it remain configurable per user role?
- **Trade-offs:**
  - Making System Maps the default home page reinforces Modulo's category positioning as a systems governance platform and gives immediate visibility into delivery health.
  - However, operational engineers who spend their day investigating individual failed runs may prefer the tabular Runs view as their default.
- **Recommendation:** Default to the System Overview dashboard for the organization, but allow individual users to toggle their preferred landing view (`System Overview` vs `Runs List`) in their account preferences.
- **Reasoning:** System Maps establishes the product identity upon login, while preserving efficiency for operators who need direct tabular run inspection.

#### Decision 2: Plan Tier Gating for System Maps
- **Tension:** What functionality in System Maps belongs in the free Community tier versus the paid Team and Enterprise tiers?
- **Trade-offs:**
  - Restricting the visual System Map to paid tiers limits top-of-funnel adoption and hides Modulo's defining visual surface.
  - Making everything free removes commercial incentive for engineering leadership.
- **Recommendation:** Core principle 6 applies: "Run-level truth is free." In System Maps:
  - **Community Tier:** Authoring, publishing, and live operational inspection of 1 System Map with up to 15 nodes; live WIP and health status.
  - **Team Tier:** Multiple System Maps, unlimited nodes, team-level visibility controls, improvement loop budget/cooldown automation, and 90-day historical trend aggregation.
  - **Enterprise Tier:** Declarative GitOps sync (`modulo apply`), compliance autonomy attestation export, and cross-organization boundaries.
- **Reasoning:** This allows any self-hoster to model and observe their delivery pipeline for free, while charging for multi-team scale, long-term trend retention, and automated governance controls.

#### Decision 3: Automatic Topology Discovery vs Explicit Modeling
- **Tension:** Should System Maps automatically generate nodes and edges by observing pipeline triggers and connector webhooks, or should authoring be strictly explicit?
- **Trade-offs:**
  - Auto-discovery reduces initial setup friction by generating an initial draft from existing pipelines and connectors.
  - However, automated inference often produces messy, unreadable topologies with misleading edge semantics, confusing users about system boundaries.
- **Recommendation:** Start with explicit authoring and template-based initialization in Beta 1 and Beta 2. Introduce an optional "Infer System Draft" wizard in Beta 3 that suggests node connections based on webhook triggers, but requires human approval before publishing.
- **Reasoning:** Explicit architecture preserves conceptual clarity. Modulo should never guess delivery topology without confirmation.
