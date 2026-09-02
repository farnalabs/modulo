# Core Principles – Modulo

**Status:** Draft
**Date:** 11 August 2026
**Related:** `architecture.md`, ADRs (`Repos/devtools/adr/` in the private `farnalabs/devtools` repo — migrated out of this repo 2026-09-02, FAR-434). This is the source of truth for Modulo's engineering principles; public blog content at modulo.run/blog derives from it.

## 1. Schema seams
Every boundary in a pipeline is a typed contract. Inputs validate against `input_schema`, outputs against `output_schema` (JSON Schema draft-07), before anything moves. Schemas are org-scoped, versioned (semver), reusable, composable; abstract schemas enable type-constraint matching during workflow import; schema inference generates drafts from connector data.
**Why:** an agent cannot pass garbage downstream without a record of it. Typed seams make automation auditable and failures loud.
**Where:** Schema Registry (`modulo/core/schema_registry/`), run lifecycle validation steps.
**Benefit:** what you see is what runs; mismatches fail loudly at the seam.

## 2. Immutability of runs
A run executes against a frozen `PipelineSnapshot`: agent versions, schema pins, connector bindings, model backend pins, prompt version hashes, environment profile - all pinned at run-start, never resolved live. Pauses and resumes behave identically. Completed runs are unaffected by later changes (connector removal, prompt edits, pipeline edits).
**Why:** reproducibility and trust - a run's outcome is a function of its snapshot, not of whatever changed since.
**Where:** `architecture.md` snapshot lifecycle.
**Benefit:** pause for a week, resume, same behavior; the audit trail stays truthful.

## 3. Audit as a first-class output
Every state-changing action produces an immutable, org-scoped AuditEvent (actor, action, resource, timestamp). Append-only at DB level (0005 triggers), SHA-256-linked chain for verifiability. SOC 2 evidence export builds on it.
**Why:** governance requires a record you can trust after the fact; the trail is a product output, not an afterthought.
**Where:** `modulo/core/audit_logger/`, `Repos/devtools/adr/004-user-deactivation-replaces-deletion.md`.
**Benefit:** the history you inspect is exactly what happened; auditors and operators read the same truth.

## 4. Deterministic gates
Automated quality checks (regex, JSON Schema, custom function, scored eval) run before HITL gates on the same edge, with pass thresholds and warn/block behavior. A gate either passes or it doesn't; the run history shows which gate caught what. The same deterministic-gate principle extends to the input side: guardrails check what enters a run at the ingestion edge, before the first node executes: guarding what enters the run rather than only what leaves it (see §4a below).
**Why:** automation must be checked by rules, not vibes; evals make quality measurable per run.
**Where:** `modulo/core/eval_engine/`, `docs/architecture.md` eval engine section.
**Benefit:** failures surface at the gate with evidence, not after merge.

