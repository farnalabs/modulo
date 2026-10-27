# Delivery System Maps Specification

**Status:** Draft
**Target Milestone:** Beta
**Ticket:** FAR-2201
**Category:** Product & Technical Architecture Specification
**Related:** `docs/architecture.md`, `docs/core-principles.md`, ADR 020 (analytics facts table), ADR 044 (agent dispatch model)

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

### 3.2 Relational Entities

```
+---------------------+         1:N         +-----------------------------+
|    system_maps      |-------------------->|    system_map_revisions     |
+---------------------+                     +-----------------------------+
| id (UUID, PK)       |                     | id (UUID, PK)               |
| organisation_id     |                     | map_id (UUID, FK)           |
| name (VARCHAR)      |                     | revision_number (INT)       |
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
        | external_ref (VARCHAR, opt) |     | config_json (JSONB)         |
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
-- Core System Map table
CREATE TABLE system_maps (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organisation_id UUID NOT NULL,
    name VARCHAR(255) NOT NULL,
    description TEXT,
    visibility VARCHAR(10) NOT NULL DEFAULT 'org',
    owner_team_id UUID REFERENCES teams(id) ON DELETE RESTRICT,
    published_revision_id UUID,
    is_default BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    deleted_at TIMESTAMPTZ,
    account_id UUID NOT NULL REFERENCES accounts(id) ON DELETE RESTRICT,
    updated_by UUID REFERENCES accounts(id) ON DELETE SET NULL,
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
    organisation_id UUID NOT NULL,
    map_id UUID NOT NULL REFERENCES system_maps(id) ON DELETE CASCADE,
    revision_number INT NOT NULL,
    status VARCHAR(20) NOT NULL DEFAULT 'draft',
    nodes_json JSONB NOT NULL DEFAULT '[]'::jsonb,
    edges_json JSONB NOT NULL DEFAULT '[]'::jsonb,
    layout_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    change_summary TEXT,
    published_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    account_id UUID NOT NULL REFERENCES accounts(id) ON DELETE RESTRICT,
    CONSTRAINT ck_system_map_revisions_status CHECK (status IN ('draft', 'published', 'archived')),
    CONSTRAINT ck_system_map_revisions_number CHECK (revision_number > 0),
    CONSTRAINT uq_system_map_revisions_map_num UNIQUE (map_id, revision_number)
);

ALTER TABLE system_maps ADD CONSTRAINT fk_system_maps_published_rev
FOREIGN KEY (published_revision_id) REFERENCES system_map_revisions(id) ON DELETE SET NULL;

-- Relational projection of nodes for fast joins and indexing
CREATE TABLE system_map_nodes (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organisation_id UUID NOT NULL,
    revision_id UUID NOT NULL REFERENCES system_map_revisions(id) ON DELETE CASCADE,
    node_key VARCHAR(128) NOT NULL,
    node_type VARCHAR(32) NOT NULL,
    name VARCHAR(255) NOT NULL,
    description TEXT,
    pipeline_id UUID REFERENCES pipelines(id) ON DELETE SET NULL,
    external_ref VARCHAR(255),
    capacity_limit INT,
    config_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
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
    ),
    CONSTRAINT uq_system_map_nodes_rev_key UNIQUE (revision_id, node_key)
);

CREATE INDEX ix_system_map_nodes_pipeline ON system_map_nodes(pipeline_id)
WHERE pipeline_id IS NOT NULL;

-- Relational projection of edges
CREATE TABLE system_map_relationships (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organisation_id UUID NOT NULL,
    revision_id UUID NOT NULL REFERENCES system_map_revisions(id) ON DELETE CASCADE,
    relationship_key VARCHAR(128) NOT NULL,
    source_node_key VARCHAR(128) NOT NULL,
    target_node_key VARCHAR(128) NOT NULL,
    relationship_type VARCHAR(32) NOT NULL,
    config_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT ck_system_map_rel_type CHECK (
        relationship_type IN (
            'flow',
            'depends_on',
            'controls',
            'feeds_back_to',
            'owned_by',
            'measured_by',
            'capacity_limited_by',
            'contains'
        )
    ),
    CONSTRAINT uq_system_map_rel_rev_key UNIQUE (revision_id, relationship_key)
);

CREATE INDEX ix_system_map_rel_source ON system_map_relationships(revision_id, source_node_key);
CREATE INDEX ix_system_map_rel_target ON system_map_relationships(revision_id, target_node_key);

-- First-class configuration table for improvement loops
CREATE TABLE system_map_loop_configs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organisation_id UUID NOT NULL,
    revision_id UUID NOT NULL REFERENCES system_map_revisions(id) ON DELETE CASCADE,
    node_key VARCHAR(128) NOT NULL,
    target_signal VARCHAR(255) NOT NULL,
    observation_window_seconds INT NOT NULL,
    action_pipeline_id UUID NOT NULL REFERENCES pipelines(id) ON DELETE RESTRICT,
    budget_limit_usd NUMERIC(10, 4) NOT NULL,
    cooldown_seconds INT NOT NULL,
    outcome_metric VARCHAR(255) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT ck_system_map_loop_obs_window CHECK (observation_window_seconds >= 60),
    CONSTRAINT ck_system_map_loop_budget CHECK (budget_limit_usd > 0),
    CONSTRAINT ck_system_map_loop_cooldown CHECK (cooldown_seconds >= 0),
    CONSTRAINT uq_system_map_loop_rev_node UNIQUE (revision_id, node_key)
);
```

