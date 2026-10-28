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

**Tenant integrity has two explicit enforcement boundaries.** Every System Map row carries `organisation_id`. Foreign keys between the new System Map tables use composite keys where both tables own the organisation column, so a revision can reference only its map and a relationship can reference only nodes in its revision. New binding and admission rows also carry a non-null `organisation_id`; their references to existing Modulo resources use ordinary primary-key foreign keys plus one shared database-backed validation function, `validate_system_map_resource_org(resource_kind, resource_id, expected_org_id, expected_team_id)`. Every writer, including UI, `modulo apply`, publication, admission, reconciliation, and administrative repair, calls that validation in the same transaction before inserting or publishing. The function reads the resource under the current RLS context and fails closed on a missing, deleted, cross-organisation, or out-of-scope resource. This is not pure relational foreign-key tenant safety: existing tables do not currently expose the composite keys needed for that claim.

`accounts` is a global table and does not gain an `organisation_id` in this design. Account ownership and audit columns therefore use the existing `account_id` foreign key only. At write time, the same transaction validates active membership through the shared membership check before accepting it as `account_id` or `updated_by`; the transaction rejects an account that lacks membership. This is an explicit database-backed membership rule, not an invented account composite key.

Node keys remain stable within a revision for import/export and human-readable diffs. The relationship and loop-config tables enforce those key references in SQL using composite foreign keys because those new tables own both `revision_id` and `organisation_id`. Existing-resource bindings are protected by the shared validation function on every write, not by a composite FK that does not exist. `external_ref` and values inside `config_json` are application-level data: they do not identify Modulo rows and cannot provide a relational tenant boundary.

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
        | max_concurrent_runs (INT)   |
        | cooldown_seconds (INT)      |
        | outcome_metric (VARCHAR)    |
        +-----------------------------+
```

### 3.3 Database Table Definitions

```sql
-- Existing tables are not changed to add composite keys as a prerequisite.
-- New System Map tables use composite keys only between tables defined here.
-- Foreign keys to existing resources use their current primary keys, while every
-- writer calls validate_system_map_resource_org(...) in the same transaction.
-- The validator reads under current RLS context and fails closed on a missing,
-- deleted, cross-organisation, or out-of-scope resource.
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
    max_concurrent_runs INT NOT NULL,
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
    CONSTRAINT ck_system_map_loop_concurrency CHECK (max_concurrent_runs >= 1),
    CONSTRAINT ck_system_map_loop_cooldown CHECK (cooldown_seconds >= 0),
    CONSTRAINT uq_system_map_loop_rev_node_org UNIQUE (revision_id, node_key, organisation_id)
);

-- Admission attempts are durable decisions, including rejections that happen
-- before a run exists. A rejected attempt never receives a run_id.
CREATE TABLE system_map_loop_admission_attempts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organisation_id UUID NOT NULL REFERENCES organisations(id) ON DELETE RESTRICT,
    loop_config_id UUID NOT NULL,
    admission_key VARCHAR(255) NOT NULL,
    run_id UUID NULL,
    month_start DATE NOT NULL,
    status VARCHAR(20) NOT NULL,
    rejection_reason VARCHAR(32),
    attempted_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_system_map_loop_attempts_id_org UNIQUE (id, organisation_id),
    CONSTRAINT uq_system_map_loop_attempts_key
        UNIQUE (organisation_id, loop_config_id, admission_key),
    CONSTRAINT fk_system_map_loop_attempts_loop_org
        FOREIGN KEY (loop_config_id, organisation_id)
        REFERENCES system_map_loop_configs(id, organisation_id) ON DELETE CASCADE,
    CONSTRAINT ck_system_map_loop_attempts_status
        CHECK (status IN ('rejected', 'admitted')),
    CONSTRAINT ck_system_map_loop_attempts_outcome CHECK (
        (status = 'rejected' AND run_id IS NULL AND rejection_reason IS NOT NULL
         AND rejection_reason IN ('attribution', 'concurrency', 'cooldown', 'budget',
                                  'unattributed', 'inactive_pipeline', 'unpublished_revision'))
        OR
        (status = 'admitted' AND run_id IS NOT NULL AND rejection_reason IS NULL)
    )
);

