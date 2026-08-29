---
id: feat-hitl
prd: N/A
adr:
  - docs/adr/025-execution-graph-router-hitl-nodes.md
code:
  - backend/src/modulo/api/routes/hitl.py
  - backend/src/modulo/core/hitl_manager/
  - backend/src/modulo/core/hitl_manager/expiry_job.py
  - backend/src/modulo/core/hitl_manager/overdue_warning.py
  - frontend/src/views/SettingsHitlReviewView.vue
unit-tests:
  - backend/tests/unit/core/hitl_manager/
  - backend/tests/unit/core/test_graph_cache_hitl.py
bdd:
  - backend/tests/bdd/features/hitl/
depends-on:
  - feat-runs
  - feat-router
status: covered
---

# Human-in-the-Loop Review

Atomic HITL gates (core principle §5 — "Humans in the loop where it matters"):
claim a pending gate, approve / approve-with-modification / reject it, deliver or
submit manual output, with claim tokens, TTL expiry, team-scoped claims,
`human_only` gates that no agent may auto-resolve, overdue warnings and
structured feedback records on rejection. Driven by `HITLManager` and surfaced
at `/settings/hitl-review` (`SettingsHitlReviewView.vue`) under `feat-hitl`.

## Behaviours

- [x] `POST /runs/{run_id}/hitl/{gate_id}/claim` atomically claims an unclaimed
      gate (200) returning a TTL'd `claim_token`; an already-claimed gate is 409,
      a missing gate 404, a non-team member 403; the run transitions to
      `claimed` (`claim.feature`, `hitl_manager.HITLManager.claim`)
- [x] `POST /runs/{run_id}/hitl/{gate_id}/approve` resumes the run past the gate
      (200, `action: approved`, resume through `PipelineExecutor.resume`);
      missing claim token 422, wrong claimant token 403, expired claim token 410,
      an already-decided gate 409, a cross-org blocking sandbox-capacity
      shortfall 409 (`approve.feature`, `_require_org_sandbox_capacity`)
- [x] `approve-with-modification` writes the approver's modified output into run
      state for downstream nodes; unauthenticated / expired tokens and
      decided gates are rejected as above (`modify_then_approve.feature`)
- [x] `reject` (`decision: rejected`, optional reason) stops the run in the
      `rejected` terminal state carrying `rejection_reason`; a later approve is
      refused 409 (`reject.feature`)
- [x] `non-approver` claims/decisions are forbidden — `human_only` gates cannot
      be auto-resolved by the agent and run status reflects `awaiting_human`;
      `submit_manual_output` validates against the node's output schema (422 on
      missing required fields) (`human_only_gate.feature`, `manual_node.feature`)
- [x] `deliver-manual` lets a claimant bypass the agent output with manually
      supplied output (schema-validated, empty rejected 422), recorded as a
      `hitl.manual_delivery` audit event containing the output
      (`deliver_manual.feature`)
- [x] `GET /runs/{run_id}/hitl/pending` (per-run) and the org-wide pending list
      expose gates awaiting human input with gate labels resolved from the graph
      (`routes/hitl.py` `list_run_pending_gates` /
      `list_org_pending_gates`)
- [x] Claim expiry: expired claims release the gate (a second reviewer can then
      claim it) via `core/hitl_manager/expiry_job.py`;
      `hitl.claim_expired` is recorded to the audit trail
      (`claim.feature` "Claim has a TTL", `event_recording.feature`)
- [x] Overdue warnings: gates within `timeout_seconds` of expiry surface an
      `overdue` warning visible in the HITL review UI with remaining-time badges,
      are highlighted in the awaiting-human filter, and fire a `hitl_overdue`
      notification to configured approvers
      (`overdue_warning.feature`, `core/hitl_manager/overdue_warning.py`)
- [x] Feedback handler: rejection feeds `pending` feedback records that can be
      triaged (status transitions `pending -> routing -> correcting`, invalid
      transitions 422) and spawn a correction run handled by `ai_correction`
      (`feedback_handler.feature`, `api/routes/feedback.py`)
- [x] Claims are JWT-backed (`claim_token`) so the approving principal is bound
      to the decision and replayable across workers
      (`test_hitl_jwt.py`)

## Known Gaps

- **Rejection finish-flow divergence between feature files** — the legacy
      `approval_gate.feature` (kept as a deprecated reference) still describes the
      obsolete non-terminal `rejected` status, while the working `reject.feature`
      asserts the terminal `rejected` status; the deprecated file must not be
      re-enabled.
- **No BDD for the org-wide pending list pagination** — per-run pending gates are
  scenario-locked; the org-scope list surface is verified at the API layer only.
- **Modulo review timeouts are config, not org policy** — the overdue-warning TTL
  derives from gate `timeout_seconds` configuration; there is no per-org or
  role-based override policy.

## QA History

- 2026-08-29: **improve-architecture (product-map walk)** — added this
  behaviour-tracker for the registered manifest feature `feat-hitl`, which had no
  `docs/product-map/` entry. Behaviours verified against `api/routes/hitl.py`,
  `core/hitl_manager/` (+`expiry_job.py`, `overdue_warning.py`), the ten
  `hitl/` BDD feature files and the hitl_manager unit suites. Status: covered.