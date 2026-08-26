---
id: feat-hitl
prd: N/A
adr:
  - docs/adr/025-execution-graph-router-hitl-nodes.md
code:
  - backend/src/modulo/api/routes/hitl.py
  - backend/src/modulo/core/hitl_manager/
  - backend/src/modulo/core/pipeline_engine/graph_cache.py
  - backend/src/modulo/core/pipeline_engine/node_runner.py
  - backend/src/modulo/db/models/hitl_claim.py
  - frontend/src/views/SettingsHitlReviewView.vue
unit-tests:
  - backend/tests/unit/pipeline_engine/test_router_hitl_nodes.py
  - backend/tests/unit/core/test_graph_cache_hitl.py
  - backend/tests/unit/api/test_hitl_resilience.py
  - backend/tests/unit/api/test_rate_limit_hitl_review.py
  - backend/tests/unit/core/hitl_manager/test_hitl_jwt.py
  - backend/tests/unit/hitl_manager/test_hitl_manager.py
  - backend/tests/unit/hitl_manager/test_claim_expiry_job.py
  - backend/tests/unit/hitl_manager/test_overdue_warning.py
bdd:
  - backend/tests/bdd/features/hitl/
  - backend/tests/bdd/steps/test_hitl.py
  - backend/tests/bdd/steps/test_team_hitl_gate.py
  - backend/tests/bdd/steps/test_alpha_hitl.py
  - backend/tests/bdd/steps/test_conditional_hitl.py
depends-on:
  - feat-pipelines
status: covered
---

# HITL Gates & Review

Human-in-the-loop approval for pipeline runs. A run pauses at a gate — authored either
as a legacy edge-level `hitl_gate_config` or, since FAR-402 P1 (ADR-025 F2-D), as a
first-class draggable `hitl` node that compiles to the same synthetic-gate path. A
human claims the gate atomically, inspects context, and approves, approves-with-
modification, rejects (routing to a reject target or terminating the run), or delivers
manual output. Claims are tracked on `hitl_claims` with expiry/overdue sweeps, an
audit trail, and the review surface under `/settings/hitl-review`.

## Behaviours

- [x] The `hitl` node type is authorable in the pipeline API schema and compiles via
      `graph_cache.build_graph_from_json`; the node produces output like a normal node
      and its `hitl_config` is injected onto each outgoing edge, flowing through the
      identical legacy synthetic-gate path (`make_hitl_gate_fn`)
- [x] Compile-equivalence: a `hitl` node produces the same compiled graph shape as the
      legacy edge-gate HITL (`test_hitl_node_compiles_like_edge_gate`)
- [x] A reviewer claims a gate atomically (`SELECT ... FOR UPDATE` on `hitl_claims`);
      not-found / already-claimed / not-a-team-member resolve to 404 / 409 / 403 and the
      run moves to `claimed`
- [x] Claim tokens are opaque random strings (alpha) or short-lived JWTs (v1) with
      expiry; invalid or expired tokens are rejected (403 / 410)
- [x] Approve resumes the run past the gate with `action: approved` and optional
      reviewer notes
- [x] Approve-with-modification injects the reviewer-supplied modified output for
      downstream nodes and logs an `hitl.output_modified` audit event
- [x] Reject routes the run to the gate's `reject_target` (or terminates it), carrying
      the rejection reason in the resume payload
- [x] Deliver-manual lets a reviewer supply the output directly at the gate (validated,
      non-empty object) and resumes the run
- [x] Pending gates are listable at run and organisation scope; the review surface
      (`/settings/hitl-review`) is backed by manifest-documented `hitl-review-*`
      elements
- [x] Claim-expiry and overdue-warning jobs sweep stale claims, escalate overdue gates,
      and dispatch notifications without aborting unrelated orgs on failure
- [x] HITL review endpoints are operation-permission-gated (`hitl.claim` /
      `hitl.approve` / `hitl.reject` / `hitl.deliver_manual`) and per-destination
      rate-limited (dedicated `HITL_RULE` 20/min)
- [x] `human_only` gates and team-scoped gates (`required_team_id`) are covered by BDD
      (`human_only_gate.feature`, `test_team_hitl_gate.py`)

## Known Gaps

- **Edge-gate HITL graphs remain supported, but not auto-converted** — backfill of
  legacy edge-gate HITL to first-class `hitl` nodes is out of scope (ADR-025 follow-up).

## QA History

- 2026-08-26: **improve-architecture (product-map walk)** — entry added for the
  registered `feat-hitl` so the feature has a feature-graph node and is discoverable by
  Remy's `search_documentation` indexer. Behaviours verified against `routes/hitl.py`,
  `core/hitl_manager/`, `node_runner.make_hitl_gate_fn`, the HITL BDD suite, and the
  unit suites (`test_graph_cache_hitl.py`, `test_hitl_resilience.py`,
  `test_rate_limit_hitl_review.py`, `test_hitl_jwt.py`, claim-expiry / overdue-warning
  jobs). Status: covered.