-- Reservations represent admitted capacity, not every attempt. They are created
-- only after run creation and therefore always have a run_id.
CREATE TABLE system_map_loop_reservations (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organisation_id UUID NOT NULL REFERENCES organisations(id) ON DELETE RESTRICT,
    admission_attempt_id UUID NOT NULL,
    loop_config_id UUID NOT NULL,
    run_id UUID NOT NULL,
    month_start DATE NOT NULL,
    reserved_budget_usd NUMERIC(10, 4) NOT NULL,
    actual_spend_usd NUMERIC(10, 4),
    reserved_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    -- Immutable admission timestamp plus its derived cooldown boundary. This
    -- boundary survives every later status transition, including cancellation.
    cooldown_until TIMESTAMPTZ NOT NULL,
    released_at TIMESTAMPTZ,
    status VARCHAR(20) NOT NULL,
    CONSTRAINT uq_system_map_loop_reservations_id_org UNIQUE (id, organisation_id),
    CONSTRAINT uq_system_map_loop_reservations_attempt UNIQUE (admission_attempt_id),
    CONSTRAINT fk_system_map_loop_reservations_attempt_org
        FOREIGN KEY (admission_attempt_id, organisation_id)
        REFERENCES system_map_loop_admission_attempts(id, organisation_id) ON DELETE RESTRICT,
    CONSTRAINT fk_system_map_loop_reservations_loop_org
        FOREIGN KEY (loop_config_id, organisation_id)
        REFERENCES system_map_loop_configs(id, organisation_id) ON DELETE CASCADE,
    CONSTRAINT ck_system_map_loop_reservations_status
        CHECK (status IN ('reserved', 'released', 'consumed')),
    CONSTRAINT ck_system_map_loop_reservations_budget CHECK (reserved_budget_usd > 0),
    CONSTRAINT ck_system_map_loop_reservations_actual_spend
        CHECK (actual_spend_usd IS NULL OR actual_spend_usd >= 0)
);

CREATE INDEX ix_system_map_loop_reservations_active
    ON system_map_loop_reservations(organisation_id, loop_config_id, reserved_at)
    WHERE status = 'reserved';
CREATE INDEX ix_system_map_loop_reservations_month
    ON system_map_loop_reservations(organisation_id, loop_config_id, month_start, status);
CREATE INDEX ix_system_map_loop_reservations_cooldown
    ON system_map_loop_reservations(organisation_id, loop_config_id, cooldown_until DESC);
```

**Reservation state and cooldown contract:** `reserved_at` is the admission timestamp and
`cooldown_until = reserved_at + make_interval(secs => cooldown_seconds)` is written in the
same admission transaction. `cooldown_until` is immutable after insert. The allowed
transitions are `reserved -> consumed` (normal terminalisation), `reserved -> released`
(cancellation or reconciliation before execution), and no transition out of `consumed` or
`released`. `released_at` is required exactly when `status = 'released'`; `actual_spend_usd`
is required for `consumed` and remains null for `released`. Cancellation releases capacity
only: it never deletes the reservation, resets `reserved_at`, or clears `cooldown_until`.

The exact admission cooldown predicate, evaluated while holding the canonical loop lock,
is:

```sql
SELECT 1
FROM system_map_loop_reservations r
WHERE r.organisation_id = :organisation_id
  AND r.loop_config_id = :loop_config_id
  AND r.cooldown_until > CURRENT_TIMESTAMP
ORDER BY r.cooldown_until DESC
LIMIT 1;
```

A matching row rejects the new attempt with `rejection_reason = 'cooldown'`. The predicate
intentionally includes `reserved`, `consumed`, and `released` rows; it excludes nothing by
status. Once `CURRENT_TIMESTAMP >= cooldown_until`, the row no longer suppresses admission.
The cooldown boundary is therefore tied to the last admitted reservation, not to whether
that reservation is still consuming capacity. A partial index may accelerate active
capacity counts, but must not be used for the cooldown predicate because it would omit
released rows before their quiet period ends.

### 3.4 Constraint and Binding Contract

The initial node binding contract is deliberately relational and small. A node may bind to at most one `pipeline_id`, `connector_instance_id`, `owner_team_id`, or `eval_id`; each populated binding uses the existing primary-key foreign key for that resource. In declarative authoring, `pipeline_ref` is the node's top-level pipeline binding reference and `owner_team_ref` is the map's top-level owner-team binding reference. These authoring references resolve to the relational `pipeline_id` and `owner_team_id` projections before save or publish. Node-kind-specific properties belong in `config_json`, including the improvement-loop `config_json.action_pipeline_ref` reference that resolves to the loop projection's `action_pipeline_id`. The service transaction must then validate that each resolved resource belongs to the current organisation and, for team-scoped maps, that the team is permitted by the map's visibility and row-level security scope. `external_ref` remains the only provider-owned identifier. Guardrail and human-approval configurations are not relational System Map bindings in this revision: controls describe those concepts through `control_type` and validated `config_json` only.

#### Deterministic attribution binding schema

Every executable pipeline that can be attributed to a System Map has one explicit row in `system_map_attribution_bindings`:

```sql
CREATE TABLE system_map_attribution_bindings (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organisation_id UUID NOT NULL REFERENCES organisations(id) ON DELETE RESTRICT,
    pipeline_id UUID NOT NULL REFERENCES pipelines(id) ON DELETE RESTRICT,
    trigger_id UUID NULL REFERENCES triggers(id) ON DELETE RESTRICT,
    system_map_id UUID NOT NULL,
    system_map_revision_id UUID NOT NULL,
    node_key VARCHAR(128) NOT NULL,
    binding_kind VARCHAR(20) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_system_map_binding_id_org UNIQUE (id, organisation_id),
    CONSTRAINT fk_system_map_binding_map_org FOREIGN KEY (system_map_id, organisation_id)
        REFERENCES system_maps(id, organisation_id) ON DELETE CASCADE,
    CONSTRAINT fk_system_map_binding_revision_org FOREIGN KEY (system_map_revision_id, organisation_id)
        REFERENCES system_map_revisions(id, organisation_id) ON DELETE RESTRICT,
    CONSTRAINT ck_system_map_binding_kind CHECK (binding_kind IN ('trigger', 'action_pipeline')),
    CONSTRAINT ck_system_map_binding_kind_trigger CHECK (

        (binding_kind = 'trigger' AND trigger_id IS NOT NULL)
        OR (binding_kind = 'action_pipeline' AND trigger_id IS NULL)
    ),
    CONSTRAINT ck_system_map_binding_node_key CHECK (length(node_key) BETWEEN 1 AND 128)
);

