# Plan: guard against silent HITL-gate removal

**Status:** v19 – adapted to fast-follow ADR-017 (`docs/adr/017-centralized-authorization.md`, v9, declared converged). **This plan and ADR-017 were developed independently and converged on the same enforcement primitive** (`is_privileged` kwarg on `replace_pipeline_graph()`/`rollback_to_snapshot()`, ADR-017 §"Service-layer backstop") **and the same kill-switch shape** (dedicated per-org boolean column, live per-request read). Verified against the actual in-flight implementation (PR #464 `dist/authz-kill-switch`, open): the general kill switch is real – `organisations.authz_enforce`, a request-scoped ContextVar, `assert_org_role(..., kill_switch_eligible=bool)` in `auth/permissions.py`, and a tenancy-bounded admin endpoint. **`kill_switch_eligible=False` is the literal, already-coded mechanism for non-liftable carve-outs** (used today for org deletion), and ADR-017 names HITL as exactly this kind of carve-out. **This resolves the kill-switch-ownership question this plan previously left open: HITL gets no switch of its own – it's always-on, via the existing carve-out mechanism, not a new one.** This version drops the entire dedicated `hitl_guard_enforce` column/migration/admin-endpoint design (v14-v18) as redundant, and sequences delivery **after** `task-authz-centralized-enforcement` (XL, `phase-authz`), reusing its permission registry, its live-role-check helper, and its kill-switch infrastructure rather than duplicating them. Everything else – the four-field weakening definition, the correlation-key/presence-signal fix, the clone torn-read fix, the MCP exclusion, the Notifier fix – carries forward unchanged; this plan remains the concrete spec ADR-017's own text asks for ("link or fold into ADR before implementation").
**Date:** 2026-08-01
**Audience:** Founders and product leadership / implementation reference
**Related:** `docs/adr/017-centralized-authorization.md` (ADR-017 – this plan implements its "Service-layer backstop" section).

## Problem

HITL gates (`hitl_gate_config` on a pipeline edge, PRD §5/§7.x/§8.8) are the product's core safety control. The gate configuration itself is not protected – it can be silently weakened via edit, rollback, or cloning. `AuditEvent` records changes only after the fact.

## Sequencing (new this iteration)

**This plan ships after `task-authz-centralized-enforcement` (XL), not in parallel with it.** Concretely, this plan depends on that task having already delivered:
- `auth/permissions.py`'s `PERMISSIONS` registry, `assert_org_role()`, and the `kill_switch_eligible` parameter (confirmed live in PR #464).
- `organisations.authz_enforce` (the column) and its migration.
- A centralized live-role-read helper (ADR-017 names this `resolve_role_from_membership` in `auth/dependencies.py`) – this plan reuses it rather than introducing its own `_get_org_role` hoist, which earlier versions of this plan (v9-v18) proposed independently before ADR-017's equivalent existed.

**Duplication accepted, sequencing preferred over parallel delivery**: even though this creates some overlap in review/merge cost (both plans touch permission-checking infrastructure), building the HITL-specific guard on top of already-merged, already-tested general infrastructure is simpler and less risky than trying to land both simultaneously. If `task-authz-centralized-enforcement` changes shape before it merges, this plan's few points of contact with it (below) are the only things that need re-checking.

## Design (v19)

### 1. Define "weakening" precisely (unchanged since v15 – stable across five consecutive prior iterations)

Field-level, on an edge with a non-null `hitl_gate_config`:
- `human_only: true → false`.
- `required_team_id` changed to `null` or to any different team ID.
- `condition` changed at all – regardless of `human_only` (verified: `node_runner.py:296-393`'s `_hitl_gate` evaluates `condition`/`eval_condition` before `human_only` is ever consulted).
- `eval_condition` changed at all – same reasoning.

`claim_expiry_minutes` is deliberately NOT a weakening-capable field: a shorter expiry is stricter, not weaker. On expiry the claim is reset and the run returns to `awaiting_human` (`expiry_job.py`) – it never releases the gate or auto-approves the run, so a decrease cannot reduce the HITL control's effect (review finding on §1; verified against `expiry_job.py` / `pipeline_execution.py` resume semantics).

Structural: an edge that carried a non-null `hitl_gate_config` no longer carries an equivalent one, correlated by `(source_node_id, target_node_id, edge_type)` – the **server-derived topology tuple, never the client-supplied edge `id`** (closes a bypass found in iteration 17: correlating by client-supplied `id` lets a client submit a "new" id for the same topological edge and defeat the "preserve existing value" protection). Edge creation with no prior row is never weakening. The old edge's correlation key being entirely absent from the new edge set is treated identically to a config-downgrade on a surviving edge. Audit/denial messages name edges by this structural key, not DB `id`.

For `rollback_to_snapshot`: a missing/`None` field in a historical snapshot is fail-closed (treated as weakening); only the audit reason code differs.

### 2. Correlation key (unchanged since v2)

`(source_node_id, target_node_id, edge_type)` – the same key used throughout this plan, including presence-detection in §3 item 6.

### 3. Enforcement

A shared diff primitive, `apply_gated_edge_diff(session, old_edges, new_edges, is_privileged) -> DiffResult`, lives in its own module.

1. **`replace_pipeline_graph()`** – guard inside the function, under the existing `with_for_update()` row lock. 6 real call expressions: `pipelines.py:716`, `:800`, `:1763`, `mcp_server.py:677`, `onboarding.py:439`, `:532` (creation-only).
2. **`rollback_to_snapshot()`** – its own guard, same pattern.
3. **`clone_pipeline()`** – drops the primitive, one batched audit event. **Torn-read protection**: instead of holding a lock for the clone's entire duration (a liveness risk found in iteration 16) or a broken isolation-level override (found unworkable in iteration 17), the clone's initial reads are split into their own short, separate transaction: (a) a brief transaction takes `with_for_update(read=True)` (`FOR SHARE`) on the source `Pipeline` row, reads `graph_nodes_json` and `PipelineEdge` rows into plain Python data structures, and commits immediately, releasing the lock within one short round-trip; (b) the existing, slower clone work (constructing the new `Pipeline`, copying nodes/edges/snapshots) runs in its own transaction using only the plain-data snapshot from (a), with no further lock dependency. A concurrent `replace_pipeline_graph()`'s `FOR UPDATE` on the same row can proceed as soon as (a) commits.
4. **Static CI/semgrep check** – weakening-capable = `replace_pipeline_graph()`, `rollback_to_snapshot()`; creation-only allowlisted = `clone_pipeline()`, `templates.py`, `library.py`, `workflow_import_export`, demo seeding; plus a rule against duplicate delete/insert logic outside the primitive's call graph. Fixture files under `.semgrep/tests/` scoped only to this plan's new rule(s); `semgrep --test` wired into CI for those rule IDs only – no retroactive coverage of this codebase's pre-existing rules.
5. **Authorization and MCP exclusion – no kill switch (redesigned this iteration)**:
   `replace_pipeline_graph()`/`rollback_to_snapshot()` take an explicit `caller_type: Literal["rest", "mcp"]` parameter. When `caller_type == "mcp"`, `is_privileged` is hardcoded `False` **with no DB query attempted at all** – this is the entire MCP exclusion mechanism, and there is no other code path by which an MCP caller can be privileged. The MCP call site (`mcp_server.py:677`) passes `caller_type="mcp"` as a literal; a CI test (a `.semgrep/` rule, following this codebase's existing `pattern-either`/`metavariable-regex` idiom) asserts this argument is a literal, not a variable, at that specific call site.
   For `caller_type == "rest"`: the function queries the caller's live org role – via ADR-017's centralized `resolve_role_from_membership()` helper, not a duplicate helper this plan introduces – immediately after the row lock is acquired and before the graph mutation. **This check is always enforced, with no kill switch of any kind** (neither a dedicated HITL column nor ADR-017's general `authz_enforce`): per ADR-017 Decision 3, HITL weakening is one of the explicit non-liftable carve-outs (alongside org deletion), so this service-layer backstop simply never consults `authz_enforce` – it doesn't need `kill_switch_eligible=False` plumbed through, because this check doesn't route through `assert_org_role()`/`require_permission()` at all; it's a direct, unconditional comparison at the point of mutation, which is the strongest form of "non-liftable." **On a DB error during this query: fail-closed immediately, no retry** (iteration 17 found that retrying inside the same transaction after a DB error hits Postgres's aborted-transaction state, `25P02`, masking the real error – since acquiring the row lock itself already required a successful DB round-trip moments earlier, a further immediate failure here is more likely a real problem than a transient blip). Distinct reason code `role-check-db-error`; detection via a structured ERROR-level log line plus a counter increment (independent of the DB), with `Notifier.dispatch_event()` as best-effort secondary.
   **Route-layer permission, separately**: whatever REST endpoint fronts a weakening-capable graph edit should also carry an ordinary `require_permission("pipeline.graph.update")`-style check from ADR-017's registry for baseline access control (already delivered by `task-authz-centralized-enforcement`) – this is unrelated to, and doesn't substitute for, the service-layer backstop above, which exists precisely because route-layer checks can be missed at a call site or bypassed by an internal caller. The two are complementary: route-layer for defense-in-depth breadth, service-layer for the one property (HITL-gate integrity) that must never depend on getting every call site's route-layer check right.
6. **Missing-key semantics, correlation key clarified (iteration-17 finding, unchanged this iteration)**: the edge dict passed from the route layer into `replace_pipeline_graph()` carries `hitl_gate_config_present: bool` (from `"hitl_gate_config" in edge.model_fields_set`, checked before `model_dump`) alongside `hitl_gate_config: dict | None`. Correlation for the "preserve existing value" lookup uses the same server-derived `(source_node_id, target_node_id, edge_type)` tuple as §1/§2 – never the client-supplied edge `id`. For an edge whose topology key matches a pre-existing row: `hitl_gate_config_present=False` means preserve the existing stored value; `hitl_gate_config_present=True` means use the provided value verbatim, including explicit `null` as genuine removal. For an edge with no matching prior topology key (genuinely new): the presence flag just determines its initial value; nothing to preserve.
7. **`MutableDict.as_mutable(JSON)`** – defense-in-depth. `old_edges` passed into `apply_gated_edge_diff` MUST be a `copy.deepcopy` taken before any subsequent write on that session.
8. **Denial UX**: name the specific edge(s) by structural correlation key, not DB `id`.
9. **Shared audit-payload builder** recording `caller_type`, with a schema-parity test.

### 3.6 Composite-template path – deferred, with a trip-wire (unchanged)

Confirmed by direct code inspection: no write path exists. A regression test asserts zero HITL references in `core/composite_engine/`.

### 4. Break-glass – descoped to a separate follow-up plan (unchanged since v13). No mechanism ships in this plan.

Four independent complete redesigns across iterations 9-12 were each found broken on review. A dedicated `break-glass-admin-recovery-plan.md`, run through its own unhurried `plan-review-iterate` cycle, should start from `admin_update_user`/`admin_deactivate_user` as the real mutation points, account for the SCIM bypass, and specify a concrete alert delivery mechanism independent of `Notifier.dispatch_event()`'s per-org subscriber model. Full history in the Appendix. **Note**: this plan's removal of its own kill switch (§3 item 5) makes break-glass slightly less relevant for this specific control than it was under v14-v18's design – there's no HITL-specific switch to get stuck in the wrong state – but break-glass for *account*-level lockout (an org's only admin can't authenticate at all) remains a separate, real, unaddressed problem, unaffected by this change.

### 5. Audit and alerting

- Dedicated `AuditEvent` via `append_audit_event()`, same transaction as graph persistence.
- Denied attempts audited: `hitl_gate_removal_denied`, reason-coded (insufficient-role / role-changed-reauth-required / role-check-db-error / correlation-key-mismatch / legacy-snapshot-ambiguous / mcp-weakening-not-permitted).
- Counters for allowed/blocked, tagged by `weakening_type` and reason code.
- **Notifier silent-loss bug fixed (found in iteration 16, unrelated to ADR-017)**: `_dispatch_inline()`'s `if not endpoints: return []` early return (`core/notifier/__init__.py:282-285`), which previously made in-app `Notification` creation unreachable whenever an org had zero webhook subscribers, is removed – webhook dispatch (a no-op zero-iteration loop when `endpoints` is empty) and in-app notification creation become two independent, always-executed steps. This is a small, general fix, not HITL-specific, but was found while building this plan's alerting and should ship with it (or earlier, as a standalone one-line fix, if that's faster – flagged in Recommendation #3 below).
- New event types require explicit `event_mapper.py` `_EVENT_CONFIG` registration with `scope: "admin"`.

### 6. Frontend

Inline confirmation, server-reject-and-explain naming affected edges. The frontend must send the `hitl_gate_config` key explicitly (with its real current value, or explicit `null` for a genuine removal) whenever the user views/touches that edge's gate config; omitting the key for untouched edges is safe by design given §3 item 6.

### 7. Testing

- Unit tests for the diff primitive, covering all four weakening-capable fields.
- Presence-signal-survives-to-write test, using the topology correlation key (not `id`).
- New-edge-omitted-key test.
- Deep-copy invariant test.
- Topology-bypass test.
- `condition`/`eval_condition` weakening test on a `human_only: false` edge.
- MCP always-denied test: runtime test (MCP-authenticated weakening attempt denied regardless of MCP scope) plus a `.semgrep/` rule asserting the `caller_type` argument at the MCP call site is the literal `"mcp"`, not a variable.
- Diff-before-privilege-check-is-applied ordering test.
- Guard-runs-before-delete test for both guarded functions, under the row lock.
- **Live-role-check-under-the-lock test**: assert the role query executes only after the row lock is held. **Fail-closed-no-retry test**: simulate a DB error on this query, assert immediate denial with reason code `role-check-db-error`, with no second query attempt.
- **Non-liftable-regardless-of-authz_enforce test (new this iteration)**: with the org's `authz_enforce` column set to `false` (the general kill switch off), assert a weakening attempt by a non-admin is still denied – proving the service-layer backstop genuinely never consults that flag, closing the loop on the "carve-out" design decision.
- Concurrent-graph-replace race test, using a named, no-op-by-default testing hook (e.g. `_on_lock_acquired: Callable[[], Awaitable[None]] | None = None`) rather than a fragile call-order-based `AsyncSession.execute` monkeypatch.
- Clone split-transaction test: using the same testing-hook pattern, pause between step (a)'s commit and step (b)'s start; run a concurrent `replace_pipeline_graph()` against the same source pipeline during that window and assert it is **not blocked** (liveness). Separately, a torn-read correctness test: pause *during* step (a) (before its commit), attempt a concurrent write, assert it blocks until (a) commits, then assert (b) sees a consistent, non-torn snapshot.
- Role-check-db-error detection test: structured log line + counter increment fire; Notifier call attempted but not depended on.
- `caller_type` derivation test (REST vs. MCP); MCP-literal spoofing negative test.
- Static CI PR-simulation plus enumeration test plus the no-duplicate-delete-logic rule test.
- Cross-path consistency test across all guarded paths' audit-event schema.
- Composite-engine trip-wire test.
- Notification-created-with-zero-webhook-endpoints test.
- BDD: rollback-denied, clone-creates-with-audit.
- Empty-old-edge-set no-op test.
- Acceptance: all tests above pass; weakening-denial audit-event count matches attempted-weakening count in the concurrency test; single-admin no-op and denied-then-allowed as automated tests; latency benchmark against a 200+ edge pipeline with the pass/fail budget set and reviewed by a human other than the implementer before merge.

### 8. Rollout

- **No migration of this plan's own** – the `organisations.authz_enforce` column (and its migration) is `task-authz-centralized-enforcement`'s deliverable, not this plan's. This plan adds no schema.
- Rollout note (unchanged): `replace_pipeline_graph_endpoint` currently has no authorization check at all – net-new for weakening transitions. An audit query for non-admin accounts with historical gate-touching edits is run before flipping enforcement on; findings treated as an incident.
- MCP tooling dependent on MCP-driven gate weakening will see denials post-ship (structurally guaranteed via the literal `caller_type="mcp"` at the MCP call site) – check dogfooded pipelines.
- Ships directly to enforcing, with **no rollback lever specific to this control** (no kill switch, by design – see §3 item 5). If the guard itself misbehaves in production, rollback is a code revert, not a flag flip. This is a deliberate tradeoff: an HITL-integrity control that can be silently disabled (even for good operational reasons) is weaker than one that can't be, and ADR-017 already made this call for the general case.
- PRD update at `docs/prd.md` §8.8 alongside §5/§7.x, and a cross-reference added to ADR-017 pointing at this plan as the concrete "Service-layer backstop" spec.
- Known limitation, stated plainly: no per-account break-glass path exists at ship time (§4). This is unrelated to the kill-switch removal in this version – it was already true in v14-v18.

## Effort estimate

**S** (down from M in v18) – removing the dedicated kill switch eliminates its migration, admin endpoint, and roughly a third of the test list. What remains is genuinely additive to already-delivered infrastructure: the weakening definition, the diff primitive, the correlation-key/presence-signal fix, the clone split-transaction fix, and the MCP `caller_type` mechanism.

## Recommendation for Duncan – decisions needed before implementation

1. **Ship this after `task-authz-centralized-enforcement` merges**, reusing its permission registry and live-role-check helper rather than duplicating them.
2. **Confirm HITL weakening should be a fully non-liftable control** (no kill switch at all, matching ADR-017's own stated carve-out intent) – this is now this plan's position, a change from v14-v18's dedicated-switch design.
3. **The Notifier zero-webhook-subscribers bug (§5) is a small, general, unrelated fix** – worth asking whether it should ship standalone/sooner rather than bundled with this plan, since it affects every `scope: "admin"` event type, not just HITL's.
4. **Confirm the no-per-account-break-glass limitation is acceptable** (§4) – unrelated to this iteration's changes, still an open item.
5. **Commission the break-glass follow-up** as a separate, unhurried plan-review-iterate cycle, independent of this plan's timeline.
6. **MCP cannot weaken gates at all in v1**: structurally guaranteed via a literal `caller_type="mcp"` at the MCP call site – confirm this is the intended shape.

## Open questions still unresolved

1. Frontend: confirmation dialog differs for admins vs. operators?
2. Snapshot/graph-cache staleness – needs subsystem-owner input.
3. Break-glass / emergency admin-recovery design – entire follow-up plan.
4. Whether to backfill/disambiguate historical snapshots' ambiguous `hitl_gate_config` fields.
5. **Resolved at implementation**: no dedicated route-layer permission key for weakening is registered. The weakening-capable endpoints carry the operator baseline (`pipeline.graph.update`) at the route layer for defense-in-depth breadth, and the service-layer backstop (operator+ privileged under the row lock) is the load-bearing control. A dedicated admin-only route gate was tried during implementation but removed on review: it blocked the operator's primary graph-edit path (`PATCH /pipelines/{id}/graph` is the frontend's save endpoint) while equivalent weakening stayed reachable via `update_pipeline`/`convert_to_agent`/`revert_to_manual` – asymmetric, not uniformly admin-only. This plan's §3 item 5 route-layer wording (baseline access control) is the operative spec.

---

## Appendix: iteration history (preserved for record)

### Iteration 1 (v1 → v2): 4 critical, ~14 major – fictional enforcement point; real choke point is `replace_pipeline_graph()`.
### Iteration 2 (v2 → v3): 1 critical (twice), ~11 major – `rollback_to_snapshot()`/composite-templates bypass the single choke point entirely.
### Iteration 3 (v3 → v4): ~8 critical, ~16 major – ORM runtime backstop structurally incapable of its job.
### Iteration 4 (v4 → v5): 5 critical, ~13 major – crash bug in a naive rollback route; audit-only rollout leaves the vulnerability exploitable indefinitely.
### Iteration 5 (v5 → v6): 2 critical, ~11 major – decoupling break-glass from enforcement creates an unbounded lockout.
### Iteration 6 (v6 → v7): 2 critical, ~14 major – "break-glass takes the same lock" not implementable across a process boundary.
### Iteration 7 (v7 → v8): 4 critical, ~10 major – bypass parameter repeats a codebase-precedented failure mode; stale-privilege token window.
### Iteration 8 (v8 → v9): 4 critical, ~11 major – v8's MCP two-tier weakening model broken. Response: removed MCP weakening authority entirely.
### Iteration 9 (v9 → v10): 5 critical, ~13 major – v9's capability token unworkable given single-container deployment. Response: vault-stored admin account.
### Iteration 10 (v10 → v11): 2 critical, ~10 major – v10's vault-account reused the exact account `AGENTS.md` already authorizes routine agent access to.
### Iteration 11 (v11 → v12): 7 critical, ~8 major – v11's break-glass account could not authenticate at all.
### Iteration 12 (v12 → v13): 5 critical, ~9 major – v12's break-glass protection guarded the wrong resource. Fourth consecutive broken break-glass redesign. **Response: descope break-glass entirely; ship the core guard on its own (v13).**
### Iteration 13 (v13 → v14): 2 critical, 8 major – first fresh review of the split, core-guard-only plan. Weakening definition incomplete; layering contradiction; unspecified kill switch; topology-bypass ambiguity; frontend-round-trip SPOF; unbuilt semgrep test infra.
### Iteration 14 (v14 → v15): 3 critical, 6 major – two of v14's own fixes were themselves broken; DB-error alert shared fate with the DB failure; env-var kill switch contradicted ADR-017 precedent; no bounded retry; MutableDict aliasing risk; PRD task named wrong section.
### Iteration 15 (v15 → v16): 4 critical, 7 major – the kill switch's RuntimeConfigStore redesign was itself fundamentally broken (no cross-process propagation; ADR-017 citation backwards). MCP privilege value at the shared enforcement point unspecified.
### Iteration 16 (v16 → v17): 3 critical, 3 major – presence-detection signal discarded before the write; `FOR SHARE` lock held for clone's entire duration (liveness risk); role+kill-switch resolved before the row lock (TOCTOU).
### Iteration 17 (v17 → v18): 3 critical, 1 major – the `REPEATABLE READ` override for clone couldn't execute against the real call path; retrying the role/kill-switch check inside the same transaction hit Postgres's aborted-transaction state; "preserve existing value" correlation was ambiguous between client-supplied `id` and the server-derived topology key.
### Post-v18 (v18 → v19): **not a plan-review-iterate finding – a scope discovery.** Comparing this plan against `docs/adr/017-centralized-authorization.md` (a separately-developed, independently-converged ADR covering the same functions) found: (a) ADR-017's "Service-layer backstop" section specifies the identical `is_privileged` guard on `replace_pipeline_graph()`/`rollback_to_snapshot()` this plan has been designing for 17 iterations, with `task-authz-centralized-enforcement` (XL) already tracked to deliver it; (b) ADR-017's kill switch (`organisations.authz_enforce`, per-org dedicated column) is architecturally identical to this plan's own kill-switch design, and is already partially implemented (PR #464, `dist/authz-kill-switch`, open); (c) ADR-017 explicitly names HITL weakening as a **non-liftable carve-out** from that kill switch (`kill_switch_eligible=False`, a mechanism already coded), directly resolving the kill-switch-ownership contradiction this plan had left as an open question. **Response (v19): the entire dedicated `hitl_guard_enforce` column/migration/admin-endpoint design (present since v14) is removed. HITL weakening becomes fully non-liftable – no switch at all – matching ADR-017's stated intent. This plan is resequenced to ship after `task-authz-centralized-enforcement`, reusing its permission registry and live-role-check helper. Everything not related to the kill switch (weakening definition, correlation-key fix, clone torn-read fix, MCP exclusion, Notifier fix) carries forward unchanged.**

All Minor findings from all seventeen review iterations were discarded per the skill's process rules and are not reproduced here.