---

## 4. Graph Semantics & Node / Relationship Typology

### 4.1 Node Typology

| Node Type | Purpose | Primary Properties | Modulo Binding |
|---|---|---|---|
| `pipeline` | Represents an automated, executable Modulo pipeline. | `pipeline_id`, `max_concurrent_runs`, `timeout_seconds` | Bound directly to `pipelines.id`. |
| `external_capability` | Represents work executed outside Modulo (e.g. GitHub Actions, ArgoCD, Jenkins, security scanner). | `provider`, `external_ref`, `execution_mode` | Optional connector binding for status telemetry. |
| `manual_capability` | Represents human engineering work (e.g. architectural spikes, design review, manual security audit). | `owner_role`, `team_id`, `sla_hours` | Associated with team responsibilities. |
| `queue` | Represents an intermediate buffer or backlog holding items between capabilities. | `queue_type`, `capacity_limit`, `drain_rate_per_hour` | Backlog or SAQ task queue binding. |
| `control` | Represents a deterministic evaluation gate, safety guardrail, or human approval barrier. | `control_type` (gate / guardrail / approval), `pass_threshold`, `fail_action` | Bound to an Eval definition, guardrail pin, or HITL edge. |
| `improvement_loop` | Represents an autonomous sidecar pipeline monitoring system signals and deploying incremental fixes. | Loop configuration (signal, window, action, budget, cooldown, metric) | Bound to `system_map_loop_configs` and an action pipeline. |
| `system_boundary` | Demarcates an organizational boundary, environment trust zone, or compliance perimeter. | `boundary_type` (network / team / environment), `compliance_scope` | Container grouping via `contains` relationships. |

### 4.2 Relationship Typology

| Relationship Type | Direction | Semantic Meaning | Validation Rule |
|---|---|---|---|
| `flow` | Source -> Target | Primary delivery flow of code, artifacts, or deliverables. | Must connect non-loop nodes; must be acyclic. |
| `depends_on` | Dependent -> Prerequisite | Operational or architectural dependency. | Source capability cannot operate without target. |
| `controls` | Controller -> Controlled | Governs execution, gating, or admission into a capability. | Source must be `control` or `improvement_loop`. |
| `feeds_back_to` | Source -> Target | Asynchronous diagnostic signal or metric emission. | Target is an upstream queue, signal buffer, or loop. |
| `owned_by` | Node -> Team | Operational stewardship and responsibility. | Target must be an active organizational team. |
| `measured_by` | Node -> Metric | Telemetry stream or daily fact dimension. | Binds an aggregate metric to a node. |
| `capacity_limited_by`| Node -> Queue/Slot | Concurrency clamp or slot pool constraint. | Clamps throughput based on shared capacity. |
| `contains` | Boundary -> Node | Hierarchical enclosure inside a subsystem or perimeter. | Source must be `system_boundary`. |

### 4.3 Loop Semantics & Acyclic Delivery Flow
A core failure mode of naive delivery graphs is introducing arbitrary executable cycles into the pipeline flow. When a pipeline failure redirects an artifact back to an earlier pipeline without structural limits, the system risks infinite execution loops, unbounded cloud compute spend, and cascading queue collapse.