CREATE UNIQUE INDEX uq_system_map_binding_trigger
    ON system_map_attribution_bindings(
        organisation_id, pipeline_id, trigger_id, system_map_id, node_key
    )
    WHERE binding_kind = 'trigger' AND trigger_id IS NOT NULL;

CREATE UNIQUE INDEX uq_system_map_binding_action_pipeline
    ON system_map_attribution_bindings(
        organisation_id, pipeline_id, system_map_id, node_key
    )
    WHERE binding_kind = 'action_pipeline' AND trigger_id IS NULL;
```


The two partial unique indexes are deliberately separate. PostgreSQL's nullable-column uniqueness is not used to distinguish action bindings from trigger bindings: an action binding has `trigger_id IS NULL` and is unique on `(organisation_id, pipeline_id, system_map_id, node_key)`, while a trigger binding has a non-null `trigger_id` and is unique on `(organisation_id, pipeline_id, trigger_id, system_map_id, node_key)`. The binding also stores `organisation_id`; the shared resource validator described above checks pipeline, trigger, map, and pinned revision ownership before insert. A duplicate write fails with a unique-violation error and is returned as a deterministic `duplicate_attribution_binding` rejection, not silently replaced. Admission requires exactly one applicable binding: zero matches reject as missing attribution and more than one reject as ambiguous. Query order, map name, node name, and pipeline name are never tie-breakers.



**Binding revision-integrity contract:** `system_map_id`, `system_map_revision_id`, `node_key`, and
`binding_kind` are all required and non-null. The composite revision FK proves that the revision
exists in the same organisation, but the writer must also lock and validate the map/revision
relationship before insert or update:

```sql
SELECT 1
FROM system_map_revisions r
JOIN system_maps m
  ON m.id = r.map_id AND m.organisation_id = r.organisation_id
WHERE r.id = :system_map_revision_id
  AND r.organisation_id = :organisation_id
  AND r.map_id = :system_map_id
  AND r.status = 'published'
  AND m.published_revision_id = r.id
  AND m.deleted_at IS NULL;
```

The same transaction then requires exactly one projected node row:

```sql
SELECT 1
FROM system_map_nodes n
WHERE n.organisation_id = :organisation_id
  AND n.revision_id = :system_map_revision_id
  AND n.node_key = :node_key
  AND n.pipeline_id = :pipeline_id;
