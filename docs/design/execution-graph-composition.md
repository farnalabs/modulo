# Design: Pipeline Composition Ergonomics (Execution Graph) — FAR-402

**Status:** Draft · **T-shirt:** L · **Type:** Design doc + execution-graph spec
**plan-review-iterate:** CAP-HIT at iteration 5 (v1 had ~12 criticals; v5 reduced to 2 deploy/desync criticals with mandated mitigations — see Appendix B)
**Scope:** Routing/branching · fan-out/fan-in · conditional transitions · HITL-as-step · node-level tool/context scoping · per-node/per-edge failure & retry · versioning & diffing
**Non-goals:** agent runtime internals (we *dispatch*, we don't run agents — Principle 7), connector authoring, model-backend selection. This design owns the *graph*, not what is inside a node.

---

## 1. Context

Modulo owns the orchestration layer — the visual, composable pipeline of atomic AI agents, i.e. the **execution graph**. Today the graph has powerful but *hidden* capabilities: nodes stored as JSON, edges as a table, LangGraph `StateGraph` execution, native fan-out, conditional edges (JMESPath), HITL as an edge gate, pipeline-level retry only, no per-node tool scoping, and snapshot versioning. The ergonomics gap is not missing features — it is that these capabilities are buried in edge strings, blanket policies, and compile-time synthetic nodes. This design makes them **first-class, visual, and composable**.

## 2. Current state (what already exists — do not reinvent)

- Graph storage: nodes as JSON on `Pipeline.graph_nodes_json`; edges as a real table `PipelineEdge` (`edge_type ∈ {normal,reject,conditional}`, `condition_expression` JMESPath, `hitl_gate_config`).
- Execution: LangGraph `StateGraph`, compiled per-snapshot, cached by `(pipeline_id, snapshot_id)`. Fan-out is native (multiple normal edges → parallel superstep). Fan-in is implicit via a state reducer (list concat; `run_context` dict last-write-wins — a known hazard).
- Routing: conditional edges (JMESPath), reject edges (HITL kickback), LLM routing (`routing_mode="llm"` + `routing_label`), and a `loop` edge type that exists *only* in compiled `graph_json` (not in the API schema — a discrepancy).
- HITL: an **edge gate**, realized as a synthetic `hitl_gate_{src}_{tgt}` node at compile time. Separate `manual` node type = human *produces* output.
- Retry: pipeline-level `retry_policy` (run-level re-dispatch, max 5, jittered backoff) + transient node-level requeue. **No per-edge retry.** Retries blanket-disabled if any node has `idempotent=false`.
- Scoping: no per-node tool allow-list. Tool access is inherited from the referenced `Agent` (`connector_type_refs`, `model_backend_id`, `required_environment_capabilities`) + sandbox capability scoping + per-node `env_vars`/`context_files`.
- Versioning: full snapshot versioning, `diff_snapshots` (structural per-field), `rollback_to_snapshot` (new snapshot, HITL-removal guard). Frontend is **Vue Flow**.

## 3. Foundations

### F1 — Port-addressed typed state (the unifying lever), v5-corrected
Each node declares `inputs`/`outputs` **ports**; edges map `source_port → target_port`. **Ports are ADDITIVE metadata over the existing flat `run_context`/`artifact` dict — they do NOT rename or namespace flat-state keys.** The port→state-key adapter maps a port to the *same* flat-state key the node already uses, so existing conditional-edge JMESPath (reading `state.foo`) resolves identically. This:
- Extends **Schema Seams (Principle 1)** from agent I/O to inter-node flow.
- Removes the last-write-wins fan-in hazard by enforcing: a non-Join target port accepts **at most one** incoming edge (compile-time rule); fan-in is ONLY via a Join node, which collects into an array.
- **Migration:** existing (port-less) nodes fall back to raw keys; a lazy backfill synthesizes a default `out`/`in` port per node at load/first-compile (adds only missing required ports; never clobbers custom ports; validates inferred key vs actual emitted artifact key). Zero breaking change; golden test asserts existing flat-dict pipelines still compile + route identically.

### F2 — Canonical node palette & edge taxonomy (reconcile discrepancies)
- Node types: `agent` · `sandbox_agent` · `manual` (human output) · `hitl` (gate — new, §D) · `router` (§A) · `join` (§B) · `composite` (expand-only, unchanged). All snake_case string values (no new Python `Enum` class — consolidate existing string-Literal/frozenset usages).
- Edge types: `normal` · `reject` · `conditional` · `loop` (promote `loop` to the API schema + `VALID_EDGE_TYPES` + `graph_validator`; it is already compiled in `graph_cache.py`). Consolidate the edge-type set to ONE canonical source (VALID_EDGE_TYPES + API regex `pipelines.py:824` + graph_validator) and a single `EDGE_TYPE → validator` registry.
- **`connector` is NOT a node type** — it is an internal engine resolution (a node carrying `connector_binding`). The engine tuple (`graph_cache.py:364`) accepts it; the API Literal (`pipelines.py:548`) correctly omits it (internal-only, never API-authored). **Do NOT add `connector` to the composite sub-node allow-list** (`graph_validator/__init__.py:366`) — that would wrongly permit it as a composite sub-node → runtime crash. Assert at the API deserialization boundary (not the shared validator) that no graph submitted via API carries `type:"connector"`. No migration.

### F3 — Engine stays LangGraph
The design is an **authoring + schema + validation layer**. Compilation to `StateGraph` is unchanged; we add node/edge *kinds* that compile to the existing `make_*_node_fn` / router / loop machinery. No rewrite of the executor.

## 4. Scope-area designs

### A. Routing / branching → **Router node**
A visual decision node holding ordered rules `{guard, target_port}`, first-match-wins (array order). Explicit `default` rule. Guards can be **data-driven** (upstream output port) or **state-driven** (a `run_context`/control flag). LLM routing becomes a Router *mode* (`mode:"classifier"`) returning a label matched to outgoing ports.
- **Lowers to existing machinery:** reuses the single shared JMESPath evaluator (see §10 R1) and the synthetic-node/read-only-router idiom. No second evaluator.
- **Default-rule enforcement:** at COMPILE time for **new** Router nodes and **new** conditional edges. **Existing** conditional-edge graphs lacking `default_target` are EXEMPTED (backward-compat; legacy semantics kept) — see Open Item O5 for the discriminator needed to implement this cleanly.
- LLM output validated against declared target ports; undefined output → `default` or hard error. Malformed JMESPath guard → typed compile error.

### B. Fan-out / fan-in → **scatter + Join node**
- **Scatter (fan-out):** a node declares `fan_out: {split, strategy}` → N parallel branches. Compiles to **N distinct graph nodes** (each a clone with unique `node_id = parent+index`) so audit/claim/feedback keys stay unique. Reuses existing native fan-out. Hard ceiling + batched scatter required (no unbounded materialization).
- **Join (fan-in):** a `join` node declares `collect: [{node, port}]` + `aggregate: concat|merge_by_key|map` (merge_by_key needs explicit key source; map over collected list to shaped output). Compiles to a normal convergence node reusing `cost_controller.finalize._merge_stored_outputs`. Reads ONLY declared upstream ports (safe, no reducer collision). `custom_function` aggregate DEFERRED or sandboxed like `sandbox_agent`.
- **Correlation:** scatter→Join correlation token; empty-collection → typed empty; Join over FAILED (non-empty, non-timeout) scatter branch → per policy (default collect-and-proceed, failed branch marked with per-child `{status: succeeded|failed|timed_out}`); Join deadline + partial-result policy defined. Child execution teardown tied to run cancellation AND Join completion AND scatter-level failure (idempotent via `run+node+index` dedupe key).

### C. Conditional transitions → folded into Router + visual builder
No separate "conditional edge" concept — conditionality is a Router rule. The visual builder (field/op/value → JMESPath) compiles to JMESPath under the hood. `conditional` edge_type formally **deprecated** in favor of Router (one branching primitive); existing conditional edges remain compile-supported. LLM routing and `loop` remain distinct mechanisms (acknowledged, not unified).

### D. HITL as a first-class step → **`hitl` node type**
Promote HITL from an edge property to a **draggable node**, while keeping the synthetic-gate compile path for backward compat. A `hitl` node carries: `mode: approve_reject|collect_input`, `form_schema_ref`, `reject_target`, `correction_target`, `claim_team_id`, `claim_expiry_min`, `human_only`, `eval_before_interrupt`.
- **Compile-equivalence:** `hitl` node compiles to EXACTLY the existing synthetic-gate path (`edge.hitl_gate_config`); add a compile-equivalence test (new node == legacy edge gate). Verify the synthetic-gate implementation supports each HITL field (the engine uses `required_team_id`, `overdue_threshold_minutes`, `eval_condition`, `condition`, `human_only`, `gate_id` — map fields 1:1 before claiming equivalence).
- **Migration:** auto-convert existing edge-gate HITL to `hitl` nodes during backfill; edge-gate authoring deprecated but compile-supported; non-mappable edge-gate conditions surfaced as a migration **WARNING** stating the node will proceed *ungated* (not silently dropped). `manual` node RETAINED as a non-gating human-output step; `hitl` is the gating variant.

### E. Node-level tool/context scoping → **capability_scope block** (gated on FAR-408 merged+stable)
A node declares a least-privilege contract: `allowed_connectors: [instance-id|type]`, `allowed_tools: [runtime tool]`, `context_scope: [run_context keys]`.
- **Default = UNRESTRICTED** (preserves today's behavior); narrow only when explicitly set. The allow-list is **populated from existing graph connector usage** (not the empty set) so no silent break.
- **Fetch-time scoping:** ConnectorHub **fetches only** `allowed_connectors` from the secret backend (deny-by-default when scope set) — NOT a post-decrypt filter. `allowed_tools` wires into the existing `check_tool_scope` chokepoint. `context_scope` defined explicitly (allowlist of `run_context` keys) or dropped if redundant.
- Scope violation → logged, metric-emitting (`scope.violation{graph_id,node_id,connector}`) typed error; run fails. Port payloads must be plain-serializable; connector/secret **objects** are never valid port payload types — only opaque connector IDs in state.

### F. Failure & retry → per-node, per-edge, compensation (gated on FAR-410 merged+stable)
- **Per-node retry:** `node.retry = {max_attempts, backoff, on:[timeout,error,stall]}` overrides pipeline default; reuses FAR-295 `_graph_is_idempotent` harness.
- **Per-edge retry (DEFINED):** retries the transition by re-executing the **source** node; mutually exclusive with compensation/`on_failure_target` per failure; ordering vs node-retry specified (edge-retry wraps node-retry; fail-closed for `idempotent:false`).
- **Idempotency:** adopt FAR-410 per-node `idempotent: bool` + `idempotency_key`; engine enforces via dedupe; non-idempotent nodes default `retry=1`. **Two idempotency keys** (see §10 R7): node-scoped `run+node+index` (within-run); run-level `idempotency_key` (FAR-410, stable across re-runs).
- **Compensation edges (`on_failure` → compensation node):** DAG try/catch. Trigger = watched node reaches terminal-failure; forward-only; **acyclic enforced at COMPILE TIME** (typed error rejects cyclic compensation graphs, including nested/sub-pipeline references); failure → terminal `COMPENSATION_FAILED` (see §10 status sites), logged, non-retried. Success = run continues (recorded flag for observability).
- Snapshot immutability preserved: retries re-execute against the same pinned snapshot. Integrates FAR-410 `UNKNOWN` + a non-terminal `UNKNOWN` state (see §10).

### G. Versioning & diffing → live history + semantic diff + channels
- **Live edit history:** each run captures the graph **VERSION** at run-start and executes against that pinned version; the live-edit chain is a separate object. Naming reconciled: per-run snapshot vs per-definition edit-version chain. Every applied edit creates an immutable prior version (rollback story).
- **Semantic diff + impact:** scoped to structural + port-signature deltas with a **deterministic propagation rule** (oracle, unit-testable) — "which downstream nodes a port change affects."
- **Release channels:** build on `pipeline_snapshot_versioning.py`; every run tagged `release_channel`; promotion/rollback thresholds tied to per-channel metrics. Channel resolution hook in `TriggerEngine` defined before any channel is written.

## 5. Data-model summary (concrete deltas)

| Entity | Change |
|---|---|
| `PipelineEdge` | +`source_port`, `target_port`, `edge_type` adds `loop`+`router`, +`retry` JSON, +`on_failure_target` |
| `PipelineGraphNode` | +`inputs`/`outputs` ports, +`fan_out`, +`retry`, +`capability_scope`, new `node_type: hitl`/`router`/`join` |
| `Pipeline` | +live edit-version chain; `retry_policy` becomes *default* (nodes override) |
| `PipelineSnapshot` | already pins everything; diff gains semantic/impact layer |
| `Run` (status) | +`COMPENSATION_FAILED` + `ROUTER_NO_MATCH` terminal statuses; `UNKNOWN` adopted from FAR-410 (non-terminal) — see §10 for the FULL status-site update set |

## 6. Frontend (Vue Flow) changes
- New palette nodes: **Router**, **Join**, **HITL** (distinct visuals; `manual` stays).
- **Ports** rendered as typed handles; edges connect port→port with type-check (schema seam enforced visually).
- **Condition builder** modal for Router rules (field/op/value → JMESPath), codegen round-trip tested.
- **Retry/scope** panels on node inspector; **compensation edge** drawn as a dashed red edge.
- **Version timeline** UI: live-edit history + semantic diff view with impact badges.
- Frontend node-type/status enums must be updated alongside backend (see §10 status sites + node-type copies).

## 7. Migration & backward compatibility
- Default ports (`out`/`in`) synthesized lazily for existing edges/nodes (zero-migration for current graphs).
- `hitl` node compiles to the existing synthetic-gate path (old edge-gate HITL kept).
- `loop` added to API schema; existing compiled-only loops now authorable.
- `connector` phantom type removed from consideration (it is internal — see F2).
- **Run-status changes require updating the FULL set of status-definition sites** (see §10) — this is the highest-risk migration item.

## 8. Coordination with REST connector epic (FAR-408–413)
These tickets (parent design **FAR-401**) build a generic REST connector — a *thing a node uses*, complementary to this execution-graph design (the *graph*). Verdict: safe to deliver as-is. Three non-blocking coordination touchpoints (cross-referenced on FAR-402 in Linear):
1. **Idempotency** — FAR-410 delivers node-config `idempotent: bool` + `idempotency_key`. This design **adopts** that primitive (§F) rather than re-specifying it.
2. **Retry is two layers** — FAR-410 = connector-internal HTTP retry; this design = graph-level node/edge retry + compensation. They compose; documented as such.
3. **Fan-out is two mechanisms** — FAR-411 = connector-internal iteration fan-out; this design = graph-level parallel-branch scatter + join. Shared budget-vs-cardinality reconciliation principle.
- Area E (`capability_scope`) **depends on** FAR-408's ConnectorHub wiring (filter point = run-start cred decryption) — gated.
- FAR-402's novel contributions (Router, Join, HITL node, per-edge retry, compensation edges, port-addressed state, live-edit history + semantic diff) remain uncovered by those tickets.

## 9. Phasing (L-sized — 6 independently shippable increments, each its own ADR + tests + docs)
- **P1 (Ergonomics, low risk, reuses existing machinery):** F2 taxonomy reconciliation + A Router + C conditionals + D hitl node.
- **P2 (Correctness RFC):** F1 port-addressed typing/validation (migration).
- **P3:** B scatter + Join.
- **P4:** E capability_scope (gated FAR-408).
- **P5:** F per-node/per-edge retry + compensation (gated FAR-410).
- **P6:** G live-edit history + semantic diff + release channels.
- **Fork/merge deferred to v1+.**

## 10. Run-status change protocol (CRITICAL — read before any status addition)
Adding `COMPENSATION_FAILED`, `ROUTER_NO_MATCH`, or adopting `UNKNOWN` requires updating the **FULL enumerated set** of status-definition sites (the codebase has a "single source of truth" leak — all must stay in sync, per `run.py:35`):
1. Migration `ck_runs_status` expected-def (`0110_schema_pipeline_runtime.py:4170`) AND create (`:4485`) strings.
2. ORM model `CheckConstraint` (`run.py:95-99`) — used by SQLite/dev/`create_all`; must match migration.
3. `RUN_STATUS_WHITELIST` (`crud/run.py:63-76`) — the **actual write gate** in `update_run_status`/`transition_run`; without it the write raises `ValueError` before DB.
4. `TERMINAL_STATUSES`, `ACTIVE_RUN_STATUSES`, `ONGOING_ACTIVE_STATUSES` frozensets (`run.py:41-47`); `UNKNOWN` must be in `ACTIVE_RUN_STATUSES` (non-terminal).
5. `_FAILURE_BUCKET_STATUSES` / `_FAILURE_REASON_STATUSES` (`run.py:88-89`) — include `compensation_failed`.
6. `classify.py` explicit bucket (notify, not fail-safe).
7. Inline SQL literals: `_UPDATE_STATUS_FENCED_SQL` / `_TRANSITION_SQL` (`crud/run.py:1568/1656`) — set `completed_at` for new terminal statuses.
8. `lifecycle_map/reconcile.py:87` `_ADVANCING_STATUSES` + `lifecycle_map/advancement.py:93` `_ADVANCING_TERMINAL_STATUSES`.
9. Frontend: `runStatuses.ts:8` + `VariantBatchCompareView.spec.ts:64` + `constants/filters.ts` + `utils/runUtils.ts` + `stores/analytics.ts` + `lib/api/schema.ts` (`AnalyticsStatus`).
10. `PROBE_TERMINAL_STATUSES` (`probe.py:77`).
11. Any watcher branching on `status=='failed'` (Merge Queue / Deploy Agent / recovery) — grep + verify no conflation.
12. Semgrep `raw-status-complete` rule (`classify.py:119-122`) expectations.
**Recommendation:** refactor to a single canonical, **attribute-driven** run-status registry (each status = metadata object with `is_terminal`, `sets_completed_at`, `advances_journey`, `is_countable_failure`, `is_active` flags) imported by all sites — eliminates the duplication leak. Mandated as a prerequisite refactor; until done, the migration plan lists all 12 sites.

## 11. Design decisions (resolved)

The following were open questions; resolutions are locked into the design.

1. **Port model depth → Full port-addressed state (F1).** Ports are additive metadata over the flat `run_context`/`artifact` dict (no rename); a non-Join target port accepts at most one incoming edge. The "ports only where fan-in needs them" hybrid was rejected — it creates two code paths (port-addressed vs flat) and a migration boundary. Full ports is consistent; the lazy backfill is zero-break.
2. **Router vs edge-conditions → Keep both; Router is primary, `conditional` deprecated-but-compile-supported.** Existing conditional edges ship and compile; forcing a migration to Router nodes is a breaking change. Router lowers to the same conditional compile path, so they are functionally equivalent. New authoring uses Router (visual builder); `conditional` edge_type is deprecated for authoring only.
3. **HITL node vs edge gate → `hitl` node is the primary authoring surface; edge-gate HITL deprecated-but-compile-supported (auto-convert on backfill).** Edge-gate HITL is invisible on the canvas; `hitl` makes it first-class. Existing edge-gate graphs auto-convert; edge-gate authoring is deprecated.
4. **Live edit history → Reuse the snapshot machinery with a `draft` flag (not a new table).** Snapshots already pin the full graph + bindings and provide diff/rollback. A `draft` boolean + `version_kind` (edit vs run) reuses `pipeline_snapshot_versioning.py` and the immutable-chain semantics, avoiding duplication of the snapshot schema and diff/rollback logic.
5. **Fork/merge → Deferred to v1+.** "Git for pipelines" is a large, independent feature (branch/edit/merge/conflict-resolution) with its own UX and risk; not required for the alpha composition win. P1–P6 cover ergonomics without it.

---

## Appendix A — Principles alignment
- **Schema Seams (1):** ports extend typed contracts to inter-node flow.
- **Immutability of runs:** retries re-execute against the same pinned snapshot.
- **Audit as first-class:** new statuses/compensation paths emit audit + OTel.
- **Deterministic gates:** Router rules + Join aggregates are deterministic; LLM routing validated.
- **HITL where it matters (5):** `hitl` node is first-class, team-scoped, `human_only` supported.
- **Secret hygiene (6):** ports carry only opaque connector IDs; secrets never in state.
- **We dispatch, we don't run (7):** graph owns orchestration; connectors/agents own tool-use.
- **Correction never rewrites history (8):** compensation is a new forward path, not an in-place edit.

## Appendix B — plan-review-iterate outcome (CAP-HIT, iteration 5)
Ran 7 antagonistic lenses × 5 iterations (30 lens runs). v1 had ~12 criticals; v5 reduced to 2 criticals + majors. Remaining items to resolve at implementation (tracked, not silently dropped):

**Critical (deploy-sequencing — must be mitigated before shipping new run statuses):**
- **O1 — CHECK-constraint / migration ordering.** New-status writes are rejected by `ck_runs_status` until the migration lands; a rolling deploy overlaps new backend + old migration → rejected writes. Mitigation: make the constraint additive tolerating the overlap window (`NOT VALID` + later `VALIDATE`, or insert new values as a backward-compatible superset applied before app code emits them, with app code feature-flagged).
- **O2 — Frontend/backend status desync.** Backend can emit a new status before `runStatuses.ts` deploys → UI crash (frozen enum `switch`). Mitigation: frontend `status ?? "unknown"` fallback; deploy frontend before/with backend; or feature-flag the status emission.

**Major (resolve during implementation):**
- **O3 — Idempotency double-execution race.** Status-filtered dedupe is a read-then-insert race. Mitigation: UNIQUE constraint on `idempotency_key` + transactional INSERT-or-return; dedup query filters `status NOT IN TERMINAL_STATUSES`.
- **O4 — Default-rule new/existing exemption not implementable at compile** (compile has no new-vs-existing notion). Mitigation: graph `schema_version` + one-time backfill, or adopt the existing cutover-date pattern (`_is_pre_existing`).
- **O5 — Port surjectivity carve-out silent misroute.** Surjectivity (every consumed key produced by a declared port) must apply to ALL consumers, not only declared-port nodes. Mitigation: extend the check to legacy JMESPath consumers.
- **O6 — Single-registry refactor** (§10) must be mandated, not just recommended, to prevent future status-site drift.
- **O7 — UNKNOWN drain fallback.** If `dispatcher_reconcile` cannot resolve a stuck `UNKNOWN`, add a terminal fallback (analogous to `age_terminalized`) so it does not hold a concurrency slot forever.

**Disposition:** design is approved-in-substance; the above are implementation-phase risks with known mitigations. No design contradiction remains.

## 12. Report
Return: branch name, commit SHA, PR URL/number, and confirm the file exists at `docs/design/execution-graph-composition.md`.
