---
id: feat-hitl
prd: N/A
adr: []
code:
  - backend/src/modulo/api/routes/hitl.py
  - backend/src/modulo/core/hitl_manager/__init__.py
  - backend/src/modulo/core/hitl_manager/expiry_job.py
  - backend/src/modulo/core/hitl_manager/overdue_warning.py
  - backend/src/modulo/core/hitl_manager/sweep_alarm.py
  - backend/src/modulo/core/run_context/autonomy.py
  - backend/src/modulo/db/crud/hitl_gate_config.py
  - backend/src/modulo/db/models/hitl_claim.py
  - frontend/src/views/SettingsHitlReviewView.vue
unit-tests:
  - backend/tests/unit/hitl_manager/test_hitl_manager.py
  - backend/tests/unit/hitl_manager/test_output_delivery_audit.py
  - backend/tests/unit/hitl_manager/test_overdue_warning.py
  - backend/tests/unit/hitl_manager/test_claim_expiry_job.py
  - backend/tests/unit/hitl_manager/test_sweep_alarm.py
  - backend/tests/unit/hitl_manager/test_client_type_audit.py
  - backend/tests/unit/core/hitl_manager/test_hitl_jwt.py
  - backend/tests/unit/api/test_hitl_resilience.py
  - backend/tests/unit/api/test_rate_limit_hitl_review.py
  - backend/tests/unit/pipeline_engine/test_node_runner_hitl.py
  - frontend/src/__tests__/SettingsHitlReviewView.spec.ts
bdd:
  - backend/tests/bdd/features/hitl/claim.feature
  - backend/tests/bdd/features/hitl/approve.feature
  - backend/tests/bdd/features/hitl/reject.feature
  - backend/tests/bdd/features/hitl/deliver_manual.feature
  - backend/tests/bdd/features/hitl/manual_node.feature
  - backend/tests/bdd/features/hitl/feedback_handler.feature
  - backend/tests/bdd/features/teams/team_hitl_gate.feature
  - backend/tests/bdd/features/evals/conditional_hitl.feature
  - backend/tests/bdd/features/dashboard/hitl_trends.feature
  - backend/tests/bdd/steps/test_hitl.py
  - backend/tests/bdd/steps/test_conditional_hitl.py
  - backend/tests/bdd/steps/test_team_hitl_gate.py
depends-on:
  - feat-audit
  - feat-teams
status: covered
---

# Human-in-the-loop (HITL) Gates & Review

A pipeline reaching a HITL gate pauses and records a `HitlClaim`; a human
reviewer claims, approves, rejects, or delivers manual output at the gate, and
the run resumes through the decided path. Gates surface on per-run and
org-wide pending queues (`/settings/hitl-review`), claims carry short-lived
tokens so reviews are exclusive and safe, and every decision is audited.
`human_only` and team-scoped gates constrain who — or what (MCP/automation) —
may decide.

## Behaviours

- [x] A run reaching a HITL gate edge creates a `HitlClaim` and pauses in
      `awaiting_human`
- [x] Pending-gate queues: `GET /api/v1/runs/{run_id}/hitl/pending` (per run)
      and `GET /api/v1/hitl/pending` (org-wide), gated by the `hitl.list`
      permission. The org-wide queue joins `runs` and lists only undecided
      gates whose run is in `awaiting_human`, `claimed`, or `hitl_parked` status
      — undecided
      gates on terminal runs are data rot, not pending work, and are excluded
      (FAR-612); the MCP `list_pending_hitl` tool applies the same
      actionable-status filter. The review UI renders a gate held by another
      session as read-only (claimed by \<user\> at \<time\>) and shows
      approve/reject only to the session holding that gate's claim token