```

`binding_kind` must be exactly `trigger` or `action_pipeline`; the existing trigger/null
constraint remains mandatory. A missing map, cross-organisation revision, revision belonging
 to another map, draft or archived revision, revision not selected by the map, missing node,
 duplicate node key, or node bound to another pipeline rejects the write. These checks run on
 every create and update and are also repeated during resolution, so stale or mismatched rows
 fail closed even if a legacy repair or a future migration bypassed a writer. Revisions are
 immutable after publication: no update may change their content, `map_id`, organisation, or
 status in place; rollback creates a new revision. Therefore the binding's revision ID remains
 a stable integrity anchor, and the partial uniqueness indexes above are sufficient for active
 bindings without an `active` flag. If a future implementation permits superseded bindings,
 add `retired_at` and make both indexes `WHERE retired_at IS NULL`; never reuse a binding row
 for a different revision.




- Resolution precedence is explicit: (1) an exact trigger binding for the triggering `pipeline_id` and `trigger_id`; (2) otherwise the organisation-scoped action-pipeline binding for the triggering `pipeline_id` and `binding_kind`; (3) otherwise an explicit `unattributed` declaration supplied by the caller. The dispatcher never falls back to a default map or scans all published maps.
- A trigger binding must identify the pipeline that the trigger directly dispatches. For an improvement loop, the `action_pipeline` binding belongs to the loop configuration's `action_pipeline_id` and `node_key`; it is not inferred from the loop node name, the map's other pipeline nodes, or the signal source. Each loop admission therefore resolves the action pipeline's binding as `binding_kind = action_pipeline` while holding the loop and published-revision locks.
- The binding resolves to one node key in one published revision. If a map has multiple published revisions, only the revision selected by the map's locked `published_revision_id` is eligible. If the selected revision has no matching node, has multiple projected rows for the key, or the node's pipeline binding does not equal the triggering pipeline, admission fails closed with an attribution error.
- A binding to a different organisation, deleted resource, archived revision, inactive pipeline, or out-of-scope team is rejected. An implicit match that could resolve to more than one map, revision, or node is always rejected rather than guessed.

The resulting `system_map_id`, selected `system_map_revision_id`, node ID, and `node_key` are copied into the immutable run snapshot before execution. The binding record is the source of the decision; the snapshot is the source of historical attribution.

`account_id` and `updated_by` use the existing account foreign key. Before inserting or publishing a map or revision, the service verifies that each account is an active member of the current organisation. The account table remains global and receives no organisation column or composite foreign key.

The initial relationship vocabulary targets map nodes only. `owned_by`, `measured_by`, and `capacity_limited_by` are therefore removed from the first implementation: team ownership is represented by `owner_team_id`, metrics are selected by the node's metric configuration, and capacity is represented by `capacity_limit` or a queue node. This avoids relationships whose target entity has no typed row or tenant-safe key.

The DDL and service transaction divide tenant enforcement as follows:

- `system_maps`, revisions, nodes, relationships, loop configurations, attribution bindings, admission attempts, and reservations each store a non-null `organisation_id` and reference `organisations(id)`.
- Composite keys are created only on new tables where both columns are owned by this schema: `(id, organisation_id)` for row identity and `(revision_id, node_key, organisation_id)` for node identity within a revision.
- A revision, published-revision pointer, relationship endpoint, or loop configuration can therefore reference only rows from the same new-schema organisation and revision.
- A pipeline, connector, evaluation, trigger, or team binding uses that existing table's primary-key foreign key. The shared database-backed validator is called by every writer in the same transaction, under the current RLS context, and checks same-organisation and permitted-team scope. Missing, deleted, cross-organisation, or out-of-scope resources fail closed. These are mandatory validation boundaries, not pure relational FK guarantees.
- Account bindings use the existing `account_id` foreign key. The shared membership validation checks active membership in the current organisation for `account_id` and `updated_by`.
- The graph validator still enforces semantic rules such as allowed relationship types, required node properties, and flow acyclicity.

The migration is executable in this order: create the new tables and their organisation-owned composite keys; add ordinary foreign keys to existing resource primary keys; add the shared validator and invoke it from every binding, publication, admission, reconciliation, and administrative repair writer; add the indexes used by its organisation and team validation queries; backfill or publish only after those checks are active; then add attribution columns and terminalization writes. The migration does not alter `accounts`, and it does not require new composite unique keys on teams, pipelines, connectors, triggers, or evaluations. References stored only inside JSON, such as `external_ref` or a connector-specific configuration value, remain application-level because they do not identify a Modulo row.

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
| `improvement_loop` | `target_signal`, `observation_window_seconds`, `config_json.action_pipeline_ref`, `budget_limit_usd`, `max_concurrent_runs`, `cooldown_seconds`, `outcome_metric` | No additional properties in the initial contract | `system_map_loop_configs.action_pipeline_id`, `name`, `node_type` |
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

System Maps enforces strict loop semantics. Static publication validation checks bounds and completeness; live dispatch admission is the only place that decides whether a trigger may create a run.

 **Canonical transaction lock order:** Every admission, cancellation, terminalisation, reconciliation, and publication transaction that touches these resources acquires locks in this total order, and never in another order: (1) `system_maps` row; (2) selected `system_map_revisions` row; (3) `system_map_loop_configs` row; (4) action `pipelines` row; (5) organisation admission/capacity row; (6) binding and projection rows; (7) the `runs` row. When a transaction needs multiple rows at one level, it locks them by ascending UUID. This order is the single deadlock-avoidance rule: callers must acquire all required locks in this sequence, skip absent levels, and release them only at transaction end. No transaction may lock a child row before its parent row or perform a second lock pass in a different order.

- **Admission and reservation transaction sequence:** The dispatcher starts one database transaction and follows the canonical lock order above, then inserts one `system_map_loop_admission_attempts` row with the stable key. It performs attribution, publication, cooldown, concurrency, and budget checks while holding those locks. On a rejected check it sets `run_id = NULL` and the closed `rejection_reason`, commits that attempt, and creates no run. On success it creates the run and snapshot in the same transaction, then inserts `system_map_loop_reservations` with the newly created `run_id` and updates the attempt with that run reference and no rejection reason. The transaction commits the attempt, reservation, run, and snapshot together. A failure rolls back all of them, so a reservation never exists without its run.

- **Idempotency key:** Every loop signal carries a required `admission_key`, stable for the logical trigger occurrence and scoped to `(organisation_id, loop_config_id)`. The admission-attempt table has a unique constraint on that tuple. A repeated delivery returns the existing attempt and, when admitted, its reservation and run decision without consuming another slot, budget, or cooldown. A different key is a new admission attempt.
- **Reservation record:** `system_map_loop_reservations` stores admitted capacity only. Its `run_id` is required because the row is inserted only after run creation; rejected-before-run decisions live in `system_map_loop_admission_attempts` and have a null `run_id`. Reservation status is `reserved`, `released`, or `consumed`. No mutable JSON counter is used.
- **Cooldown:** While holding the loop row lock in the canonical order, admission evaluates the exact `cooldown_until > CURRENT_TIMESTAMP` predicate from §3.3 across every reservation status, including `released`. If any row matches, the dispatcher records an attempt with `run_id = NULL` and reason `cooldown`, then commits without creating a run. Cooldown begins at admission reservation time, not at signal receipt, cancellation, or run completion. Cancellation changes only capacity state (`reserved -> released`); it cannot delete the row or clear its cooldown boundary, so a cancelled run cannot immediately bypass the quiet period.

- **Concurrency:** While holding the canonical map, revision, loop, pipeline, and organisation locks, admission counts `reserved` reservations whose runs are not terminal and applies the lower of the loop, action-pipeline, and organisation limits. If no capacity exists, it records a rejected attempt with no run. A terminalisation transaction follows the same lock order, locks the run row last, marks the reservation `consumed`, and releases the loop slot in the same transaction as the terminal run update. A reservation cancelled before execution follows the same order and is marked `released`; it does not count as active, but its immutable `cooldown_until` continues to suppress admission until expiry.

- **Monthly budget:** Admission computes the UTC calendar-month sum of `reserved_budget_usd` for `reserved` and `consumed` reservations, locks the month-scoped set through the loop row lock, and inserts a reservation only when the sum plus the new reservation is at most `budget_limit_usd`. It records a rejected attempt with reason `budget` when the cap would be exceeded. Terminalisation records actual spend in the reservation or an immutable settlement row; unused reserved budget is released only by an explicit cancellation/reconciliation transaction. Existing organisation and pipeline cost controls remain additional lower bounds and cannot be raised by this loop.
- **Rollback and failures:** If run insertion, snapshot creation, or any later admission write fails, the transaction rolls back the attempt, reservation, snapshot, and run together. If dispatch infrastructure fails after commit, the durable reservation remains visible and a reconciler either dispatches that run or marks the reservation `released` with an auditable reason; it never silently creates a second reservation. A rejected attempt is retained as a decision record and can be retried with the same key without double counting.
- **Publication and dispatch both enforce controls:** publication rejects missing or invalid values and validates the reservation-table configuration; dispatch re-checks the published revision and current counters inside the admission transaction. Database uniqueness and row locks enforce atomic admission and reservation, while the shared resource validator performs same-organisation checks.
- **Run reference integrity:** The reservation's `run_id` is an immutable denormalized reference because the current `runs` table does not expose an organisation-owned composite key for a cross-table FK. Before inserting the reservation, the same transaction locks `runs` by `id`, verifies exactly one non-deleted row has the same `organisation_id`, and rejects a missing or cross-organisation run. Reconciliation repeats that same-org check; it never repairs a mismatch by changing `organisation_id` or `run_id`, and marks the reservation unrecoverable for operator review.
- **Rejection auditability:** every rejected attempt has `status = 'rejected'`, `run_id = NULL`, and one non-null `rejection_reason` from the closed vocabulary in the DDL. Every admitted attempt has `status = 'admitted'`, a non-null `run_id`, and a null reason. The check constraints and transaction sequence make it impossible to persist a rejected-before-run row with a fake run reference.
- **Published-loop dispatch predicate:** the admission transaction follows the canonical lock order and uses `SELECT ... FOR UPDATE` at each required level. It admits only when the locked rows satisfy `system_maps.published_revision_id = system_map_revisions.id`, `system_map_revisions.status = 'published'`, `system_map_loop_configs.revision_id = system_map_revisions.id`, and the action pipeline is active and same-organisation. It must not perform a pre-lock read followed by an unlocked status check. Publication takes the same canonical order, validates that the candidate revision is complete, writes all projections, sets the map pointer, and commits as one transaction. A draft is therefore either still unpublished and ineligible, or fully published before a dispatcher can observe it. If a database constraint cannot enforce the cross-row status predicate, the application transaction lock is the enforcement boundary and any missing, stale, or contradictory row causes fail-closed rejection with `unpublished_revision`.


1. **The delivery flow is acyclic:** Every relationship of type `flow` belongs to the primary delivery flow. The directed subgraph formed by all `flow` edges must be a Directed Acyclic Graph (DAG). Any draft containing a cycle among `flow` edges is rejected by the graph validator at save and publish time.
2. **Improvement loops are modeled as control sidecars:** An `improvement_loop` is never placed as an inline node on a `flow` edge. It sits adjacent to the flow as an autonomous observer. It reads signals via `feeds_back_to` or metric streams, and acts upon capabilities or queues through `controls` or asynchronous trigger actions.
3. **The Seven-Parameter Improvement Loop Contract:** Every `improvement_loop` node must define exactly seven control parameters:
   - **Target Signal (`target_signal`):** The quantifiable operational symptom or defect to monitor (e.g. `failing_test_ratio`, `uncovered_architecture_gaps`, `critical_security_findings`).
   - **Observation Window (`observation_window_seconds`):** The time duration over which signal data is collected before deciding to act (e.g. 3600 seconds, 86400 seconds).
   - **Action Pipeline (`config_json.action_pipeline_ref`):** The authoring reference for the executable Modulo pipeline dispatched to investigate and apply a remediation (e.g. Improve Architecture, Improve Security). It resolves to the relational `action_pipeline_id` binding in the published loop configuration.
   - **Budget Limit (`budget_limit_usd`):** Maximum model and compute spend allocated to this loop per UTC calendar month. Publication requires a positive value; dispatch accounts each admitted run against the remaining loop budget and refuses or defers admission when the cap would be exceeded.
   - **Maximum Concurrent Runs (`max_concurrent_runs`):** Maximum number of active remediation runs admitted by this loop at once. Publication requires an integer of at least 1; dispatch refuses admission above this cap and also applies the lower action-pipeline and organisation concurrency limits.
   - **Cooldown Window (`cooldown_seconds`):** Minimum quiet period required after a remediation run before the loop is permitted to fire again, preventing rapid-fire oscillatory commits. Dispatch enforces this interval from the last admitted run.
   - **Outcome Metric (`outcome_metric`):** The verifiable metric used to confirm whether the remediation succeeded or failed (e.g. `delta_vulnerabilities`, `delta_test_failures`).

---

## 5. Data Contracts, Attribution & Provenance

### 5.1 Snapshot Attribution Freezing (Beta implementation contract)
A pipeline run must not guess its position in the organizational delivery system at runtime. At run initialization, the trigger engine uses the explicit System Map binding carried by the trigger or action pipeline, as defined by the organisation-scoped binding tuple in §3.4. It does not search all published maps or select a row by query order. Because an organisation may publish multiple maps and a map may contain multiple nodes bound to the same pipeline, an admission without one explicit binding is ambiguous and is rejected with an attribution error. There is no implicit default-map fallback. A run that is intentionally outside System Maps may be created only when the caller marks it `unattributed`; it is then shown as unknown provenance and is never assigned to a map node after the fact.

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

- `system_map_id`, `system_map_revision_id`, and `system_map_node_id` are nullable only for an explicitly `unattributed` run. `system_map_node_key` is nullable with them. A mapped run stores all four fields: the parent map UUID, the published revision UUID active at trigger time, the projected node UUID, and the node key (1-128 characters).
- The trigger or action-pipeline binding supplies the target `system_map_id` and `system_map_node_key` (or the exact node UUID). Validation resolves exactly one published revision and one node in that map. Zero matches, multiple matches, a binding to a different organisation, or a binding that is not permitted by scope rejects run admission. Query order is never a tie-breaker.
- The trigger engine writes the block once, when it creates the run snapshot, before execution begins. It does not resolve the current map again during execution or terminalization.
- The block is immutable for the lifetime of the run. Editing, republishing, or archiving a System Map cannot change an existing run's attribution.
- A future relational projection may also store the same values on `Run`, but `PipelineSnapshot` is the source of truth for the frozen attribution contract. The implementation must not read a live `system_map_nodes` row to reconstruct historical attribution.
- Validation requires every map-bound trigger and action pipeline to carry this explicit binding. Validation also requires an explicit `unattributed` choice for intentionally unbound execution. The provenance record retains that choice, so an unmatched or rejected attribution is not silently converted into a node match.

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
| `unknown` | Modulo cannot classify the value reliably because it is unavailable, contradictory, stale beyond its validity window, or missing required attribution. | **Unknown** - source unavailable, conflicting, or unattributed. |

`unknown` is a required displayed provenance badge. Evidence rows and aggregate cards render it exactly as **Unknown**, never as a blank, zero, `Unobserved`, or an inferred node value. `observed`, `reported`, and `derived` render as **Observed**, **Reported**, and **Derived** respectively. Each badge tooltip names the source and calculation path when available, and says why the value is unknown when it is not.

Receipt and content have separate provenance. Modulo may **observe** that a self-report envelope arrived, including its sender, timestamp, signature status, and raw receipt metadata. The claim inside that envelope remains **reported** unless Modulo independently records the underlying event. The UI therefore renders separate badges rather than upgrading the claim: for example, `Envelope observed` beside `Claim reported`, with `Derived` shown only for calculations based on observed facts.

`Unobserved` is not a provenance value. It is a separate coverage/data-state label that means the node or external capability has no active telemetry observation for the selected period. It renders as **Unobserved** beside the provenance badge when applicable. A missing metric therefore renders **Unknown** for provenance plus **Unobserved** for coverage, while an explicitly unattributed run renders **Unknown** plus **Unattributed**. Neither state is coerced to zero or silently treated as observed.

### 5.4 Honest Reporting of Missing Data
When a pipeline or execution path is not mapped to a System Map, or when an external step lacks telemetry:
- Modulo never coerces missing metrics to zero.
- Unmapped runs are grouped under an explicit `Unattributed Flow` category and carry an **Unknown** provenance badge.
- Nodes lacking active telemetry instrumentation display an **Unobserved** coverage/data-state label, independently of their provenance badge.
- Dashboard totals separate observed throughput, reported throughput, derived aggregates, and unknown or unattributed throughput.
- Evidence and aggregate views always show a provenance badge, including **Unknown** when the source is missing.

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
System Maps is a first-class operational view integrated into the Dashboard.

1. **Navigation Entry:** System Maps is positioned in the primary navigation under the **BUILD** group as a tab/section within the existing **Dashboard** page at `/dashboard#system-maps`. The standalone `/system-maps` route is not used.
2. **Default Home View:** When an organization has a published System Map marked as default (`is_default = TRUE`), the Dashboard (`/dashboard`) automatically displays the **System Overview** tab as the active section instead of the generic runs list.
3. **Empty / First-Run State:** If no System Map has been published, the Dashboard's System Overview tab renders a guided onboarding layout offering four options:
   - **Discover my system** — automatically scan existing pipelines, connectors, triggers, folders, and improvement-loop pipelines to produce a draft System Map for review, correction, and publication.
   - Select an opinionated system template (e.g. "Agentic PR Delivery with Autonomous Review").
   - Import an existing System Map specification via YAML or JSON.
   - Continue to the raw Pipelines management view.

