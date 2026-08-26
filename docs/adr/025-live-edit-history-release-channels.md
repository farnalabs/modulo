# ADR 025 — Live-edit history + semantic diff + release channels (FAR-402 P6)

**Date:** 2026-08-25
**Status:** Accepted

---

## Context

Modulo's orchestration layer owns the visual, composable pipeline of atomic AI
agents — the **execution graph**. The graph already has full snapshot
versioning (`PipelineSnapshot` + `diff_snapshots` + `rollback_to_snapshot`),
but versioning today is run-centric: a snapshot is a *run-start freeze*. Two
gaps remain from the execution-graph design (`docs/design/execution-graph-composition.md`
§4 G, the FAR-402 P6 phase):

1. **No live-edit history.** Every time someone edits the pipeline graph, the
   prior definition is silently overwritten. There is no per-definition
   version chain to inspect, diff, or roll back to — only the run-frozen
   snapshots that happen to exist.
2. **No semantic diff + impact.** `diff_snapshots` reports *structural*
   differences (nodes/edges added/removed/modified by field), but it does not
   surface **port-signature** deltas or answer "which downstream nodes does
   this change break?" — the deterministic impact oracle the design mandates.
3. **No release channels.** Runs always pin the live graph. There is no way to
   bind a trigger (or a definition version) to a `stable`/`canary` channel, no
   per-channel latest-version resolution, and no promotion/rollback contract.

## Decision

Build all three on the **existing snapshot machinery** — do NOT create a new
table (design decision 4).

### Live-edit history

Add four additive columns to `pipeline_snapshots` (migration `0138_*`):
`version_kind` (`edit` | `run`), `created_kind` (`initial` | `edit` |
`rollback` | `run`), `draft` (bool), and `channel` (`none` | `stable` |
`canary`). Live-edit saves go through `create_snapshot_edit`, which creates a
new snapshot row tagged `version_kind='edit'`. Each save leaves the prior row
immutable, so rollback is a pointer swap to a prior snapshot
(`rollback_to_snapshot`, now tagging the new row `created_kind='rollback'`).
Run-start callers keep their default and produce `version_kind='run'`, so a run
executing mid-edit is unaffected (it pins its `run` snapshot).

### Semantic diff + impact

`diff_snapshots` now additionally surfaces **port-signature** deltas (input /
output port names + `schema_ref`s and edge `source_port` / `target_port`) and a
`semantic` block:

- `port_changes` — the union of per-node and per-edge port deltas.
- `impacted_nodes` — the deterministic propagation oracle:
  `compute_port_change_impact(graph, changed_ports)` BFS-es from the node owning
  each changed port along outgoing edges and returns every transitively
  downstream node.
- `breaking_changes` — `check_port_change_breaking(graph_old, graph_new,
  changed_ports)`: a save-time check that flags a `block` when a port change
  would drop an edge's referenced data and a `warning` when a referenced
  port's schema-ref changes.

### Release channels

`modulo/core/release_channels.py` owns the contract (no metrics pipeline, no
promotion dashboard — flagged as follow-up):

- `VALID_RELEASE_CHANNELS = {"none", "stable", "canary"}` and
  `ROUTABLE_RELEASE_CHANNELS = {"stable", "canary"}`.
- `ReleaseChannelThresholds` (`rollback_threshold_error_rate_pct`,
  `rollback_min_observed_runs`, `promotion_min_observed_runs`) as pure data.
- `should_rollback(channel_metrics, thresholds) -> bool` — deterministic
  rollback oracle (enough observed runs AND error rate meets/exceeds threshold).
- `resolve_channel_binding(trigger_config)` — maps a trigger's `release_channel`
  binding to a valid channel, defaulting to `none`.
- `TriggerEngine.resolve_snapshot_id_for_trigger(...)` — the channel-resolution
  hook: a `stable`/`canary`-bound trigger resolves to the latest snapshot for
  that channel (`resolve_snapshot_for_channel`); an unbound trigger returns
  `None` and the caller pins the live graph (current behaviour).

## Consequences

- **Reuses** snapshot schema + diff/rollback logic; the live-edit chain and
  run snapshots are distinguished by discriminator columns, not a parallel
  store.
- **Backward compatible.** All four columns have `server_default`, so legacy
  rows are run-kind, non-draft, no-channel snapshots; existing run-start
  callers are unchanged. Port helpers are tolerant of port-less (pre-P2)
  graphs — no breaking behaviour.
- **Deterministic, unit-testable oracles.** `compute_port_change_impact` and
  `should_rollback` are pure functions with no DB or engine dependency.
- **Out of scope (follow-up):** the promotion/rollback dashboard, the metrics
  pipeline feeding `ChannelMetrics`, and the actual channel-family promotion
  reducer. This ADR fixes the data model + decision contract; the operational
  dashboard is a later phase.
- **Migration surface.** The snapshot REST responses gain
  `version_kind`/`created_kind`/`draft`/`channel`; a `POST
  /pipelines/{pipeline_id}/snapshots` save-edit endpoint is added.