System Maps enforces strict loop semantics:
1. **The delivery flow is acyclic:** Every relationship of type `flow` belongs to the primary delivery flow. The directed subgraph formed by all `flow` edges must be a Directed Acyclic Graph (DAG). Any draft containing a cycle among `flow` edges is rejected by the graph validator at save and publish time.
2. **Improvement loops are modeled as control sidecars:** An `improvement_loop` is never placed as an inline node on a `flow` edge. It sits adjacent to the flow as an autonomous observer. It reads signals via `feeds_back_to` or metric streams, and acts upon capabilities or queues through `controls` or asynchronous trigger actions.
3. **The Six-Parameter Improvement Loop Contract:** Every `improvement_loop` node must define exactly six control parameters:
   - **Target Signal (`target_signal`):** The quantifiable operational symptom or defect to monitor (e.g. `failing_test_ratio`, `uncovered_architecture_gaps`, `critical_security_findings`).
   - **Observation Window (`observation_window_seconds`):** The time duration over which signal data is collected before deciding to act (e.g. 3600 seconds, 86400 seconds).
   - **Action Pipeline (`action_pipeline_id`):** The executable Modulo pipeline dispatched to investigate and apply a remediation (e.g. Improve Architecture, Improve Security).
   - **Budget Limit (`budget_limit_usd`):** Maximum model and compute spend allocated to this loop per calendar month.
   - **Cooldown Window (`cooldown_seconds`):** Minimum quiet period required after a remediation run before the loop is permitted to fire again, preventing rapid-fire oscillatory commits.
   - **Outcome Metric (`outcome_metric`):** The verifiable metric used to confirm whether the remediation succeeded or failed (e.g. `delta_vulnerabilities`, `delta_test_failures`).

---

## 5. Data Contracts, Attribution & Provenance

### 5.1 Snapshot Attribution Freezing
A pipeline run must never guess its position in the organizational delivery system at runtime. When a run is initialized by the trigger engine, Modulo inspects the organization's published System Map.

If the pipeline is mapped to a `pipeline` node in the published map revision:
1. The run record permanently stores:
   - `system_map_id`: UUID of the parent System Map.
   - `system_map_revision_id`: UUID of the exact published revision active at trigger time.
   - `system_map_node_id`: UUID of the mapped `system_map_nodes` row.
   - `system_map_node_key`: VARCHAR(128) string identifier of the mapped node within the revision (allowing `record_run_facts` to copy it directly into `run_daily_facts` at terminalization without joining `system_map_nodes`, in compliance with ADR 020).
2. The run's immutable `PipelineSnapshot` captures this attribution block. If the System Map is updated or archived tomorrow, the historical run's attribution remains frozen.

### 5.2 Attribution Storage in `run_daily_facts`
To comply with ADR 020, analytics and system overview queries must survive the 90-day raw run retention purge without joining the `runs` table.

The `run_daily_facts` table is extended with three attribution columns:

```sql
ALTER TABLE run_daily_facts
ADD COLUMN system_map_id UUID,
ADD COLUMN system_map_revision_id UUID,
ADD COLUMN system_map_node_key VARCHAR(128);

CREATE INDEX ix_run_daily_facts_system_map_node
ON run_daily_facts(organisation_id, system_map_id, system_map_node_key, run_date);
```

When `core.analytics.record_run_facts` executes during run terminalization, it copies `system_map_id`, `system_map_revision_id`, and the node's string identifier (`system_map_node_key`) into the fact row. This guarantees that multi-year system performance trends, node throughput, and loop remediation statistics remain queryable without keeping raw run blobs.

### 5.3 Provenance Taxonomy
Every aggregate metric displayed on a System Map node or evidence drawer carries an explicit provenance tag:

```
+------------------------------------------------------------------------+
| Provenance Tag | Source Description                                   |
|----------------|------------------------------------------------------|
| observed       | Directly recorded by Modulo runtime (run start/end,  |
|                | gate decisions, wall-clock duration, token spend).    |
| derived        | Computed deterministically from observed facts       |
|                | (queue latency, failure rate, 7-day moving averages). |
| reported       | Received from external connectors or webhooks        |
|                | (GitHub PR merged, CI runner status, Jira status).   |
| unknown        | Incomplete or unverified data source.                |
+------------------------------------------------------------------------+
```

Provenance tags prevent teams from mistaking self-reported external data for verified Modulo audit evidence.

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
2. Prompt-to-PR / Worker Implementation (`pipeline`)
3. Pre-PR Quality & Lint Gate (`control`)
4. PR Reviewer & Security Gate (`pipeline` + `control`)
5. Merge Queue Verification (`pipeline`)
6. Production Deployment (`external_capability`)