### 7.2 "Discover My System" Automatic Topology Discovery

The **Discover my system** flow scans the organization's existing Modulo resources to generate an initial draft System Map:

1. **Scan Sources:**
   - **Pipelines** — all active pipelines with their node types, triggers, and connector bindings.
   - **Connectors** — configured connector instances and their operational status.
   - **Triggers** — webhook, cron, polling, and agent-signal triggers with their target pipelines.
   - **Folders** — pipeline folder hierarchy for logical grouping.
   - **Improvement-loop pipelines** — pipelines tagged or configured as improvement loops (via `improvement_loop` node type or metadata).

2. **Inference Rules:**
   - Each pipeline becomes a `pipeline` node.
   - Each trigger targeting a pipeline creates a `flow` edge from a synthetic trigger source to the pipeline node.
   - Connector instances bound to pipeline nodes become `external_capability` nodes with `flow` edges from the pipeline.
   - Folders become `system_boundary` nodes with `contains` relationships to their member pipelines.
   - Pipelines identified as improvement loops (by metadata or naming convention) become `improvement_loop` nodes with `feeds_back_to` edges to the capability they monitor.

3. **User Review & Correction:**
   - The generated draft opens in the visual editor with all inferred nodes and edges highlighted as *suggested*.
   - Users can add, remove, re-type, or reposition nodes; edit configurations; and validate the graph.
   - The graph validator enforces DAG delivery flow, control integrity, and binding scope before publish.