- [x] Claim is atomic — `claim()` issues a short-lived (15-minute) JWT
      `claim_token` scoped to run + gate + client; expired/invalid claim
      tokens are rejected. The three claim conflicts each return 409 with a
      distinct machine-readable problem type the frontend discriminates on by
      `type` rather than English prose (FAR-645): an already-claimed gate →
      `urn:problem:modulo:hitl_gate_already_claimed`, an already-decided gate
      → `urn:problem:modulo:hitl_gate_already_decided` (previously a generic
      500), and a run that is not claimable →
      `urn:problem:modulo:hitl_run_not_awaiting`. The run-status guard uses
      `HITL_CLAIMABLE_RUN_STATUSES` (`awaiting_human` or `hitl_parked` — a
      parked run's gate stays OPEN AND CLAIMABLE, park != decide, FAR-604 D2)
      and is enforced atomically with the claim UPDATE via a `runs` EXISTS
      predicate, so a run that goes terminal between the pre-check and the
      write is refused, a terminal run is never flipped to `claimed` by a
      stale gate (re-claim after expiry works: the expiry sweep resets the run
      back to `awaiting_human`) (FAR-612, FAR-645)
      (claim.feature, `test_hitl_manager`, `test_hitl_jwt`)
- [x] Same-account re-claim (FAR-686 token recovery): a reviewer who reloaded
      the page may re-claim a gate they already hold, including during the
      claimed-but-undecided window when the run status itself is `claimed` —
      the EXISTS predicate carries a same-account `claimed` arm alongside the
      `HITL_CLAIMABLE_RUN_STATUSES` base, so the atomic guard and the
      pre-check cannot drift. Fresh and cross-account claims keep the strict
      data-rot guard (`test_hitl_manager` same-account re-claim cases)
- [x] Approve resumes the run (`action: approved`, optional notes) — gated by
      `hitl.approve`; a claimed-by-other caller cannot approve
- [x] Reject records the decision and resumes the graph through a router on
      the rejected path rather than leaving a non-terminal state
- [x] Modify-then-approve applies the reviewer's modified output into state
      before resuming; missing/expired claim_token → 403/410, already-decided
      → 409 (`test_hitl_manager` approve-with-modification cases,
      `test_node_runner_hitl` modified-output resume cases)
- [x] Deliver-manual / submit-manual validates reviewer-supplied output and
      passes it to the pipeline; manual output delivery is audited
      (deliver_manual.feature, `test_output_delivery_audit`)
- [x] `human_only` gates refuse automation/MCP clients entirely
      (team_hitl_gate.feature, `test_mcp_security`, `test_mcp_runtime_tools`,
      `test_node_runner_hitl`). REST enforcement keys on the credential
      class: a principal is denied when it is an API key OR its JWT
      `client_kind` claim is not `browser` (FAR-634 — every access/refresh
      token carries `client_kind` stamped at mint time; legacy tokens without
      the claim decode as `browser`). MCP denies outright regardless of
      credential class. Every denial (REST + MCP) emits a warning log and the
      `hitl.human_only_denied` audit event, failure-isolated so an audit
      failure never changes the denial outcome. Honest limitation: agent
      sessions hold the admin password, so a password-minted JWT is
      indistinguishable from a browser login at issuance — the credential
      class is defense-in-depth, and the FAR-611 sweep alarm is the detective
      control (`test_hitl_resilience`, `test_mcp_runtime_tools`)
- [x] The fired gate's config is stamped on the claim row at fire time — the
      executor's interrupt handler resolves the gate's `hitl_gate_config` and
      writes it to `hitl_claims.gate_config_json` (migration 0195) inside the
      interrupt savepoint; a stamp failure is failure-isolated and the gate
      still fires with a NULL config (FAR-634). The human_only resolver reads
      the stamp FIRST — one claim-row lookup instead of walking snapshot
      edges → node configs → live edges — so gate policy is the graph state
      at fire time even if the pipeline is edited afterwards; the walk stays
      as the fallback for legacy rows (fired before the column existed) and
      never-fired gates, with the fail-closed unresolved semantics intact
      (`test_hitl_gate_config`, `test_executor`)
- [x] Team-scoped gates restrict claiming to members whose team role is
      `runner`/`operator` — otherwise `NotTeamMemberError` (`_TEAM_CLAIM_ROLES`)
- [x] Stale gates warn their owners and expired claims are reset to unclaimed
      (`test_overdue_warning`, `test_claim_expiry_job`, `overdue_warning.py`,
      `expiry_job.py`)
- [x] Conditional HITL: an eval condition decides whether a gate activates at
      run time (conditional_hitl BDD + `test_conditional_hitl`)
- [x] Decisions and deliveries are audited (`hitl.output_delivered`,
      `hitl.claim_expired`) and feed the HITL effort-trends panel
      (`/api/v1/dashboard/trends`: hitl_volume, rejection_trend,
      decision/rejection/approval-time aggregates)
- [x] Decision endpoints enforce per-action permissions
      (`hitl.claim`/`hitl.approve`/`hitl.reject`/`hitl.deliver_manual`/
      `hitl.list`) — a caller without the grant gets 403
      (`test_rate_limit_hitl_review`, `test_hitl_resilience`)
- [x] Audit events carry the caller's client type when known (FAR-611):
      `hitl_claimed` / `hitl.output_delivered` / `hitl.output_modified` /
      `hitl.output_rejected` / `hitl.manual_delivery` gain `client_type`
      (`"browser"` for JWT logins via the principal's `via_api_key` marker,
      `"api_key"` for mk_ keys, `"mcp"` for the MCP surface); internal
      callers that cannot know the client omit the key
      (`test_client_type_audit`)
- [x] Approve-sweep anomaly alarm (FAR-611): when one actor's committed
      decisions exceed 5 within 60 seconds AND span more than one pipeline,
      the decision path emits `hitl_approve_sweep_suspected` — an audit
      event and a fire-and-forget webhook dispatch whose `dispatch_event`
      also creates the in-app admin notification (hitl_overdue sibling
      pattern — the alarm writes the notification once, never twice)
      (`sweep_alarm.py`, `test_sweep_alarm`). Detection counts BOTH decision
      surfaces — `hitl.output_delivered` (approve) AND `hitl.manual_delivery`
      (a manual delivery resumes the run past the gate with caller-supplied
      output, the same sweep signal as an approve) — and keys off the audit
      chain (the only per-actor decision record — `hitl_claims.account_id` is
      NULLed at decision time), is failure-isolated (a broken alarm never
      fails the human's decision — the detection SELECT and the emission
      write each run inside a savepoint), and self-suppresses to at most one
      alarm per (org, actor) per hour via a bounded in-process marker (a
      multi-replica deployment may therefore emit up to one alarm per replica
      per hour — bounded duplicates, the correct envelope for an anomaly page)
- [x] HITL review actions are rate limited at 20/min per identity,
      AGGREGATE across runs, gates, actions, and both surfaces — the
      `/hitl/` review routes AND the approve-capable
      `/runs/{run_id}/manual/{gate_id}/submit` route share one budget
      (FAR-611) — the bucket key normalizes the whole variable path tail,
      so the 2026-09-05 bulk sweep's per-gate bucket rotation cannot recur
      (`test_rate_limit_hitl_review` aggregate/throttle cases,
      `test_middleware_internals`)

## Known Gaps

- **`human_only` is enforced at the API/ViewModel layer, not in the HITL
  manager** — `HITLManager` records decisions without re-checking the flag, so
  a mislabelled caller outside the API boundary is the trust boundary.
- **Claim expiry runs on the SAQ worker cadence** — a held claim that expires
  between ticks stays claimed until the next `claim_expiry` sweep
  (`expiry_job.py`).
- **No executing BDD surface for modify-then-approve, `human_only` refusal, or
  overdue warnings** — the pre-existing `modify_then_approve.feature`,
  `human_only_gate.feature` and `overdue_warning.feature` drafts shipped under
  `tests/bdd/features/hitl/` were removed in the 2026-09-07 product-map walk:
  they described a removed API surface (`/api/runs/{id}/human-input`, the
  `waiting_for_human` status, pre-claim-token flows), were never registered via
  `scenarios(...)`, and therefore never executed. The behaviours themselves are
  unit-tested (`test_hitl_manager`, `test_node_runner_hitl`, `test_mcp_security`,
  `test_mcp_runtime_tools`, `test_overdue_warning`, `test_claim_expiry_job`);
  an executing BDD surface would need the drafts rewritten against the current
  API before registration.

## QA History

- 2026-08-29: **improve-architecture (product-map walk)** — new behaviour
  tracker for the registered `feat-hitl` manifest feature (route
  `/settings/hitl-review`, previously absent from the feature graph).
  Behaviours verified against `api/routes/hitl.py`, `core/hitl_manager/*`,
  `core/run_context/autonomy.py`, and the HITL unit/BDD suites. Status:
  covered.
- 2026-08-30: **duplicate-entry reconciliation** — a parallel product-map walk
  had added a second `feat-hitl` tracker at `configure/hitl.md`, breaking the
  one-entry-per-feature invariant. This entry is the superset and is retained.
  The duplicate's only unique citation (`hitl/approval_gate.feature`) was *not*
  folded into `bdd:` here: that feature file ships but no step module registers
  it via `scenarios(...)`, so citing it would claim BDD coverage for scenarios
  that never execute. Status: covered.
- 2026-09-08: **improve-architecture (product-map walk)** — registered the
  FAR-727 HITL review queue testids (`hitl-review-column-headers`,
  `hitl-review-node-name`, `hitl-review-pipeline-name`) in the manifest
  `/settings/hitl-review` `elements` list. They shipped in the view
  (`SettingsHitlReviewView.vue`) without a manifest entry, failing the reverse
  element-coverage guard (`test_mapped_route_elements_cover_owning_view_testids`);
  the elements list now covers the owning view's testids exactly. Status:
  covered.
- 2026-09-07: **improve-architecture (product-map walk)** — closed the stale-BDD
  drift: removed the never-executed, superseded feature files
  (`hitl/approval_gate.feature` marked `@deprecated`, `hitl/human_only_gate.feature`,
  `hitl/modify_then_approve.feature`, `hitl/overdue_warning.feature`) and the
  byte-identical duplicate `eval/conditional_hitl.feature` (the registered copy
  lives at `evals/conditional_hitl.feature`). The covered behaviours are
  unchanged; the architecture suite now guards against new orphaned `.feature`
  files (see `backend/tests/architecture/test_product_map_feature_gaps.py`).
  Status: covered.
