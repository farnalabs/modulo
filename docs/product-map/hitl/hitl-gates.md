---
id: feat-hitl
prd: N/A
adr: []
code:
  - backend/src/modulo/api/routes/hitl.py
  - backend/src/modulo/core/hitl_manager/__init__.py
  - backend/src/modulo/core/hitl_manager/expiry_job.py
  - backend/src/modulo/core/hitl_manager/overdue_warning.py
  - backend/src/modulo/core/run_context/autonomy.py
  - backend/src/modulo/db/models/hitl_claim.py
  - frontend/src/views/SettingsHitlReviewView.vue
unit-tests:
  - backend/tests/unit/hitl_manager/test_hitl_manager.py
  - backend/tests/unit/hitl_manager/test_output_delivery_audit.py
  - backend/tests/unit/hitl_manager/test_overdue_warning.py
  - backend/tests/unit/hitl_manager/test_claim_expiry_job.py
  - backend/tests/unit/core/hitl_manager/test_hitl_jwt.py
  - backend/tests/unit/api/test_hitl_resilience.py
  - backend/tests/unit/api/test_rate_limit_hitl_review.py
  - backend/tests/unit/pipeline_engine/test_node_runner_hitl.py
bdd:
  - backend/tests/bdd/features/hitl/claim.feature
  - backend/tests/bdd/features/hitl/approve.feature
  - backend/tests/bdd/features/hitl/reject.feature
  - backend/tests/bdd/features/hitl/modify_then_approve.feature
  - backend/tests/bdd/features/hitl/deliver_manual.feature
  - backend/tests/bdd/features/hitl/human_only_gate.feature
  - backend/tests/bdd/features/hitl/overdue_warning.feature
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
      permission
- [x] Claim is atomic — `claim()` issues a short-lived (15-minute) JWT
      `claim_token` scoped to run + gate + client; an already-claimed gate →
      409, expired/invalid claim token rejected (claim.feature,
      `test_hitl_manager`, `test_hitl_jwt`)
- [x] Approve resumes the run (`action: approved`, optional notes) — gated by
      `hitl.approve`; a claimed-by-other caller cannot approve
- [x] Reject records the decision and resumes the graph through a router on
      the rejected path rather than leaving a non-terminal state
- [x] Modify-then-approve applies the reviewer's modified output into state
      before resuming; missing/expired claim_token → 403/410, already-decided
      → 409 (modify_then_approve.feature)
- [x] Deliver-manual / submit-manual validates reviewer-supplied output and
      passes it to the pipeline; manual output delivery is audited
      (deliver_manual.feature, `test_output_delivery_audit`)
- [x] `human_only` gates refuse automation/MCP clients entirely
      (human_only_gate.feature, team_hitl_gate.feature)
- [x] Team-scoped gates restrict claiming to members whose team role is
      `runner`/`operator` — otherwise `NotTeamMemberError` (`_TEAM_CLAIM_ROLES`)
- [x] Stale gates warn their owners and expired claims are reset to unclaimed
      (overdue_warning.feature, `overdue_warning.py`, `expiry_job.py`)
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

## Known Gaps

- **`human_only` is enforced at the API/ViewModel layer, not in the HITL
  manager** — `HITLManager` records decisions without re-checking the flag, so
  a mislabelled caller outside the API boundary is the trust boundary.
- **Claim expiry runs on the SAQ worker cadence** — a held claim that expires
  between ticks stays claimed until the next `claim_expiry` sweep
  (`expiry_job.py`).

## QA History

- 2026-08-29: **improve-architecture (product-map walk)** — new behaviour
  tracker for the registered `feat-hitl` manifest feature (route
  `/settings/hitl-review`, previously absent from the feature graph).
  Behaviours verified against `api/routes/hitl.py`, `core/hitl_manager/*`,
  `core/run_context/autonomy.py`, and the HITL unit/BDD suites. Status:
  covered.