4. **Publication:**
   - After review, the user publishes the draft as revision 1 of a new System Map.
   - The map can be marked as default to appear in the Dashboard System Overview tab.

### 7.3 Visual Language & Canvas Layout
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
  - Provenance badges (`Observed`, `Derived`, `Reported`, or required `Unknown`) on each listed data item, plus `Unobserved` when the coverage/data state has no active telemetry.

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
Before a draft revision can transition to `published`, it must pass these strict graph validation checks:
1. **Connectivity:** Every node (except explicit `system_boundary` nodes) must have at least one incoming or outgoing relationship.
2. **Acyclic Delivery Flow:** The directed graph of all `flow` relationships must contain zero cycles.
3. **Control Integrity:** A `controls` relationship must originate from a `control` or `improvement_loop` node.
4. **Loop Completeness and Safety:** Every `improvement_loop` node must carry a valid configuration in `system_map_loop_configs` with all seven parameters specified: target signal, observation window, action pipeline, positive monthly budget, `max_concurrent_runs >= 1`, cooldown, and outcome metric. Publication rejects missing, non-positive, or non-integer safety values. The loop dispatcher must enforce the published cooldown, budget cap, and concurrency cap again at every admission.
5. **Relational Binding Resolution and Scope:** Every declared `pipeline_id`, `connector_instance_id`, `owner_team_id`, `eval_id`, and improvement-loop `action_pipeline_id` must resolve unambiguously to an existing, active resource in the current organisation and, where applicable, the permitted team scope. UUIDs and exact case-insensitive names are accepted; zero matches, deleted resources, cross-organisation resources, or out-of-scope team resources reject publication. More than one name match is an ambiguity error that requires a UUID. A missing optional binding is allowed only when that node kind marks the binding optional; a missing required binding rejects publication.
6. **Graph and Projection Consistency:** Every resolved binding must be written to the published relational projection, and publication must fail atomically if any resolution, scope check, or projection write fails.

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
System Maps is planned as a Beta extension to the declarative `modulo apply` engine. The current engine accepts the `api_version: modulo.dev/v1` envelope with an `entities:` mapping, but it does not currently register a `system_maps` entity kind. The example below uses that current envelope to illustrate the planned Beta payload; it is syntactically valid YAML, but it is illustrative/future only until the `system_maps` schema kind and apply handlers are added. The current engine rejects this entity set, so do not present the example as a currently applicable configuration.

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
            max_concurrent_runs: 1
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
| `pipeline_ref` | `pipeline_id` | Pipeline UUID or exact pipeline name | Resolve an active, non-deleted pipeline within the current organisation and permitted team scope. |
| `connector_instance_ref` | `connector_instance_id` | Connector UUID or exact connector name | Resolve an existing, non-deleted connector instance within the current organisation and permitted team scope; zero or ambiguous matches fail. |
| `eval_ref` | `eval_id` | Evaluation UUID or exact evaluation name | Resolve an existing evaluation within the current organisation and permitted team scope; zero or ambiguous matches fail. |
| `config_json.action_pipeline_ref` | `action_pipeline_id` | Pipeline UUID or exact pipeline name | Resolve an active, non-deleted action pipeline within the current organisation and permitted team scope. |

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
  - Read-only System Overview **tab within the Dashboard** displaying published system topography.
  - Node Inspector showing aggregated throughput, latency, and failure metrics derived from `run_daily_facts`.
  - **Empty-state onboarding with "Discover my system" flow** — scans pipelines, connectors, triggers, folders, and improvement-loop pipelines to generate a draft System Map for review and publication.