### 4a. Guardrails at the ingestion edge (input-side)
Deterministic data-safety checks also run on the **input** side: before a run's `input_payload` is persisted. A guardrail is an eval definition with `eval_type="guardrail"`; detection is deterministic pure evals only (regex | json_schema), and actions are `observe | warn | block | redact`. A `block` is terminal (`eval_failed`) with no HITL gate; remediation is the dedicated guardrail-override endpoint (`POST /runs/{run_id}/guardrail-override`), which re-runs the guardrail pass on the supplied input (re-block safe). The generic `recover_node` path REFUSES guardrail-blocked runs (never `deliver_manual`, which requires a HITL gate). Redaction is masks-only, static field-path based with exact/anchor matching (substring matching forbidden), and persisted state is always post-redaction. For webhooks the boundary is enforced TWICE over: webhook intake additionally runs the pass at the trigger boundary in `TriggerEngine.handle_webhook` (FAR-214), BEFORE the dedup insert: a `block` rejects-and-retries at the boundary (400, `guardrail_blocked` TriggerEvent + raw payload stored for replay, no run, no dedup slot consumed), `redact` applies masks pre-dedup, and the dedup key is a canonical POST-guardrail payload hash (so logically identical payloads dedup across encodings). Guardrail configuration is itself governance-controlled: orgs manage safety controls as versioned, hashed, PR-gated YAML (propose, diff, apply, `modulo/core/guardrails/config.py`) with snapshot pinning (`organisations.guardrail_pins_json`) and drift detection, so changes to safety controls are reviewed and audited before they take effect. A guardrail may additionally declare a conformance claim (`required_capabilities`); that claim is re-validated at **node start** against the live capability surface (bound connector scopes, EnvironmentProfile capabilities, agent required capabilities); a `block`-action guardrail whose capability is absent/unknown mid-run blocks the node and routes it to HITL (`awaiting_human`), never a silent abort or a fail-open continuation, while warn/observe claims stay advisory.
**Why:** boundaries must be checked before data crosses them, not after damage is done; redaction that destroys is a loss, masking preserves; and a capability promise made at run-creation must be re-confirmed before a node acts on it.
**Where:** `modulo/core/guardrails/`, `modulo/core/guardrails/config.py`, `modulo/core/guardrails/conformance.py`, ingestion seam in `modulo.db.crud.run.create_run`.
**Benefit:** structured credentials never persist unmasked, the run history shows which guardrail caught what at the edge, and a mid-run capability change can never silently outrun the boundary it was promised.

## 5. Humans in the loop where it matters
HITL gates are atomic (claim, decide, record); `human_only` gates cannot be approved by the agent under review; team-scoped claims. Autonomy is earned, not assumed.
**Why:** governance is permissions plus gates plus audit; automation never gets a blank check.
**Where:** `modulo/core/hitl_manager/`, BDD `backend/tests/bdd/features/hitl/human_only_gate.feature`.
**Benefit:** you can scale agents without surrendering control.

## 6. Secret hygiene
Credentials decrypt once at run-start into a run-scoped context; they never enter LangGraph state, checkpoint blobs, OTel spans, or logs. Semgrep-enforced.
**Why:** secrets in agent-visible state are a liability; one-decrypt-per-run bounds exposure.
**Where:** `modulo/connectors/`, semgrep rule `credential_in_state`, `docs/security/secret-management.md`.
**Benefit:** a run cannot leak credentials in traces, logs, or prompts.

## 7. We dispatch, we don't run agents
Modulo dispatches work to external agent runtimes; it owns dispatch, auth, audit, cost tracking, eval gates, HITL. The runtime owns the tool-using loop. Bring your own model, agent, prompt.
**Why:** established agent runtimes do tool-use better than a platform can; competing with them is the wrong strategy.
**Where:** `Repos/devtools/adr/003-agent-dispatch-model.md`, `sandbox_agent` node type.
**Benefit:** swap runtimes without changing governance; your stack stays yours.

## 8. Correction never rewrites history
A failed output produces a new run with feedback attached; the original stays as it was. FeedbackRecords are immutable after creation.
**Why:** an editable trail is not a trail; the correction loop is a first-class pipeline, not an in-place edit.
**Where:** `modulo/core/feedback_manager/`.
**Benefit:** you can trust the history while still improving the system.

## 9. Self-hosted, no telemetry
Your infra, your data, no cloud dependency. Runs on Docker Compose or Fly; no telemetry by default.
**Why:** governance without data ownership is theatre for regulated and IP-sensitive teams.
**Where:** deployment docs, `docs/architecture.md`, brand positioning in Repos/admin.
**Benefit:** adoption is not blocked by data-residency concerns.

## How this doc is used

- New engineers and agents read it to understand why the system is shaped this way.
- Blog content at modulo.run/blog derives from it (the "Principles of Modulo" post links back here).
- Comparison pages reference individual principles instead of re-explaining them.
- When a principle changes, update this doc first; the blog follows.