### 6.2 The Six Autonomous Improvement Loops
Modulo operates six autonomous sidecar improvement loops. Each loop acts on a specific vulnerability, defect, or drift vector without blocking the forward delivery flow:

```
[ Primary Flow ]
Feature Spec ---> Worker Implement ---> QA/Lint Gate ---> PR Review ---> Merge Queue ---> Deploy
                         ^                  ^                  ^               ^
                         |                  |                  |               |
[ Improvement Loops ]    |                  |                  |               |
Improve Security --------+------------------+------------------+               |
Improve Database ---------------------------+                                  |
Improve Architecture ----+------------------+------------------+               |
Improve Tests -----------+------------------+------------------+---------------+
Improve UX --------------+------------------+
Improve Docs ------------+
```

#### Loop 1: Improve Security
- **Target Signal:** Static analysis findings (`bandit`, `semgrep`), dependency audit alerts (`pip-audit`), and permission boundary warnings.
- **Observation Window:** 14,400 seconds (4 hours).
- **Action Pipeline:** `improve-security` pipeline. Scans repo, isolates findings into XS/S/M/L categories, fixes vulnerabilities in an isolated worktree, and submits a PR.
- **Budget Limit:** $150.00 USD / month.
- **Cooldown Window:** 7,200 seconds (2 hours).
- **Outcome Metric:** Net decrease in unresolved static security findings.

#### Loop 2: Improve Database
- **Target Signal:** Unindexed foreign keys, migration revision collisions, soft-delete bypasses, and un-optimized table scans.
- **Observation Window:** 28,800 seconds (8 hours).
- **Action Pipeline:** `improve-database` pipeline. Inspects models and migrations, writes idempotent forward migration scripts, and validates schema constraints.
- **Budget Limit:** $100.00 USD / month.
- **Cooldown Window:** 14,400 seconds (4 hours).
- **Outcome Metric:** Number of migration hygiene violations resolved.

#### Loop 3: Improve Architecture
- **Target Signal:** Uncovered product map behaviors, untested boundary branches, or drifted feature specifications.
- **Observation Window:** 43,200 seconds (12 hours).
- **Action Pipeline:** `improve-architecture` pipeline. Walks `frontend/src/manifest.yaml`, identifies unverified behavior checkboxes, and writes regression tests.
- **Budget Limit:** $200.00 USD / month.
- **Cooldown Window:** 14,400 seconds (4 hours).
- **Outcome Metric:** Percentage of product map behaviors covered by automated tests.

#### Loop 4: Improve Tests
- **Target Signal:** Test execution duration creep, test rot patterns (e.g. outdated fixtures), flaky test runs, and test-style violations.
- **Observation Window:** 21,600 seconds (6 hours).
- **Action Pipeline:** `improve-tests` pipeline. Runs test-style architecture scanners, removes dead mocks, refactors brittle assertions, and normalizes test structures.
- **Budget Limit:** $150.00 USD / month.
- **Cooldown Window:** 7,200 seconds (2 hours).
- **Outcome Metric:** Test suite runtime reduction and zero test-style findings.

#### Loop 5: Improve UX
- **Target Signal:** WCAG accessibility violations (contrast, missing labels, missing `aria-live` on dynamic regions), responsive layout shifts, and criteria deviations.
- **Observation Window:** 43,200 seconds (12 hours).
- **Action Pipeline:** `improve-ux` pipeline. Audits frontend views against `ux-conformance/criteria.yaml` and delivers UI polish PRs.
- **Budget Limit:** $120.00 USD / month.
- **Cooldown Window:** 14,400 seconds (4 hours).
- **Outcome Metric:** Conformance score on frontend routes.

#### Loop 6: Improve Documentation
- **Target Signal:** Stale path references, drifted CLI parameters, unverified docstring claims, and broken internal links.
- **Observation Window:** 86,400 seconds (24 hours).
- **Action Pipeline:** `improve-docs` pipeline. Runs doc validity sweeps, compares documented behavior against code call sites, and updates documentation.
- **Budget Limit:** $80.00 USD / month.
- **Cooldown Window:** 21,600 seconds (6 hours).
- **Outcome Metric:** Zero doc-validity findings in `scripts/check_doc_validity.py`.

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
    added: [queue_review_backlog]
    removed: []
    modified:
      pipeline_worker:
        capacity_limit: 5 -> 10
  relationships:
    added:
      - flow: queue_review_backlog -> pipeline_pr_reviewer
  ```

### 8.4 Rollback Semantics
An organization can roll back to a prior published revision at any time. Rolling back:
1. Creates a new published revision whose content is identical to the target historical revision.
2. Increments the `revision_number`.
3. Points `system_maps.published_revision_id` to the newly minted revision.
4. Leaves historical fact records and past run snapshots untouched.

### 8.5 Declarative GitOps Authoring (`modulo apply`)
System Maps supports declarative specification via `modulo apply`. A file named `system-map.yaml` can be committed to version control and applied via CI:

```yaml
version: modulo/v1alpha1
kind: SystemMap
metadata:
  name: "Core SDLC Delivery"
  visibility: org