- **Exit Criteria:** A predefined system map renders live operational metrics without modifying running pipelines; a new organization can discover, review, and publish their first System Map from the Dashboard.

### Beta 2: Visual Authoring, Revision Lifecycle & Draft/Publish
- **Scope:**
  - Interactive canvas editor supporting drag-and-drop node placement and edge connection.
  - Node configuration dialogs for all seven node types.
  - Graph validation engine enforcing DAG delivery flow and control constraints.
  - Draft, publish, diff, and rollback revision workflow.
  - `modulo apply` declarative YAML support for System Maps.
  - **"Discover my system" re-scan action** — re-run the automatic topology discovery on an existing draft to incorporate newly created pipelines, connectors, or folders.
- **Exit Criteria:** Platform engineers can create, validate, publish, and update custom system maps via UI and CLI; automatic discovery can be re-run on drafts to stay current.

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
| **Runaway Improvement Loops** | An autonomous improvement loop spins out of control, generating dozens of conflicting PRs. | Atomic admission reservations under database row locks, a unique loop-plus-admission idempotency key, and transactionally reconciled cooldown, monthly budget, and concurrency reservations. |
| **Data Sync Drift with Issue Trackers** | Work-item keys displayed in the Evidence Drawer drift out of sync with external trackers. | Work items are non-authoritative drill-down references; Modulo never writes back ticket status to external trackers. |

