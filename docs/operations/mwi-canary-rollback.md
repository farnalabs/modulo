# MWI (Managed Workspace Inputs) P1 — Canary Plan, Acceptance Criteria, Rollback

FAR-803 production readiness. Scope: the `sandbox_agent` workspace-inputs spine
(FAR-796/797/798/800/801/802, ADR 033). Factual; no feature claims beyond what
is merged on the P1 branches.

## What ships

- Node config `workspace_inputs` on `sandbox_agent` nodes (validated at save
  time by `_validate_sandbox_managed_inputs_config`), resolved host-side
  (refs → commit SHA) BEFORE sandbox creation, provisioned inside the sandbox
  (credential setup → clone of the resolved SHA → teardown), with post-agent
  drift detection attached to the node envelope (`workspace_drift`,
  `workspace_drift_detected` run flags).
- DB migration 0221 adds `environment_profiles.workspace_inputs` (JSON,
  nullable, default `[]` — MWI is strictly opt-in).
- Credentials resolve from the connector store at provisioning time; tokens
  are delivered via a GIT_ASKPASS helper on /dev/shm, never in argv or URLs.

## Canary plan (staged rollout)

1. **Stage 0 — flag-off soak.** Deploy with no pipelines using
   `workspace_inputs`. Zero behaviour change expected: absent config is a
   no-op in the validator (`_validate_sandbox_managed_inputs_config`)
   and in `_sandbox_agent_impl`.
2. **Stage 1 — internal single pipeline.** Enable MWI on ONE internal
   app.modulo.run pipeline (one input, github.com, fixed SHA ref — no movable
   refs in stage 1). Watch: `workspace_input.resolution_failed`,
   `workspace_input.provisioning_failed`, `workspace_input.drift_detection_*`
   warning logs, node failure rate on that pipeline.
3. **Stage 2 — internal movable refs + two hosts.** Add a gitlab.com input and
   connect-backed token auth. Watch the same signals plus credential
   resolution failures (`sandbox.input_credential_failed`).
4. **Stage 3 — broader internal rollout.** Remaining dogfood pipelines.
5. **Customer GA** is NOT gated by us — the feature is already reachable by
   any operator with the merged code; the canary controls US internally until
   acceptance criteria are met.

## Acceptance criteria (to promote stages)

- Save-time validation: no run is ever dispatched with an invalid
  `workspace_inputs` config (unit + graph-validator tests green).
- Resolution failures NEVER create a sandbox
  (`test_workspace_resolution_failure_raises_sandbox_node_failed`, and the
  create-mock assertion in it).
- A provisioning failure kills the node with `SandboxNodeFailedError`; the
  agent command never runs on partial provision.
- Multi-input provisioning is atomic at the node level: all-or-nothing,
  ordered, and a mid-way failure leaves no partial write.
- Token isolation: each input's setup script contains only its own token
  (unit: `TestMultiHostTokenIsolation`); no secret in argv/URLs; teardown
  removes /dev/shm files and asserts absence.
- Drift: a healthy input reports `drift_detected=False` with
  `final_sha == expected_sha`; unknown states fail CLOSED (True).
- Stage-2 promotion: < 1% sandbox_loss/provision failure rate across the
  internal pipelines over 3 consecutive days.

## Rollback criteria and the killswitch (clone-failure-rate threshold)

Abort the canary (move affected pipelines back to stage 0) when either:

1. **Clone / provisioning failure rate** (`workspace_input.provisioning_failed`
   + `workspace_input.resolution_failed` + `sandbox.input_checkout_failed`
   node failures) exceeds **5% of MWI-enabled node runs in any 24h window**,
   or exceeds **20% in any single hour** (clusters a forking remote outage vs.
   a systematic provisioning bug; the 1h spike is the killswitch because it
   usually means a code/config defect, not the network).
2. Any single incident of **credential leakage** (a token observed in agent
   stdout, envelope fields, or logs) — immediate stage-0 rollback, no rate
   condition applies.

### Killswitch mechanism (how to turn MWI off NOW)

- Per node: remove the `workspace_inputs` key from the graph node (UI/MCP
  `update_pipeline_graph`) — the validator and runtime are no-ops without it;
  the node reverts to the empty-home default.
- Org-wide: clear `environment_profiles.workspace_inputs` back to `[]`
  (the migration default; no code deploy needed).
- Last resort: revert the merge commit; the DB column 0221 is additive and
  nullable — no migration-down is required for a safe revert, and leaving the
  column in place after a revert is harmless (unused by the older code).

## Known gaps at the time of writing (verification suite, FAR-803)

- Audit persistence of resolved workspace inputs (FAR-801) lands in a sibling
  merge; until it ships, the check "drift-check-fails-but-audit-still-writes"
  is verified at module level only (orchestration never raises; envelope write
  still occurs), not end-to-end in the DB.
- No MWI-specific retention estimator exists; run-level retention estimates
  (`crud.run_retention._run_row_bytes`) do not yet account for
  `workspace_drift` payloads.