spec:
  nodes:
    - key: spec_triage
      name: "Specification Triage"
      type: manual_capability
      owner_team: "Product Architecture"

    - key: pipeline_worker
      name: "Autonomous Worker Delivery"
      type: pipeline
      pipeline_ref: "Worker Implementation"
      capacity_limit: 8

    - key: gate_lint_test
      name: "Lint & Architecture Gate"
      type: control
      config:
        control_type: gate
        fail_action: block

    - key: loop_security
      name: "Improve Security Loop"
      type: improvement_loop
      loop_config:
        target_signal: "security_vulnerability_count"
        observation_window_seconds: 14400
        action_pipeline_ref: "Improve Security"
        budget_limit_usd: 150.00
        cooldown_seconds: 7200
        outcome_metric: "delta_vulnerabilities"

  relationships:
    - key: flow_spec_to_worker
      type: flow
      source: spec_triage
      target: pipeline_worker

    - key: flow_worker_to_gate
      type: flow
      source: pipeline_worker
      target: gate_lint_test

    - key: ctrl_security_to_worker
      type: controls
      source: loop_security
      target: pipeline_worker
```

---

## 9. Migration & Deprecation Strategy for Lifecycle Maps

### 9.1 Evaluation of Legacy Lifecycle Maps
The legacy `feat-lifecycle-maps` implementation focused on tracking individual work-item journeys through sequential status columns. This resulted in low adoption because users treated it as a weak copy of Linear boards.

Several underlying technical components remain sound and will be retained.

### 9.2 What Is Retained
1. **Layered Auto-Layout Algorithm:** The Sugiyama-style layered graph auto-layout engine (`frontend/src/__tests__/lifecycleMapLayout.spec.ts`) cleanly positions nodes and computes routing paths. This algorithm is retained and extended to support top/bottom sidecar channels.
2. **Deterministic Work-Item Key Derivation:** The `uuid5` canonical work-item reference normalizer (`modulo.db.lifecycle_refs`) reliably collapses variations of GitHub PR URLs, Linear keys, and Jira ticket formats. This logic is preserved in the Evidence Drawer for optional drill-down lookups.
3. **Cursor-Based Fact Pagination:** The keyset pagination patterns developed for journey detail endpoints are reused for the Node Inspector evidence tables.
4. **Concurrency Locking Patterns:** The row-locking safeguards (`FOR UPDATE` bounded helpers) that prevent concurrent revision save collisions are preserved.

### 9.3 What Is Deprecated and Discarded
1. **Mutable Stage State Columns:** The `lifecycle_map_stages` table which stored mutable stage position and status lists is retired. Replaced by `system_map_nodes` projections.
2. **Ticket Journey Boards:** The column-based work-item card UI is discarded.
3. **Stage Graduation RPCs:** The endpoints that manually advanced work items between stages (`/graduate`) are removed. Modulo tracks executable pipeline facts, not manual board state.

### 9.4 Database Migration Plan
1. A new migration creates `system_maps`, `system_map_revisions`, `system_map_nodes`, `system_map_relationships`, and `system_map_loop_configs`.
2. Existing `lifecycle_maps` tables remain untouched in Alpha (hidden behind internal flags per FAR-2200).
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

### Beta 3: Improvement Loop Sidecars & Autonomous Feedback Metrics
- **Scope:**
  - Implementation of `system_map_loop_configs` relational storage and validation.
  - Dedicated sidecar rendering channels for improvement loops on the canvas.
  - Signal monitoring daemon connecting target metrics to loop action pipeline triggers.
  - Cooldown and budget enforcement for improvement loops.
  - Loop outcome reporting (measuring signal before and after remediation runs).
- **Exit Criteria:** Modulo's six dogfood improvement loops run with visible budget, cooldown, and outcome tracking on the map.

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