### 11.3 Open Architecture & Product Decisions

#### Decision 1: Default Landing Page Behavior — RESOLVED
- **Resolution:** System Maps is integrated as a **System Overview tab within the existing Dashboard page** (`/dashboard#system-maps`), not a separate route. When a default map exists, the Dashboard activates the System Overview tab by default. Individual users can toggle their preferred Dashboard default tab (`System Overview` vs `Runs List`) in account preferences.
- **Trade-offs:**
  - Integrating into Dashboard preserves the existing navigation structure and avoids a standalone route.
  - Defaulting to System Overview on first login establishes product identity; per-user tab preference preserves operator efficiency.
- **Reasoning:** This avoids fragmenting the primary navigation while still making System Maps the organizational default view.

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

#### Decision 3: Automatic Topology Discovery — RESOLVED
- **Resolution:** Automatic topology discovery is **adopted** as the "Discover my system" flow. It scans existing pipelines, connectors, triggers, folders, and improvement-loop pipelines to produce a draft System Map. The user reviews, corrects, and publishes the draft. Available in the empty-state onboarding (Beta 1) and as a re-scan action on drafts (Beta 2).
- **Trade-offs:**
  - Reduces initial setup friction by generating a meaningful starting point from real resources.
  - Inferred edges are marked as *suggested* and require human validation before publish, preserving conceptual clarity.
  - Re-scan on drafts keeps maps current as the organization adds pipelines and connectors.
- **Reasoning:** Explicit authoring alone creates a cold-start barrier. Guided discovery with mandatory review balances friction reduction with architectural intent.
