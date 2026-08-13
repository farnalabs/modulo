# Agent Failure UX — How Modulo Should Surface Failure Modes

**Status:** Design spec (promoted 2026-08-13 from the harness proposal
`Repos/devtools/harness/.agent-work/agent-failure-ux-proposal.md`; the source file
remains in devtools — this is the product-docs home). §15 (v6) is the consolidated
implementable spec; §0–§14 are design history, and wherever an earlier section
conflicts with v6, v6 wins.
**Owner:** Modulo product
**Target PRD section:** run-status vocabulary + error-code vocabulary (see PRD §8
run entity / state machine; the proposal's `superseded` status is not yet in the
shipped status set — `stalled` and `budget_exceeded` already are)
**Related:** `docs/troubleshooting.md` (run-level `error_code` runbook), PRD §8
run entity

**Shipped-state notes (do not read as current code — read as the target design):**
- The base agent output contract (`status` + `summary` required, `outcome`
  encouraged) is already adopted by dogfood drivers; `node_runner` reads
  `summary`/`changed_files`/`pr_url` from `output.json`.
- The shipped run status set (PRD §8) is exactly `pending`, `running`,
  `awaiting_human`, `claimed`, `complete`, `failed`, `cancelled`, `eval_failed`,
  `stalled`, `budget_exceeded` — the 10 values of the current `ck_runs_status`
  CHECK constraint (`stalled` added by migration 0077, `budget_exceeded` by
  0090/FAR-104; both are shipped terminal statuses). The proposal's `superseded`
  *status* is the only one NOT shipped; today's `executor_superseded` is an
  `error_code` value (runbook in `docs/troubleshooting.md`), and the proposal
  maps it to `superseded`. `executor_stalled` is likewise a legacy `error_code`
  that maps to the already-shipped `stalled` status via `agent.stall`.
- This document is the implementable reference for the failure-surfacing
  design. Section numbering (§0–§15) is preserved verbatim from the source.

---

## 0. Design principles

1. **Separate harness truth from work truth.** Run `status` = machinery verdict. Agent `status`/`outcome` (in the node output contract) = work verdict. Today these are conflated → both false failures (harness broke, work fine) and false successes (harness fine, work zero).
2. **The machine decides run `status`; the agent declares its own `status`/`outcome`; the harness verifies work where it can; the user decides what to do.** Unambiguous cases are decided for the user; ambiguous cases get a banner + affordances.
3. **Harness-verifiable evidence beats agent word — but evidence unavailability is a distinct state, never conflated with evidence-empty.** Where the harness can check a claim (git tree diff, PR existence, connector logs), it does. Unverifiable → `unknown`, never a flag, never a silent pass.
4. **One vocabulary everywhere.** Same codes, statuses, guidance, and alert payload shapes across run detail, notifications, emails, webhooks, API/MCP — sourced from a single registry.
5. **Zero configuration must be useful.** Defaults catch the expensive failures (silent no-op, hallucinated completion, connector auth) and stay quiet about noisy transient infra until retries exhaust.
6. **Terminal statuses are immutable.** Recovery NEVER mutates a stored terminal row: it records a user verdict (presentation-layer) or spawns a NEW attempt row. Reporting/analytics stay consistent.
7. **Explicit author configuration beats defaults; defaults are code-filtered.** An author who deliberately sets a retry policy or an alert rule gets their intent honored — the defaults are a floor for the unconfigured, not a cudgel over the configured.

---

## 1. The single failure taxonomy: the error-code registry

One axis. Each error code = registry entry `{code, class, display_name, retryable, alert_severity, guidance}`. `class` is a tag for grouping/filtering, NOT a parallel taxonomy. `retryable`/`alert_severity` derive from the class table (§1.3) plus per-code overrides.

### 1.1 Classes (tags)

| Class | Meaning | Typical codes |
|---|---|---|
| `harness` | Executor/dispatch/checkpoint/DB/worker machinery (false-failure potential) | `harness.db.connection_lost`, `harness.state_serialization`, `harness.sdk_task_cancelled`, `harness.node_cancelled`, `harness.worker_failed` |
| `sandbox` | Sandbox env failed to produce valid result | `sandbox.no_output_json`, `sandbox.spawn`, `sandbox.network` |
| `agent` | Agent-verdict failure (self-reported or verified) | `agent.failed`, `agent.stall`, `agent.no_op` |
| `node` | Per-node guards | `node.timeout`, `node.runaway` |
| `contract` | Output contract violations | `contract.schema`, `contract.no_output` |
| `connector` | Typed provider errors | `connector.invalid_key`, `connector.permission`, `connector.rate_limit`, `connector.network` |
| `capacity` | Capacity/queue | `capacity.org`, `capacity.pipeline`, `capacity.claim` |
| `eval` | Eval suite verdicts | `eval.blocked`, `eval.failed` |
| `config` | Pipeline configuration errors | `config.invalid` |

### 1.3 Class → retryable/alert/evidence matrix (single source of truth)

| Class | Retryable (default) | Alert (default) | Harness-verifiable evidence? |
|---|---|---|---|
| `harness` transient | yes | OFF until retries exhaust | n/a (machinery) |
| `harness` permanent (state_serialization) | no | warning | n/a |
| `sandbox.*` | yes | OFF until retries exhaust | exit code, sandbox filesystem |
| `node.timeout` | yes (raise timeout) | warning | elapsed vs timeout |
| `node.runaway` | no | warning | token usage vs budget |
| `agent.failed` | no | **critical** | agent status + evidence check |
| `agent.stall` | **no** (resume instead) | warning | last-activity timestamp |
| `agent.no_op` | no | warning | harness evidence (tri-state §7.2) |
| `contract.schema` | no | warning | schema result |
| `connector.invalid_key` / `permission` | no | **critical** | auth probe |
| `connector.rate_limit` / `network` | yes | OFF until retry-exhaust | SDK error class |
| `capacity.*` | yes (when free) | OFF | queue state |
| `eval.*` | no | warning | eval report |
| `config.invalid` | no | warning | graph validation |

---

## 2. Run status taxonomy (harness truth only)

### 2.1 Proposed status set

**Active:**

| Status | Meaning |
|---|---|
| `pending` | Queued, not yet dispatched. Also the **capacity-deferred** state: a run demoted to `pending` with a `capacity.*` error_code marker is recovered by the dispatcher/sweep, NOT failed (matches executor `_check_capacity` today). |
| `running` | Executing; at least one node active. |
| `claimed` | HITL gate claimed by a human reviewer (real status today — in CHECK constraint, whitelist, resumable set). |
| `awaiting_human` | Paused at a HITL gate. |

**Terminal (immutable):**

| Status | Meaning | Maps from | Classes |
|---|---|---|---|
| `complete` | Harness succeeded; work verdict success or unknown-positive. | — | healthy |
| `failed` | Harness failed OR agent self-reported failure (A1 elevation). Union. | — | harness, sandbox, agent, node, contract, connector, capacity_timeout, eval, config |
| `stalled` | Node went silent past idle watchdog. Terminal. | (exists) | agent.stall |
| `budget_exceeded` | Token budget exhausted (FAR-104). Terminal. | (exists) | — |
| `cancelled` | Human cancelled. Terminal. | — | operator |
| `superseded` | **(NEW)** Newer run took over. Terminal, never alerts on its own. | `executor_superseded` → `superseded` | operator |
| `eval_failed` | Work finished but eval blocked/failed. Terminal. | (exists) | eval |

**Explicitly NOT statuses:** `degraded` (attribute); `partial` (deferred until a real partial-completion incident is observed — per-node chips carry the nuance today); `waiting_for_lock` (excised in 0074/0075/0077); `expired` (vestigial in analytics tuples only — purge it there).

### 2.2 Status → error-code mapping

| Failure | Resulting status | error_code |
|---|---|---|
| Harness/infra | `failed` (+ false-failure banner if work intact) | `harness.*` |
| Sandbox/execution | `failed` (retryable) | `sandbox.*` |
| Agent self-reported failure | `failed` (unconditional elevation — §2.3.1) | `agent.failed` |
| Silent no-op / hallucinated | `complete` + `agent.no_op` warning (tri-state evidence) | `agent.no_op` |
| Stall | `stalled` | `agent.stall` |
| Resource exhaustion | `failed` | `node.timeout` / `node.runaway` |
| Token budget exceeded (FAR-104) | `budget_exceeded` | `budget_exceeded` |
| Contract violation | `failed` (schema fails run — §10) | `contract.schema` / `sandbox.no_output_json` |
| Connector/provider | `failed` | `connector.*` |
| Capacity (deferred) | `pending` + `capacity.*` marker (NOT failed — matches engine today) | `capacity.*` |
| Capacity (timeout) | `failed` | `capacity.timeout` |
| Eval | `eval_failed` | `eval.*` |
| Operator/superseded | `cancelled` / `superseded` | none (or `operator.superseded` in detail) |

### 2.3 Transition rules

1. **A1 elevation (unconditional, predicate explicit):** trigger = `agent_status == "failed" OR outcome == "failed"` on a captured sandbox node output. Consequence: run terminal `failed` + `error_code=agent.failed` + critical alert — REGARDLESS of work-intact. Then branch on work for the banner: work intact → "agent reported failure but produced output" (inspect); no work → zero-work banner. A self-declared failure NEVER lands `complete` — including `agent_status=failed` + exit 0.
   **Non-zero exit is still harness truth for node status:** a crash/OOM/segfault after writing `agent_status=completed` marks the node `failed` (`sandbox.no_output_json`/`harness.sdk_task_cancelled`) — exit code is NOT discarded, it just no longer *overwrites* the self-report for elevation purposes.
2. **Harness failure with intact work (false-failure guard):** run about to terminalize `failed` for `harness.*`/`sandbox.*` + ≥1 node completed a valid artifact + full DAG ran → `failed` + `work_intact=true` + false-failure banner. `work_intact` is per-node aggregated: ALL completed nodes intact AND no unexecuted downstream nodes. A run truncated at node 3 of 5 is NOT work-intact.
3. **`partial` deferred** (per v2 decision; no observed incident requires it).
4. **Supersession:** real `superseded` status. The superseder is watched — if the superseding run terminalizes `failed`/`stalled`, that failure alerts normally. Superseder chain is bounded to the immediate successor; a chain that never terminalizes is time-boxed. A run superseded while `awaiting_human`: the open gate is auto-cancelled and the run lands `superseded`.
5. **Retry = new attempt row (resolves the v2 contradiction).** A re-dispatch spawns a NEW run row (attempt_n+1) carrying `parent_attempt_id`; the terminal row is NEVER flipped back to `pending`. This replaces today's `UPDATE runs SET status='pending'` (executor.py:1639/1750) with insert-new-attempt semantics. Supersession guard: a pending retry is cancelled if a newer run claims the pipeline. `accepted_as_complete` lives on the terminal row it was granted for, and does not follow a re-run.
6. **Fail-open terminalization:** all new elevation/verdict/heuristic computation wrapped so that on ANY exception the run still terminalizes via today's path + `harness.elevation_failed` warning flag. Scope: protects against computation errors. The DB-down-at-terminalization case is out of scope for the new machinery (it is today's H1 problem; the stale-run sweep remains the backstop). Terminalization is a single atomic transaction; no partial writes.

---

## 3. Error-code contract

### 3.1 Shape
`<namespace>.<reason>`, lowercase dotted, no exception class names. Registry per §1.2. Namespaces = class tags.

### 3.2 Legacy → new mapping (write-time, pure code→code)

| Legacy (today) | New code | retryable | alert |
|---|---|---|---|
| `OperationalError` (checkpoint) | `harness.db.connection_lost` | yes | off until retry-exhaust |
| `TypeError` (msgpack state) | `harness.state_serialization` | no | warning |
| `NodeCancelledError` | `harness.sdk_task_cancelled` | yes | off |
| `TimeoutError` / `node_timeout` | `node.timeout` | yes | warning |
| `SandboxNodeFailedError` | `sandbox.no_output_json` | yes | off |
| `runaway` / `runaway.tokens_exceeded` | `node.runaway` | no | warning |
| `executor_stalled` | `agent.stall` | no (resume) | warning |
| `executor_superseded` | `superseded` (status) | n/a | never |
| `executor_setup_failed` / `executor_failed` / `executor_heartbeat_lost` | `harness.executor_*` | yes | off |
| `never_dispatched` / `dispatch_failed` / `worker_lost` / `capacity_timeout` | `harness.dispatch_failed` / `capacity.timeout` | yes | off |
| `configuration_error` | `config.invalid` | no | warning |
| `node_cancelled` | `harness.node_cancelled` | yes | off |
| `task_failure` | `harness.worker_failed` | yes | off |
| `eval_blocked` / `eval_suite_blocked` | `eval.blocked` | no | warning |
| `output_rejected` | `contract.schema` | no | warning |
| `gate_creation_failed` | `harness.gate_creation_failed` | yes | off |
| `claim_cap_exhausted` / `pipeline_capacity` / `org_capacity_limited` | `capacity.*` | yes (when free) | off |

**Hard rules:**
- The alias table is a SHARED MODULE used by `_retry_after_policy` (which must also match the new codes AND legacy aliases, with a regression test per `on` event including `node_timeout`), the alert-rule matcher, and the notifier event_mapper — one table, three consumers, no drift.
- The stale-run sweep's `CAPACITY_MARKERS` exemption is extended to `capacity.*` codes.
- Old runs keep raw codes forever; only new writes are mapped. Presentation renders legacy codes through the registry.

### 3.3 Retry semantics (premise-corrected, explicit-author-precedence)

**Premise (verified):** no default retry policy exists today; `_retry_after_policy` returns None without a policy; transient retries flow through SAQ `saq_run_retries`/`node_attempt_count`.

**New default policy:** `{"on":["timeout","failure"],"max_retries":1}` applied ONLY when the pipeline sets no explicit policy, CODE-FILTERED: only `retryable=yes` codes retry.

**Explicit-author precedence (v3 fix):** an author-set `retry_policy` is honored VERBATIM — including `on:["stall"]` (a documented, working feature today) and `on:["failure"]` for `agent.failed`. The registry's `retryable=no` is a DEFAULT floor, not a hard cudgel. A save-time validation warning tells the author when their policy names a code the registry marks non-retryable ("your retry_policy on:['failure'] will retry agent.failed; the registry marks it non-retryable — confirm this is intended"). The default policy's filter uses the floor; explicit policies do not.

**Stall = resume, not retry:** `agent.stall` default retryable=no; recovery is Resume (new attempt inheriting checkpoint). An author may still set `on:["stall"]` explicitly.

---

## 4. Run-detail UX

### 4.1 Page anatomy (top → bottom)

1. **Verdict header** — status badge + one plain-sentence verdict (from `error_code.guidance`). Colors: `complete` green, `failed` red (sub-color harness vs agent), `stalled` amber, `cancelled`/`superseded` grey, `eval_failed` amber. An `accepted_as_complete` run renders the stored `failed` status with an "accepted as complete by <user>" badge overlay (§4.4).
2. **Guidance card** — "What happened" + "What to do" (registry), recovery buttons (§6). Traceback behind "Show technical details".
3. **Two-truth strip** — `Harness: succeeded/failed/stalled` vs `Work: intact / none / unknown / partial-node` + evidence state (`verified` / `unverifiable`).
4. **Node timeline** — one row per node: status chip (ACTUAL artifact statuses: `completed`, `failed`, `executed`, `interrupted`, `condition_skipped`, `skipped`, `auto_approved`, `awaiting_human`), agent verdict chip (`completed`/`failed`/`unknown`), outcome chip (`success`/`partial`/`failed`/`unknown`), elapsed, cost. `approved`/`rejected`/`delivered_manual` render from the nested `result` sub-field of an `interrupted` artifact.
5. **Node panel** — self-reported agent `status`, `outcome`, `summary`; `output_json`; `changed_files`; `pr_url`; `cost`; `stall_reason`; masked stdout/stderr; exit code; traceback; schema errors inline.
6. **Attempt history** — one row per attempt (new-attempt-row semantics §2.3.5): attempt #, status, error_code, duration, cost, link.

### 4.2 Banners

- **"Work completed but run failed"** — `failed` (harness/sandbox) + `work_intact=true`. Buttons: **Accept as complete** (human-only §4.4), **Re-run**.
- **"Run completed but no work done"** — `complete` + `agent.no_op` (evidence=verified-empty). Buttons: **Inspect**, **Escalate**, **Re-run with stricter guard**. When evidence=unverifiable, the banner is replaced by a muted "work could not be verified" notice — never a no-op flag.
- **"Agent reported failure but produced output"** — A1 elevation + `work_intact=true`. Buttons: **Inspect**, **Escalate**, **Re-run**.
- **"Agent reported failure — no work was done"** — A1 elevation + `work_intact=false`. Buttons: **Escalate**, **Re-run (opt-in)**.

### 4.3 Per-failure presentation matrix

| Code class | Verdict line | Distinct UI element |
|---|---|---|
| `harness.*` (work intact) | "Platform error, work intact" | false-failure banner |
| `harness.*` (no work) | "Platform error" | retry CTA |
| `sandbox.*` | "Sandbox failed to return usable output" | retry CTA |
| `agent.failed` | "The agent reported it failed" | critical styling; banner per §4.2 |
| `agent.no_op` | "Completed, but no verifiable work" | false-success banner (evidence=verified-empty only) |
| `agent.stall` | "Agent went silent" | stall callout; resume CTA |
| `node.timeout` | "Hit the timeout guard" | inline timeout suggestion |
| `node.runaway` | "Hit the token budget" | raise-budget + re-run |
| `contract.schema` | "Output didn't match the schema" | field-level errors; schema editor link |
| `connector.invalid_key`/`permission` | "Connector issue: <specific>" | fix-credentials CTA; never raw provider message |
| `connector.rate_limit`/`network` | "Connector temporarily unavailable" | retry CTA |
| `capacity.*` (deferred) | "Queued — waiting for capacity" | queued state; notify-me-when-free |
| `eval.*` | "Work done, evals failed" | eval report link |
| `cancelled` | "Cancelled" | grey |
| `superseded` | "Superseded by run #N" | link; grey |

### 4.4 `Accept as complete` — human-only, presentation-layer verdict (v3)

- **Who:** human operator only. Enforcement is SPECIFIED: the endpoint rejects any bearer token carrying machine/worker scope (browser-session auth only), requires org-member + pipeline-owner role, and reuses the `human_only` HITL gate pattern (`make_hitl_gate_fn`) for the interrupt + claim + approve/reject flow. A worker MCP token MUST get 403 (acceptance test). The action targets a TERMINAL run with no live gate record, so it is a new verdict record owned by `hitl_manager`, not a literal drop-in of the mid-run gate — same enforcement machinery, new record type.
- **What it does:** does NOT mutate stored `status`. Records a user verdict (`accepted_as_complete=true`, `accepted_by`, `work_intact` snapshot, `recovery_action` audit event) and renders it as a presentation-layer overlay — the same pattern as `superseded` (§8.5). Consumers that only read `status` are unaffected; the run detail + lists + a derived `effective_success` render include accepted runs. `is_success` SQL helper computed at terminalization is provided so analytics can choose whether to count accepted runs.
- **Guard:** only offered when `work_intact=true` AND full DAG completed. Also offered for `agent.failed` + work intact (adds the missing §6 row).

---

## 5. Alerting & notifications (plugged into the existing stack)

**Wiring (v3 fix — no parallel path):** every new signal maps to an existing `EVENT_*` constant in the notifier (`EVENT_RUN_FAILED`, `EVENT_RUN_STALLED`, …), adding `EVENT_AGENT_FAILED`, `EVENT_AGENT_NO_OP`, `EVENT_RUN_SUPERSEDED`; per-event title/body/url templates live in `event_mapper`; delivery lands in the existing `notification_delivery_log`. The §5.1 defaults materialize as synthesised `ErrorNotificationRule` rows (a seeded default-rule set, editable per org), evaluated by the existing AlertEngine with its Redis cooldown keyed `(rule_id, fingerprint)` — dedup extends that key to `(rule_id, fingerprint, run_id)` so merged alerts enumerate runs and `alert_resolved` carries the run_id for partial resolution. Webhook dedup/rate-limit state lives in Redis with an in-memory local fallback, never the DB.

### 5.1 Alert-by-default matrix (out of the box)

| Signal | Severity | Default | Evidence basis |
|---|---|---|---|
| `agent.failed` (A1 elevation) | **Critical** | ON | agent status + work check |
| `agent.no_op` | **Warning** | ON | harness evidence verified-empty ONLY |
| `connector.invalid_key`/`permission` | **Critical** | ON | auth probe |
| `contract.schema` | Warning | ON | schema result |
| `node.runaway` | Warning | ON | budget |
| `agent.stall` | Warning | ON | idle watchdog |
| `node.timeout` | Warning | ON | timeout |
| `harness.*` transient | Warning | OFF until retries exhaust | — |
| `harness.state_serialization`, `config.invalid` | Warning | ON | — |
| `capacity.*` | Info | OFF | — |
| `eval.*` | Warning | ON | — |
| `cancelled`/`superseded` | — | OFF forever (superseder watched) | — |

**Chronic-failure signal (deferred to analytics tile, v3):** the consecutive-failure counter becomes a dashboard tile (failing-pipeline badge after N=3 same-code failures), NOT an active alert in v1 — the individual failures already alert after retries exhaust, and the counter's atomicity/reset semantics need field data. Promote to alert only if field data shows daily failures staying silent. (Resolves L3-5/L2-9: don't build speculative alert machinery for an unproven gap.)

### 5.2 Alert message contract

**Human (in-app + email):**
```
[Modulo] Pipeline "Backlog Groomer" — agent reported failure (critical)
Run #1234 · 42m · $0.83 · <link>
The agent reported it failed and produced no work.
What to do: inspect the agent's output, then re-run or edit the agent prompt.
Node: groom-tickets · error_code: agent.failed · <details link>
```
No traceback in alert bodies. **Webhook (structured, separate schema):** `{event, alert_id (stable), group_id, run_id, run_number, pipeline_id, error_code, severity, attempt_n, retried, node_id, run_url}` + `alert_resolved {alert_id, group_id, run_id, reason}`. Payload is schema-asserted to contain no traceback/env/credential/stdout patterns (golden-corpus test).

### 5.3 Noise rules

1. Retry suppression: harness/sandbox/connector-transient alert only after retries exhaust; `attempt_n` included.
2. Dedup: `(rule_id, fingerprint, run_id)` — distinct runs stay distinct in the enumeration; `alert_resolved` carries run_id for partial resolution.
3. Org-wide correlation: a fleet-wide infra outage collapses to ONE "platform incident" alert with drill-down ("N suppressed") — implemented via a correlation rule over the existing rule engine, not a new mechanism. **`[REMOVED in v4]`** — no corpus incident is a fleet outage; dashboard tile + retry-exhaust alerts cover it.
4. Precedence: explicit user rules beat defaults (the defaults are seeded `ErrorNotificationRule` rows, editable); migration report lists behavior changes.
5. Superseded/cancelled never alert; the superseding run's failure alerts normally.
6. Alert delivery failure: retry with backoff + dead-letter + `modulo_alert_delivery_failed_total` metric.

---

## 6. Recovery affordances — full transition table

Every primary action logs a `recovery_action` event. Recovery NEVER mutates a terminal row: accept = presentation-layer verdict; resume/rerun = NEW attempt row.

| Current terminal | Primary action | Result |
|---|---|---|
| `failed` (harness, work intact) | **Accept as complete** | presentation-layer verdict on terminal row (status stays `failed`) |
| `failed` (harness, no work) | **Re-run** | new attempt row (attempt_n+1) |
| `failed` (sandbox) | **Re-run** | new attempt row |
| `failed` (agent.failed, work intact) | **Accept as complete** / Escalate | verdict or new attempt |
| `failed` (agent.failed, no work) | **Escalate to human** | shareable run link (org-scoped, least-privilege) |
| `failed` (agent.failed, no work) | Re-run (opt-in) | new attempt row; explicit policy required |
| `complete` + `agent.no_op` | **Edit-and-rerun** | new attempt row |
| `stalled` | **Resume** | new attempt inheriting checkpoint; terminal row unchanged |
| `failed` (node.timeout) | **Edit timeout + re-run** | new attempt; suggested_timeout = max(2×elapsed_to_stall, timeout+300) |
| `failed` (node.runaway) | **Raise budget + re-run** | new attempt |
| `failed` (contract.schema) | **Edit schema + re-run** | new attempt |
| `failed` (connector.*) | **Fix connector → re-run** | new attempt; deep link |
| `failed` (capacity.timeout) | **Notify me when free** | queued; notification on retry |
| `eval_failed` | **View eval report / Re-run evals** | new eval attempt (run row unchanged) |
| `cancelled` | **Re-run** | new attempt |
| `superseded` | **View superseding run** | navigation only |

---

## 7. Out-of-the-box defaults

### 7.1 The canonical agent output contract (v3 — field name `status`, already adopted)

```jsonc
{
  "status": "completed",            // REQUIRED: "running" | "completed" | "failed" — the AGENT's verdict
  "summary": "Groomed 5/5 tickets", // REQUIRED: human-readable, non-empty for completed
  "outcome": "success",             // OPTIONAL-but-encouraged: "success" | "partial" | "failed"
  "output_json": { ... },           // OPTIONAL free-form extension
  "changed_files": [],              // OPTIONAL, encouraged for code agents
  "pr_url": "",                     // OPTIONAL, encouraged for code agents
  "error": "..."                    // OPTIONAL, recommended when status="failed"
}
```

**Wiring (v3 fix — no rename, verbatim propagation):** node_runner currently derives node status from exit_code and reads only summary/changed_files/pr_url from output.json (node_runner.py:1706-1718). v3 mandates: node_runner ALSO surfaces the agent's raw `status`/`outcome` from output.json VERBATIM as distinct node-output fields (`agent_status`, `agent_outcome`), without overwriting them from exit_code. The executor's A1 elevation reads `agent_status`/`agent_outcome`; the exit-code-derived node status remains harness truth for the node. Legacy drivers already write `status` (verified: `_common.exit_completed/exit_failed`, backlog-groomer, ticket-picker, ticket-to-pr-coder) — so the base contract is already adopted; no driver migration needed. A `status` field missing from output.json degrades to `unknown` (never a false `complete`).

- Base fields (`status`, `summary`) required, extracted before any custom schema.
- `outcome` optional; absence → `unknown` — no heuristic flag, no alert (§7.2 gate).
- v1's `agent_verdict` rename is REJECTED: the agent contract keeps `status` (collision with run `status` is not a real collision — different objects; run status means harness truth, agent status means work verdict, and node_runner's node status remains exit-code-derived harness truth).

### 7.2 Completion-truthfulness heuristic — tri-state evidence (v3)

**Evidence is TRI-STATE: `verified_empty` | `has_work` | `unverifiable`.** `unverifiable` NEVER fires a flag or alert, logs `heuristic.unverifiable`, and renders "work could not be verified" — it is not evidence-empty and not evidence-has-work.

**Capture timing:** evidence is captured BEFORE sandbox teardown (at terminalization, on the snapshot the runner already holds) — a post-teardown git-diff walk is impossible and would force `unverifiable` on every run. The probe is bounded (`asyncio.wait_for` per SDK call, per repo rules) and runs off the run's critical path so it never blocks terminalization.

**Minimal detector (v3 — cut the over-machinery):**
1. For code agents (node declares `changed_files`/`pr_url` intent OR class=code): `verified_empty` = real git diff is empty (whitespace-only ignored) AND no PR exists AND `output_json` has no non-metadata key. `has_work` = any evidence positive.
2. For non-code agents: `verified_empty` = `output_json` has no agent-specific content (empty array/dict does NOT count). `has_work` = ≥1 agent-specific key.
3. **Gate:** `agent.no_op` fires ONLY when `outcome` is explicitly `success` AND evidence=verified_empty. Absent `outcome` → `unknown`, no flag, no alert. An explicit `outcome:"noop"` (new optional value) declares an intended no-op and never flags. A summary-only agent that declares `outcome:success` with empty evidence IS flagged (that is the #7 shape) — class-based exemption is dropped because class is not a runtime node property (v2 flaw); the explicit `outcome:"noop"` is the honest escape hatch.
4. **Zero-volume claim check (kept, minimal):** a success summary claiming quantities with an empty artifact list is evidence of `verified_empty` — the cross-check is agent-count vs artifact-count, noted as necessary-not-sufficient (detects silent no-ops, not adversarial agents; stated honestly).
5. Severity: Warning (§5.1). Escalates to Critical only on verifiable fabrication (e.g. `pr_url` claims a PR that does not exist). **`[REMOVED in v4]`** — PR-claim-fabrication escalation is struck; `agent.no_op` is warning-only for v1.
6. Deployment guard: ships in dry-run (computes, logs, no alert) for one deprecation cycle, per-pipeline opt-in; golden-corpus test (§11).

### 7.3 Defaults table

| Setting | Default | Notes |
|---|---|---|
| Base contract | `status`+`summary` required; `outcome` encouraged; `output_json` free-form | Already adopted by dogfood drivers |
| Custom schema | Extends base; validation fails the run (§10) | Opt-in legacy, default new |
| Retry policy | `{"on":["timeout","failure"],"max_retries":1}`, code-filtered; explicit author policies win | Premise-corrected |
| Alerts | §5.1 matrix as seeded rules; `agent.no_op`=Warning | Plugged into AlertEngine |
| Node timeout | keep per-node; guidance 1200s+ for complex tasks | — |
| Truthfulness heuristic | ON (tri-state), dry-run for a cycle | — |
| Chronic-failure | analytics tile only in v1 (no active alert) | #11 visibility via dashboard |

### 7.4 Signal precedence (truth table)

Authority: **harness evidence** > **agent status** > **outcome** > **heuristic inference**. A1 elevation is unconditional on `agent_status=failed OR outcome=failed`; evidence can add a positive-completion note but does not override a self-declared failure (conservative bias; recoverable via Accept-as-complete).

| agent status | outcome | evidence | Run status | Alert |
|---|---|---|---|---|
| failed | failed | any | `failed` (A1) | critical agent.failed |
| failed | success | has_work | `failed` (A1) + inspect banner | critical agent.failed |
| completed | failed | any | `failed` (A1) | critical agent.failed |
| completed | success | verified_empty | `complete` + agent.no_op | warning |
| completed | success | unverifiable | `complete` (no flag) + "could not verify" notice | none |
| completed | success | has_work | `complete` | none |
| completed | partial | any | `complete` + per-node outcome chips | info (none out of box) |
| completed | noop | verified_empty | `complete` (declared no-op) | none |
| (absent) | (absent) | n/a | legacy path, `unknown` | none |
| completed (agent) + exit≠0 | — | — | node `failed` (harness truth), elevation n/a | sandbox/harness code |

---

## 8. Migration / compatibility

1. **Legacy pipelines:** `status` already written; `outcome` absent → `unknown`, no heuristic flag, no alert. **A1 elevation is the one behavior change for legacy — flag-gated** with global kill-switch + per-pipeline opt-out + pre-flight report listing pipelines whose nodes self-report `status:failed` while exiting 0. Rollout: report → new pipelines → legacy after audit → remove flag. `status`→`agent_status` propagation in node_runner ships in the same phase (backward-compatible: reading a field that exists).
2. **Legacy error codes:** old runs keep raw codes; new writes mapped; §3.2 shared alias module serves `_retry_after_policy`, alert matcher, event_mapper — each with regression tests.
3. **Alert rules:** explicit user rules beat defaults; migration report lists behavior changes.
4. **Custom schemas:** custom-schema fail-the-run for `output_json` only; base fields extracted independently.
5. **New statuses (`superseded`, `claimed` listed, `waiting_for_lock`/`expired` purged):** **Single shared status-enum module** consumed by run.py, run_ws.py, mcp_server.py, cost probe, crud/run.py (incl. the hardcoded `completed_at` terminal tuples at :592/:635/:723 AND the analytics status-count tuples at :1140/:1200 — purging `expired` there), retention/purge, org deletion, saq_hooks `_mark_run_failed` guard (`NOT IN` set), notification log. Postgres CHECK constraint `ck_runs_status` (currently the 10-value set — `pending`, `running`, `awaiting_human`, `claimed`, `complete`, `failed`, `cancelled`, `eval_failed`, `stalled` [0077], `budget_exceeded` [0090]) needs drop/re-add to ADD `superseded` (precedent 0077_add_stalled_status). Rollback rule: legacy readers treat unknown status as terminal. `executor_superseded`→`superseded` is PRESENTATION-LAYER — stored rows NOT mutated.
6. **Retry policy:** default is explicit + code-filtered; explicit author policies win; save-time validation warning; SAQ budget composition documented; retry spawns NEW attempt rows (migration: add `parent_attempt_id` column; backfill existing retried runs' attempts as best-effort, default null).
7. **Rollout ordering:** (a) shared status-enum module + registry + alias table, (b) `agent_status`/`agent_outcome` propagation in node_runner + A1 elevation (flag-gated, pre-flight), (c) verdict header + guidance + banners (frontend), (d) `superseded` status + CHECK migration + new-attempt-row retry, (e) schema fail-the-run (opt-in legacy, default new), (f) truthfulness heuristic (dry-run → opt-in → on), (g) accept-as-complete (human-only gate + verdict record).
8. **API/MCP surface:** additive-only for one cycle; keep legacy status/error_code fields alongside new; publish status/code migration table; then remove legacy.
9. **Schema diff (v3 addition):** `runs`: add `parent_attempt_id UUID NULL`, `work_intact BOOL NULL`, `accepted_as_complete BOOL NULL`, `accepted_by VARCHAR NULL`, `superseder_run_id UUID NULL`; node outputs: `agent_status`/`agent_outcome` in the node artifact JSON (no new table); CHECK `ck_runs_status` widened with `superseded`; historical rows default `work_intact=null` (never retro-false), `accepted_as_complete=null`.

---

## 9. Summary of opinionated decisions (v3)

1. `status` = harness truth (run row); agent `status` = work truth (node contract); node_runner propagates agent `status`/`outcome` verbatim (no rename); exit code stays harness truth for node status.
2. ONE taxonomy — the error-code registry; class is a tag.
3. Dotted registry codes; shared alias module for `_retry_after_policy` + alert matcher + event_mapper; write-time mapping; legacy rows untouched.
4. A1 elevation unconditional on `agent_status=failed OR outcome=failed`; flag-gated for legacy with pre-flight report; never a self-declared failure landing `complete`.
5. Retry = NEW attempt row; terminal rows immutable; supersession guard cancels pending retries.
6. Truthfulness heuristic: tri-state evidence (verified_empty/has_work/unverifiable), captured pre-teardown, off critical path; `unverifiable` never flags; fires only on explicit `outcome:success` + verified_empty; `outcome:"noop"` declares intended no-op; Warning severity.
7. Two banners primary UX; Accept-as-complete = human-only, presentation-layer verdict (never mutates status).
8. Alerts plug into existing notifier EVENT_* + AlertEngine + notification log; dedup (rule_id, fingerprint, run_id); org-wide correlation via rule engine; explicit user rules beat defaults.
9. Supersession never alerts alone; superseder watched; chronic-failure = analytics tile (not alert) in v1.
10. `partial` status deferred; `claimed` kept in active set; `waiting_for_lock`/`expired` purged.
11. Schema validation fails the run (§10); opt-in legacy, default new.
12. Fail-open terminalization (computation errors degrade to today's path; DB-down is today's H1 problem, sweep is backstop); terminalization atomic.
13. Explicit-author retry/alert config beats defaults (with save-time warnings); defaults code-filtered.

---

## 10. Schema validation precedence & enforcement

1. Base fields (`status`, `summary`) extracted + validated FIRST, independent of custom schema.
2. `output_json` validated against custom schema IF set; else free-form.
3. Violation of EITHER → node `failed` + `contract.schema` AND run terminal `failed` + `error_code=contract.schema` (kills the silent-pass #10).
4. Rollout: new pipelines ON by default; legacy custom-schema fail-the-run OFF for one deprecation cycle (`enforce_schema_fails_run=false`, warnings recorded, migration report auto-published before flip). Base-field strictness: `base_contract: "lenient"` opt-out degrades missing `status` to `unknown`, never false `complete`.
5. Graph validator `_check_sandbox_agent_config`: `output_schema_json` must be valid JSON Schema + base contract auto-merged so a custom schema can never shadow `status`/`summary`.

---

## 11. Acceptance tests, metrics, rollback gates

### 11.1 Per-incident acceptance tests (golden corpus — full 12 rows in §12)

Replay pipeline: fixture → mapping → elevation → banner-selector → alert-filter → retry-filter. **Every fixture carries `exit_code`** so the exit-0 A1 case is testable. Each incident has TWO fixtures: (a) true-replay of the historical artifact (asserts what the legacy artifact actually yields — e.g. #2 yields `complete`+`unknown` because the legacy driver self-reported success), and (b) post-fix artifact (driver writes `agent_status=failed`) asserting the elevation. This resolves the v2 "unreachable acceptance test" flaw: replay proves the mapping, the post-fix fixture proves the fix.
Key rows: exit-0 A1 elevation; legacy `status` compat (old `{"status":"failed"}` maps to `agent_status=failed`); `retryable=no` default floor + explicit-policy override; superseder-watch; fail-open under thrown subsystems; `agent.no_op` fires on #7 verified-empty and NOT on #2 (A1 case) or healthy no-ops; webhook payload poison-field assertion; machine-token 403 on Accept-as-complete. Healthy-run corpus (`tests/fixtures/failure_modes/healthy/`, ~20 fixtures) asserted not-flagged.

### 11.2 Metrics
Per-error-code daily distribution; `modulo_run_elevation_agent_failed_total`; `modulo_alerts_suppressed_total`; `modulo_heuristic_errors_total`; `modulo_heuristic_unverifiable_total`; `modulo_alert_delivery_failed_total`; accept-count by class; superseded-run count; fail-open (`harness.elevation_failed`) count. Each metric has a fires-when unit test.

### 11.3 Rollback gates
- A1 elevation: rollback if daily `agent_reported_failure` spikes >3× trailing-14-day baseline within 24h AND sampled audit shows >10% false elevations. Circuit-breaker: auto-disable elevation for a pipeline on sustained false-elevation.
- Truthfulness heuristic: rollback if sampled audit FP >5% of flagged runs verified as genuine work (sampling: weekly cron, N runs, human verdict stored in analytics-facts).
- Schema-strict: flip per-pipeline based on observed warnings (never a deterministic surprise flip); migration report published automatically.
- Superseded: presentation-layer, rollback-safe; revert the enum-consumer release together.

---

## 12. The 12-incident corpus (full enumeration)

| # | Incident (observed) | Legacy artifact/behaviour | Mapped error_code | Expected status (post-fix) | Banner | Alert |
|---|---|---|---|---|---|---|
| 1 | Stale DB conn at checkpoint write, work completed | `OperationalError` in `aput` after node completed | `harness.db.connection_lost` | `failed` + work_intact | false-failure | none until retry-exhaust |
| 2 | "Groomed 0/5", all sub-calls timed out, run "completed" | driver wrote status=completed, exit 0, zero processed | post-fix driver → `agent.failed` | `failed` (A1) | zero-work | critical |
| 3 | msgpack `TypeError` after completed node (RunEventBroker) | `TypeError` at checkpoint write | `harness.state_serialization` | `failed` + work_intact | false-failure | warning |
| 4 | `asyncio.wait_for` cancelling E2B SDK tasks | `NodeCancelledError` every run ~7s | `harness.sdk_task_cancelled` | `failed` | none (transient) | off until retry-exhaust |
| 5 | Sandbox no parseable output.json (exit 0) | `SandboxNodeFailedError` | `sandbox.no_output_json` | `failed` (retryable) | none | off until retry-exhaust |
| 6 | Idle watchdog stall (long sessions go silent) | `stall_reason` returned | `agent.stall` | `stalled` | stall callout | warning |
| 7 | Fabricated "improvement applied" (changed_files=[], pr_url="") | status=completed, exit 0, empty evidence | `agent.no_op` | `complete` + agent.no_op | false-success | warning (critical if PR-claim verifiably false — **warning-only in v1**, see §14.10) |
| 8 | Node timeout (600s too short) | `node_timeout` | `node.timeout` | `failed` | inline timeout suggestion | warning |
| 9 | Token runaway | `runaway` | `node.runaway` | `failed` | raise-budget CTA | warning |
| 10 | Schema validation failed but run completed | `output_schema_json` validation failed → failed artifact, complete run | `contract.schema` | `failed` | field-level errors | warning |
| 11 | Redundant app-side Merge Queue failing daily (OperationalError), nothing alerted | daily `failed` + `OperationalError` | `harness.db.connection_lost` | `failed` | none | chronic-failure tile + retry-exhaust alerts |
| 12 | Connector typed errors (invalid-key vs permission vs rate-limit vs network) | typed codes (#1088) | `connector.*` | `failed` | connector CTA | critical for auth, off for transient |

---

## 13. v4 amendments (resolves plan-review-iterate iteration-3 findings)

The following AMEND the sections above. Where a line conflicts with an earlier section, the v4 amendment wins.

### 13.1 Retry grouping — reuse existing run-chain columns (resolves L2-C1, L4-1, L4-2, L5-4)

- **REUSE the existing `parent_run_id` FK + `work_item_id` journey anchor instead of a new `parent_attempt_id` column.** `runs` already has `parent_run_id UUID NULL` (FK, indexed, migration 0003) used for child-run chaining. Attempts are the same shape: attempt rows link via `parent_run_id` to the root; `run_group_id` is DERIVED as the root run id (walk `parent_run_id` to root). No new column, no chain-walk O(depth) aggregation: add an index on `parent_run_id` (exists) and define `run_group_id` as the root id.
- **`langgraph_thread_id` continuity:** a new attempt row MUST inherit the parent's `langgraph_thread_id` so Resume-after-stall keeps its checkpoint. The current `unique=True` on `langgraph_thread_id` blocks this — relax to `unique=True` over `(run_group_id, thread_id)` (a unique composite), migration included. Checkpoints keyed by thread_id are then shared across attempts as intended. **REVOKED by v6 §14.1/§15.1** — thread inheritance is Resume-only; retries get a fresh thread.
- **Retry is a run-grouping layer over the EXISTING fenced same-row reset, not a replacement.** Keep today's fenced `UPDATE runs SET status='pending'` (claim-token-guarded) as the inner mechanism for short-transient SAQ-level retries; add run-grouping ONLY for the policy-driven "new attempt" path: insert a child row (`parent_run_id=root`, inherited thread_id) and reset the parent to terminal `superseded`-equivalent (or leave parent terminal and treat the child as the continuation). `attempt_n` is the child's ordinal within `run_group_id` (derived, not stored). `node_attempt_count`/`claim_count` budgets stay root-scoped so `max_retries:1` cannot retry forever across attempts.
- **Idempotency:** Re-run is guarded by a token (reuse the existing `cancellation_requested`/claim machinery pattern); double-click cannot spawn two children for the same root+trigger.
- **Analytics:** facts aggregate by `run_group_id` (logical run), with per-attempt rows preserved for detail. Dashboard "last run" = latest attempt of the group. Success/failure/cost-per-success use the group verdict (complete if ANY attempt completed; else the final terminal).

### 13.2 `is_success` is a DERIVED expression, not a terminalization-computed column (resolves L2-C2, L3-3)

- `is_success` = SQL expression `(status = 'complete' OR accepted_as_complete IS TRUE)` recomputed on demand (a view or helper), NEVER a stored column — acceptance happens after terminalization, so a stored column computed at terminalization would always be NULL for accepted runs (v3 flaw).
- Enumerate every consumer that must switch from `status = 'complete'` to `is_success`: dashboard `RunDailyFact.status == "complete"`, `crud/run.py:1128` success_count, `:1198` SQL CASE, analytics-facts loaders, cost-per-success, week-over-week. Swept through the shared status-enum module (§8.5) — the sweep list now includes the STATUS-PREDICATE consumers, not just status-value consumers.

### 13.3 Evidence write-ordering — post-commit, never on the critical path (resolves L2-C3, L5-3, L2-11)

- Terminalization commits `work_intact=NULL` + the terminal status in the single atomic transaction. The evidence probe runs AFTER commit, asynchronously, bounded to ≤3s per probe, gated to nodes that DECLARED `outcome:success` (legacy/unknown-outcome nodes skip the probe entirely — where the flag can't fire anyway, saving cost).
- Evidence writes to a side table `run_evidence (run_id, node_id, evidence_state, evidence_detail, evidence_written_at)`; the banner renders from the side table. A reconciliation sweep fills `work_intact`/evidence rows that missed the async write.
- **Crash window handled explicitly:** a run terminal with `work_intact=NULL` and no evidence row renders "work could not be verified" (muted) — never a silent pass, never a no_op flag.
- Metrics: `modulo_heuristic_probe_latency`, `modulo_heuristic_probe_cost`, `modulo_heuristic_unverifiable_total`; health alert when unverifiable rate >20% of eligible runs over a window (resolves L5-8's systematic-degradation risk).

### 13.4 Seeded default alert rules — idempotent, user-editable (resolves L2-C4, L7-7)

- Seed ONCE per org, guarded by `seeded_defaults_version` (settings table). Upsert by `(org, signal)`: re-seed only ADDS missing signals, never touches edited or deleted ones.
- `is_default=true` flips to `false` on first user edit; deleted defaults stay deleted. "Restore defaults" button re-adds missing signals only.
- **Resolution algorithm:** user rules (`is_default=false`) beat seeded defaults; within a class, most-specific signal wins; a user-disabled default stays disabled. Migration report lists behavior changes.
- Seeded rules are first-class configurable rows in the alert-rules UI (not hidden).

### 13.5 Driver contract updates are an explicit deliverable (resolves L1-1, L1-2)

Add to §8 rollout as a prerequisite for A1 elevation + heuristic phases:
- `backlog-groomer.py`: already writes `status=failed` on sub-call exhaustion (shipped in devtools 9479fc9). Verify + test.
- `ticket-picker.py`, `ticket-to-pr-coder.py`: already write `status`/`outcome` (shipped). Verify.
- `codebase-improver.py` and any other success-claiming driver: declare `outcome:"success"` when claiming completion, so the no_op heuristic has its gate input.
- §11.1: #7 gets the SAME dual-fixture treatment as #2 — (a) true-replay of the legacy artifact asserting `complete`+`unknown` (honest baseline), (b) post-fix fixture (driver declares `outcome:success`, empty evidence) asserting `agent.no_op`.
- **`outcome` enum corrected:** `"success" | "partial" | "failed" | "noop"` — add `noop` to §7.1 so the escape hatch is expressible and schema-valid; `noop` bypasses schema fail-the-run and never triggers A1 or no_op.

### 13.6 A1 elevation — exit-code independence + retry-alert deferral (resolves L1-4, L2-6, L7-3)

- **Truth-table addition:** `agent_status=failed` + ANY exit code (0 or ≠0) → A1 elevation fires. Run `error_code=agent.failed` (critical alert) with the crash code (`sandbox.*`/`harness.*`) preserved in `error_detail`. Node status remains harness-truth from exit code; elevation is orthogonal. §7.4 row 10 rewritten: elevation n/a only when `agent_status=completed`.
- **Retry-alert deferral:** an explicit author policy retrying `agent.failed` defers the critical alert until retries exhaust (attempt 2 succeeds → no alert; fails → critical with `attempt_n`). Aligns §5.3.1 with §3.3.
- **"NEVER lands complete" is attempt-scoped:** a retry attempt may land `complete` while the run group's FIRST attempt stays `failed`; the group verdict (§13.1) is what consumers read.

### 13.7 Superseded — written status for new rows, presentation for legacy, reversible CHECK (resolves L2-7, L4-3, L5-7)

- **New runs WRITE `superseded`** (post-release). Legacy rows keep `executor_superseded`/`failed` and RENDER via the registry mapping. Both representations coexist deliberately; consumers use the shared status-enum module.
- **CHECK downgrade is explicit:** the migration to add `superseded` to `ck_runs_status` ships with a paired downgrade that FIRST converts `superseded` rows (→ `cancelled`, retaining `superseder_run_id`) and THEN re-applies the narrowed CHECK. Revert gate: "zero superseded rows written" count check before reverting (L5-7). **AMENDED by v6 §14.8** — the downgrade does NOT convert rows; the revert gate asserts zero `superseded` rows before re-narrowing; superseded rows are never mutated.
- **Superseder chains:** watch follows `superseder_run_id` to the LEAF; alert only on the leaf's terminal failure. Chain time-box = 24h (configurable); a chain that never terminalizes within the box is swept to `superseded` + a `superseder_watch_timeout` note.
- Add `superseded` to the stale-run sweep and retention/purge candidate list with a retention policy (L6-7).

### 13.8 Alerting bridge — ErrorEvent ingestion + late-write quarantine (resolves L4-4, L6-1, L6-2)

- **Bridge specified:** each terminal signal BOTH (a) dispatches via `Notifier.dispatch_event(EVENT_*)` (new `EVENT_AGENT_FAILED`, `EVENT_AGENT_NO_OP`, `EVENT_RUN_SUPERSEDED`) through event_mapper into `notification_delivery_log`, AND (b) is ingested as an `ErrorEvent` with defined level+fingerprint so the seeded `ErrorNotificationRule` rows can fire via the existing AlertEngine. §5.2's webhook schema is the notification-log webhook payload.
- **Dedup:** keep the existing cross-run cooldown per `(rule_id, fingerprint, pipeline_id)` SEPARATE from the per-run enumeration `(rule_id, fingerprint, run_id)` — the cross-run cooldown prevents the 5-emails/day storm (L2-12); the per-run key keeps distinct runs distinct in the enumeration.
- **Org-wide correlation cut from v1** (L3-7): no corpus incident is a fleet outage; the dashboard tile + retry-exhaust alerts cover #11. Remove §5.3.3 for v1.

### 13.9 S1→A2 chain — late-write quarantine (resolves L6-1)

- **Stall wins the race:** once the idle watchdog terminalizes a node/run as `stalled`, ANY later output write is QUARANTINED: recorded as `harness.late_write` (metadata only), excluded from the terminal row, excluded from inherited resume checkpoints unless explicitly adopted by a human. A fabricated completion arriving after stall cannot re-open the run or ride the resume path.
- Acceptance test: node writes artifact after watchdog trip → run stays `stalled`, artifact quarantined, resume checkpoint excludes it.

### 13.10 HITL transitions + watchdog exemption (resolves L6-3)

- **HITL transition table:** gate approve → run continues (`running`); gate reject → `cancelled` (with `rejected` detail); gate expire → stays `awaiting_human` with `claim_expired` note (existing behavior; the run row is NOT terminalized by expiry alone); supersede-while-gated → gate auto-cancelled, run lands `superseded`.
- **Stall watchdog EXEMPT on `awaiting_human`/`claimed`:** the idle watchdog must not fire on a paused run (every gate would stall). Add the exemption + test.

### 13.11 Edge-case matrix (resolves L6-6, L4-7, L6-8)

| Input shape | Status | error_code | Acceptance |
|---|---|---|---|
| node output not a dict (string/array/null) | `failed` | `contract.no_output` | test |
| `status` present, `summary` missing, `status=completed` | `failed` | `contract.schema` (fail-the-run) | test; legacy drivers already write both |
| `status` present, `summary` missing, `status=failed` | `failed` | `agent.failed` (elevation; summary not required for failure) | test |
| `outcome` present, `status` absent, `outcome=failed` | `failed` | `agent.failed` (A1 fires on outcome=failed alone) | test |
| `outcome` present, `status` absent, `outcome=success` | `complete` + `unknown` (no no_op — status required for the gate) | none | test |
| empty pipeline graph / no nodes | `failed` | `config.invalid` | test |
| `skipped` node output (prompt-undefined path, node_runner:1119) | node verdict chip `skipped` | — | add to §4.1 verdict vocabulary |

### 13.12 Schema diff completed (resolves L6-4)

- `runs`: `accepted_at TIMESTAMP NULL`, `superseder_run_id UUID NULL` (FK, indexed), `run_group_id UUID NULL` (indexed, set = root id), `langgraph_thread_id` unique relaxed to composite `(run_group_id, thread_id)`; CHECK `ck_runs_status` (10 values incl. `stalled` [0077] and `budget_exceeded` [0090]) widened with `superseded`.
- New table `run_evidence (run_id FK, node_id, evidence_state, evidence_detail, evidence_written_at)`.
- New table `run_verdict (run_id FK, verdict_type, verdict_by, verdict_at, work_intact_snapshot, recovery_action)` — the hitl_manager-owned Accept-as-complete record (§4.4); `runs.accepted_as_complete`/`accepted_by` are denormalized from it.
- `attempt_n` derived (ordinal within run_group_id), not stored; `parent_run_id` reused for chaining.
- Backfill: historical runs `run_group_id = id` (self-root), `work_intact = NULL` (never retro-false), `accepted_as_complete = NULL`.

### 13.13 Rollout-flag retirement + config-surface table (resolves L6-5)

- **Retirement mechanism:** a scheduled job flips each rollout flag after its deprecation cycle and auto-publishes the migration report (no manual audit); flags self-retire on a deadline with a canary. **`[REMOVED in v4]`** — replaced with a manual rollout-checklist step.
- **Config-surface table** (§0.5): every default classified code-constant (alert severities, dedup window, probe ≤3s) vs env-tunable (rollout flags, rollback thresholds) vs seeded-rule (alert defaults). New config surfaces are MINIMIZED: the only new knobs are the rollout flags (self-retiring) and the seeded-rule set.

### 13.14 Metrics + rollback gates completed (resolves L5-5, L6-9, L7-10, L7-11)

- Add: `modulo_agent_noop_total` (fires-when), `modulo_false_failure_total` (work_intact=true terminalizations), `modulo_superseder_watch_total`, `modulo_heuristic_probe_latency`/`cost`, **recovery-rate audit** — per `retryable=no` code, manual re-run success rate; if >60% over N samples, flag for promotion (closes the permanent-failure sink, L5-6). **`[REMOVED in v4]`** — recovery-rate audit deferred to follow-up.
- **A1 rollback gate hardened:** trip if `elevation_count > 5` AND `elevation_rate > 3× trailing-14-day baseline` within 24h (absolute floor fixes the 3×-of-zero undefined case) AND sampled audit >10% false. **Automated fast-path:** elevation rate >10% of a pipeline's runs in 1h → auto-disable that pipeline's elevation (audit later), + a per-pipeline `agent.failed` drift monitor (baseline + monotonic increase) as warning. **`[REMOVED in v4]`** — per-pipeline drift monitor folded into the fast-path threshold.
- **EvidenceProvider seam:** §11.1 defines an injected `EvidenceProvider` protocol (`git_diff_empty`, `pr_exists`, `auth_probe`, `sandbox_filesystem`) with a fake used by the replay pipeline; probe timeout/exception/no-repo → `unverifiable` rows added to acceptance (L7-1, L7-2).
- **Accept-as-complete enforcement test hardened:** reject any request whose `auth_principal` came from the API-key/MCP path REGARDLESS of role (L5-1) — test with an OPERATOR-role machine token, not just runner.
- **Banner e2e parity:** dev-mode synthetic-run injection renders both banners + the muted unverifiable notice; Playwright asserts Accept-as-complete renders only for browser sessions (L7-11).
- **Webhook poison-field:** schema-strict `additionalProperties:false`; CRLF/header-injection assertion on `run_url`; agent-provided text NEVER enters webhook/email templates (render only in escaped run-detail) (L5-2).

### 13.15 Scope confirmation (resolves L3-1, L3-2, L3-4, L3-5, L3-6, L3-7)

Honest restatement of cuts vs relabels: genuinely cut — chronic-failure ALERT (now analytics tile derived from the existing daily-facts table, no incremental counter: badge = ≥N same-code failures in a trailing window), org-wide correlation (§5.3.3 removed), live PR-existence check (removed — warning-only no_op for v1; PR-claim fabrication has no corpus incident), in-memory dedup fallback (removed — Redis only). Retained because load-bearing for observed incidents: tri-state evidence (for #7), pre-teardown capture (now post-commit async — cost only on declared-success nodes), zero-volume cross-check (minimal, folded into verified_empty). §11 trimmed: healthy corpus 6–8 fixtures (three aligned conditions required to false-flag), heuristic FP-sampling weekly cron kept (it is the only way to measure the load-bearing detector's precision), A1 sampled-audit replaced by the automated fast-path (13.14). The remaining surface is proportionate to 12 observed incidents.

### 13.16 Final acceptance summary (resolves L1-3, L6-2, L7-2, L7-8, L7-9)

The §11.1 acceptance program now includes: all 12 incidents (dual-fixture for #2/#7, single-fixture for machine-detected #1/3-6/8-12), exit-code pinned per row, `(failed+exit 0)` AND `(failed+exit≠0)` A1 rows, legacy `status:"failed"` compat fixture, missing-status→unknown, flag-off→no-elevation, retry lifecycle (parent_run_id, supersession cancel, idempotency, attempt history), retryable floor + override parameterized + save-time warning, alert precedence (user-disables-default), superseder three scenarios, fail-open exception-injection, rollback-gate measurability (baseline window + floor), banner e2e + machine-403.

---

## 14. v5 amendments (resolves plan-review-iterate iteration-4 findings)

Where a line conflicts with any earlier section, the v5 amendment wins. This section resolves the remaining design breaks and internal contradictions; the base architecture (single taxonomy, A1 elevation, tri-state evidence, superseded, fail-open, schema-fails-run) is confirmed by all lenses and unchanged.

### 14.1 Thread identity: ONLY Resume inherits the thread; retries get a fresh thread (resolves L2-C1/C2, L4-2)

- **Root fact:** `langgraph_thread_id = f"{org_id}:{run_id}"` (crud/run.py:341) embeds the run id — two sibling attempts can never share a thread id, so the v4 "inherit thread for all attempts" was geometrically impossible.
- **Correct model — two sub-paths:**
  - **Retry (policy-driven new attempt):** fresh thread id (`f"{org}:{child_run_id}"`). Runs the graph CLEAN. No checkpoint inheritance — a retry of `agent.failed` restarts, never resumes mid-graph. Unique stays `langgraph_thread_id` (already unique because it embeds the run id). NO composite-unique relaxation needed — v4's §13.1 relaxation is REVOKED.
  - **Resume (stall recovery):** new run row, SAME `langgraph_thread_id` as the stalled parent (the resume sub-path is the ONLY inheritor). `langgraph_thread_id` unique is relaxed to `(run_group_kind='resume', thread_id)` via a partial unique index (Postgres `WHERE run_group_kind='resume'`), so a stalled row + its resume successor share the thread while every other run keeps a globally unique thread. `PipelineExecutor.resume` (executor.py:1166) continues from the checkpoint as today.
- **Doc fixes:** §2.3.5's "replaces today's UPDATE" and §13.1's "KEEP the fenced reset" are reconciled: the fenced same-row reset remains for SAQ short-transient retries; policy-driven retries insert child rows; Resume inserts a child row with inherited thread. The retry model is ONE place: §14.1.

### 14.2 `run_group_kind` discriminator (resolves L2-C3, L3-1)

- Add `runs.run_group_kind VARCHAR NULL` — `'retry'` | `'resume'` | `'signal'` | `'correction'` | NULL (root).
- `run_group_id` is a STORED column (v4 §13.12 wins over §13.1's "no new column") set to the root run id at insert.
- **Grouping scope:** only `run_group_kind IN ('retry','resume')` rows are "attempts" for group-verdict, attempt_n, budget, and analytics aggregation. Signal children (`agent_signal`), correction runs (`feedback_manager`), and work-item chains are EXCLUDED from attempt semantics — they keep their existing child-run rollup behavior unchanged.
- `attempt_n` = ordinal of retry/resume rows within the group (derived via `run_group_kind` filter, not a chain walk over all children).
- `node_attempt_count`/`claim_count` budgets stay root-scoped and count retry/resume attempts only.

### 14.3 Group verdict — no false-success from no-op attempts (resolves L1-1, L1-2)

- **Per-attempt `is_success` is the ONLY success predicate** (v4 §13.2 derived expression `(status='complete' OR accepted_as_complete IS TRUE)`), shared by group verdict, analytics, cost-per-success, and dashboards.
- **Group verdict rule:** `complete` iff ANY attempt has `is_success=true` AND its evidence_state ≠ `verified_empty` AND its `outcome` ≠ `noop`. Otherwise: the group's terminal = the most informative terminal among attempts (worst-wins: any `failed` → `failed`; else any `stalled` → `stalled`; else `complete`+no-op aggregation → surfaces as no-op with the false-success banner).
- A group whose only completed attempt was a no-op aggregates as no-op, never success. A group whose completing attempt was `failed`+accepted-as-complete aggregates as success (matches `is_success`).

### 14.4 A1 elevation — crash class wins severity for retryable codes (resolves L1-3)

- A1 elevation always sets run status `failed` (never `complete`) on `agent_status=failed OR outcome=failed`.
- **But run `error_code`/alert severity are chosen by precedence:** if the node's harness-truth code is a retryable transient (`sandbox.*`, `harness.sdk_task_cancelled`, `harness.worker_failed`, transient `connector.*`), the CRASH CLASS wins the error_code and severity (deferred to retry-exhaust); `agent.failed` is preserved in `error_detail`. This prevents incident #4 (SDK cancellation every ~7s) from firing critical `agent.failed` — the harness fault is the truth.
- If the harness-truth code is NOT retryable-transient (e.g. the node genuinely completed with exit 0, or a permanent harness code), `agent.failed` wins as critical.

### 14.5 Retry-alert deferral — compensating signals (resolves L2-4)

- **Retry cancelled by supersession:** the deferred critical alert FIRES at cancellation with `attempt_n` + reason `superseded` — a critical agent.failed is never silently lost.
- **Retry succeeds:** no critical alert; emit a low-severity notification `agent.failed then succeeded on retry` (visible in notification log; not a page).
- **Parent status on retry:** the failed attempt row stays `failed` (attempt 1 remains `failed` for the audit trail); the GROUP verdict (14.3) reflects the retry outcome. No "superseded-equivalent" parent mutation.

### 14.6 `work_intact` computed at terminalization, probe only for no-op (resolves L2-5, L6-2)

- **`work_intact` comes from completed-node artifacts at terminalization** (§2.3.2's own definition: all completed nodes have valid artifacts AND full DAG ran) — NOT from the async evidence probe. This restores the false-failure banner + Accept-as-complete for harness-crash incidents #1/#3 (the node that crashed has no output, but SIBLING completed nodes prove work).
- The async evidence probe (§13.3) is used ONLY for the `agent.no_op` flag (declared `outcome:success` nodes). The reconciliation sweep backfills no_op evidence; it does NOT attempt git-evidence recovery post-teardown (that is permanently unverifiable and renders the muted notice).
- Supersession mid-execution: sandbox torn down via the executor cancel path; node-cancel codes recorded as node detail only; elevation suppressed on superseded-cancelled nodes; run lands `superseded`.

### 14.7 Seeded-rule tombstones + version semantics (resolves L2-6)

- Add `deleted_defaults (org_id, signal)` tombstone table.
- **Re-seed** (on `seeded_defaults_version` bump): upsert by `(org, signal)`; ADDS missing signals; never touches edited (`is_default=false`) or tombstoned signals.
- **Restore defaults:** re-adds signals that are missing AND not tombstoned. A deliberately-deleted default stays deleted until an explicit "restore this rule" action clears the tombstone.
- **Version bump semantics:** a bump force-updates only rows still `is_default=true` and never edited; edited rows keep the user's version.

### 14.8 Superseded downgrade keeps rows (resolves L2-7)

- The CHECK downgrade does NOT convert `superseded` rows to `cancelled` — that violated immutability (§0.6) and erased the superseded distinction. The revert gate asserts zero `superseded` rows before re-narrowing the CHECK (per v4 §13.7); if rows exist, the CHECK stays widened. Superseded rows are never mutated.

### 14.9 Alerting — single evaluator, single cooldown (resolves L3-3)

- **AlertEngine is the SOLE rule evaluator and dedup owner.** The `EVENT_*`/notifier leg is ONLY the delivery sink (event_mapper templates → notification_delivery_log → webhook/email/in-app). Every signal ingests as an ErrorEvent (level+fingerprint) → AlertEngine evaluates seeded/user rules → a matched rule dispatches once through the notifier.
- One cooldown key family: `(rule_id, fingerprint)` for cross-run suppression + `(rule_id, fingerprint, run_id)` for per-run enumeration; the webhook payload carries both `alert_id` (per delivery) and `group_id` (per rule+fingerprint). No dual dispatch path.

### 14.10 Cut-list applied (resolves L3-2, L3-6)

- **Strike the PR-claim-fabrication escalation** from §7.2.5 and §12 row 7 → `agent.no_op` is warning-only for v1 (the live PR-existence check is removed; there is no evidence source for critical escalation).
- **Drop for v1:** flag-retirement scheduled job (replace with a manual rollout-checklist step), per-pipeline drift monitor (fold into the fast-path threshold), recovery-rate audit (defer to follow-up), `seeded_defaults_version` column (idempotent upsert + tombstones suffice), reconciliation sweep for no_op evidence (side table + unverifiable health alert + muted notice cover it).
- Propagate removals to the body: §5.3.3 org-wide correlation → `[REMOVED in v4]`; §5's in-memory dedup fallback → removed (Redis only); §5.1 chronic-failure wording → "trailing-window count over daily-facts".

### 14.11 Config-surface table (resolves L6-3)

| Setting | Value | Classification |
|---|---|---|
| Alert severities | §5.1 matrix | code-constant (seeded rules, user-editable) |
| Dedup window | 6h | code-constant |
| Cross-run cooldown | `(rule_id, fingerprint)` | code-constant |
| Evidence probe bound | ≤3s | code-constant (env-tunable in debug) |
| Rollout flags (A1, heuristic, schema, base_contract) | off/on | env-tunable, manual rollout checklist |
| Rollback thresholds | floor >5, >3× 14d baseline, fast-path >10%/1h | env-tunable |
| Superseder chain time-box | 24h | env-tunable |
| Chronic-failure badge N | 3 | code-constant |
| suggested_timeout formula | max(2×elapsed_to_stall, timeout+300) | code-constant |
| Default retry | `{"on":["timeout","failure"],"max_retries":1}` | code-constant (pipeline-overridable) |

### 14.12 Edge-case additions — invalid enums + evidence probe outcomes (resolves L6-4, L7-2)

Add to §13.11 edge matrix:

| Input shape | Status | error_code | Acceptance |
|---|---|---|---|
| `status` non-enum value (`"SUCCESS"`, `"done"`, boolean) | `failed` | `contract.schema` (fail-the-run) | test |
| `outcome` non-enum value | `failed` | `contract.schema` | test |
| `summary` non-string | `failed` | `contract.schema` | test |
| probe timeout | `complete` + `unknown`, muted "could not verify" | none (log `heuristic.unverifiable`) | test |
| probe raises | `complete` + `unknown`, muted | none | test |
| no git repo available | `complete` + `unknown`, muted | none | test |

### 14.13 Truth table reissued with status-absent rows + gate condition (resolves L6-5)

- §7.2 gate condition explicitly: fires ONLY when `status` is present AND `outcome == "success"` AND evidence=verified_empty.
- §7.4 additions: `status absent, outcome=failed` → A1 fires (run `failed`); `status absent, outcome=success` → `complete` + `unknown`, no no_op.
- A1 predicate unchanged: `agent_status == "failed" OR outcome == "failed"` — outcome alone can trigger A1 (as the §7.4 row already stated); the no_op gate additionally requires status present.

### 14.14 EvidenceProvider protocol spec (resolves L7-1)

```python
class EvidenceProvider(Protocol):
    def git_diff_empty(self, run_id, node_id) -> EvidenceResult: ...
    def sandbox_filesystem_probe(self, run_id, node_id) -> EvidenceResult: ...
# EvidenceResult = "has_work" | "verified_empty" | "unverifiable"
# timeout, any exception, and no-repo all map to "unverifiable"
```
- `FakeEvidenceProvider` fixture in `tests/fixtures/failure_modes/` with canned timeout/exception/no-repo/has-work/verified-empty responses, injected into the §11.1 replay pipeline. The mapping `{probe result → evidence_state}` is documented in the protocol module.

### 14.15 Acceptance program additions (resolves L7-3/4/5/8)

- **Retry lifecycle fires-when matrix:** parent_run_id + run_group_kind set on insert; short-transient SAQ retry uses fenced reset; policy retry inserts child; double-click idempotency (second click rejected); thread fresh on retry, inherited on resume; group verdict aggregation (no-op attempt does not make group success).
- **is_success tests:** accept post-terminalization → re-derive → success_count/cost-per-success move; import-linter rule forbidding raw `status='complete'` predicates outside the shared status-enum module; verdict→denormalized-column sync test.
- **Seeded-rule tests:** re-seed after edit (edited untouched), re-seed after delete (stays deleted via tombstone), restore-defaults (missing+non-tombstoned only).
- **Rollback-gate fires-when:** floor + baseline trip; fast-path auto-disable leaves auditable record. **Internal contradictions resolved:** §14.14's sampled-audit question is answered — the hardened gate uses (floor AND rate AND fast-path); the sampled audit is a separate weekly precision check, NOT an AND condition of the automated gate. Retry model unified in §14.1.

### 14.16 Documentation impact (resolves L6-6)

- PRD: run-status section (add `superseded`; document `status`=harness truth vs agent `status`=work truth), error-code vocabulary section.
- API reference: run response fields (`error_code`, `run_group_id`, `run_group_kind`, `attempt_n`, `superseder_run_id`, `accepted_as_complete`, `work_intact`), new endpoints (accept-as-complete, evidence/verdict resources).
- MCP: `get_run_status` detail fields, `list_runs` filters (superseded, run_group_kind), `review_hitl` surface for terminal-run verdicts.
- Node-chip vocabulary: add `skipped` (node_runner:1119 prompt-undefined path); `delivered_manual` defined as `interrupted` + result=delivered_manual.

### 14.17 Metrics additions (resolves L6-7, L7-9)

- Add `modulo_late_write_quarantined_total`, `modulo_hitl_gate_rejected_total`, `modulo_hitl_gate_expired_total`, `modulo_superseder_watch_timeout_total`, `modulo_agent_noop_total`, `modulo_false_failure_total`, `modulo_superseder_watch_total`, `modulo_heuristic_probe_latency`/`cost` — each with a fires-when unit test; probe-latency fires-when asserts the ≤3s wait_for bound and emission on the success path.

### 14.18 Resource lifecycle (resolves L6-2, L6-8)

- Supersession → executor cancel path tears down the sandbox, records node-cancel as node detail, suppresses elevation, lands `superseded`.
- Cancelled run with in-flight nodes → same teardown; partial artifacts retained in node records for evidence/attempt history.
- Stale-run sweep consumes the shared status-enum module and handles `superseded`/`stalled`/`awaiting_human`/`pending`-retry children explicitly.
- Superseded retention: retained 30 days then purged to an audit record preserving `superseder_run_id` (env-tunable).

---

## 15. v6 — FINAL CONSOLIDATED SPEC (supersedes §13 and §14)

This is the single implementable statement. All of §13/§14 remain as design history; wherever any earlier line conflicts with this section, v6 wins. The base architecture (§0–§12) is confirmed: single error-code taxonomy, A1 elevation, tri-state evidence, `superseded`, fail-open terminalization, schema-fails-run.

### 15.1 Run grouping, retry, and resume (one coherent model)

**Columns:** `runs.run_group_id UUID NULL` (stored, = root run id), `runs.run_group_kind VARCHAR NULL` — values `'retry'` | `'resume'` | NULL (root / signal / correction / work-item children). `attempt_n` derived = ordinal of `run_group_kind IN ('retry','resume')` rows within the group; the ROOT is attempt 1 (a root-only run has exactly one attempt: itself). Signal/correction/work-item children (`trigger_type` distinguishes them today) keep `run_group_kind=NULL` and their existing child-run rollup behavior unchanged — no consumer changes required; the new grouping logic simply filters `IN ('retry','resume')`.

**Thread model (corrected):**
- DROP the global `UNIQUE(langgraph_thread_id)` constraint (migration: `op.drop_constraint`, remove `unique=True` from the ORM).
- **Retry** = child row (`run_group_kind='retry'`), fresh thread `f"{org}:{child_run_id}"` (default `create_run` behavior; no override needed). Runs the graph CLEAN.
- **Resume** = child row (`run_group_kind='resume'`), `create_run(thread_id=<parent's thread string>)` override (new kwarg). Continues the parent's checkpoint.
- **Indexes (two partial unique indexes replace the global unique):**
  - `UNIQUE(parent_run_id) WHERE run_group_kind = 'resume'` — each parent admits at most ONE resume child (double-resume of the same stall → 409/DB reject); a resume child that stalls can itself be resumed (its parent is a different row) → chained resume works.
  - `UNIQUE(langgraph_thread_id) WHERE run_group_kind IS DISTINCT FROM 'resume'` — global thread uniqueness for roots/retries preserved.
- `_resume_run` reference corrected: the real method is `PipelineExecutor.resume` (executor.py:1166), which today re-claims the SAME row; the v6 child-row resume is a NEW path added alongside (SAQ `resume_run` → claim new child row → `resume` against inherited thread).
- **Fenced same-row reset stays** for SAQ short-transient retries; policy-driven retries insert child rows; Resume inserts child rows. One retry model, one place.

**Backfill (new, resolves the legacy hole):** walk `parent_run_id` for existing rows: `run_group_id` = true root (not self-root for children); `run_group_kind` = `'resume'` where a child shares its parent's `langgraph_thread_id` (evidence of a legacy resume), else NULL. A legacy stalled parent becomes resumable exactly once. Acceptance test: legacy parent resumed once succeeds, second resume rejected.

**Group verdict:** per-attempt `is_success` (single predicate below) is the ONLY success definition, shared by analytics, cost-per-success, dashboards, and the group. Group verdict = `complete` iff ANY attempt `is_success`; else the most informative terminal among attempts per registry guidance (any `failed` → `failed`; any `stalled` → `stalled`; `cancelled`/`superseded`/`eval_failed` → that terminal; else no-op aggregation with false-success banner). Root-only runs: the root IS the attempt.

### 15.2 The single success predicate

```
is_success = (status = 'complete' OR accepted_as_complete IS TRUE)
             AND (accepted_as_complete IS TRUE
                  OR (evidence_state IS DISTINCT FROM 'verified_empty'
                      AND outcome IS DISTINCT FROM 'noop'))
```

One expression, recomputed on demand (never stored; acceptance is post-terminalization). The group rule collapses to "complete iff ANY attempt is_success". Consumer sweep (switch from raw `status='complete'`): dashboard.py:167, crud/run.py:1128, :1138, :1198, analytics-facts loaders, cost-per-success, week-over-week — enforced by a **semgrep rule** (not import-linter; it cannot express SQL-predicate bans) forbidding raw `status='complete'` outside the shared status-enum module. `RunDailyFact` gains `run_group_id`.

### 15.3 Evidence & no-op detection

- `work_intact` computed at terminalization from completed-node artifacts (all completed nodes have valid artifacts AND full DAG ran). NOT from the async probe. Restores the false-failure banner for #1/#3.
- Async evidence probe: ONLY for the `agent.no_op` flag, ONLY on nodes that declared `outcome:"success"`, post-commit, bounded ≤3s, gated by the EvidenceProvider seam. `verified_empty` = git diff empty (whitespace ignored) AND no non-metadata `output_json` key; `has_work` = any positive; `unverifiable` = timeout/exception/no-repo (never flags, logs `heuristic.unverifiable`, renders muted notice).
- **Bounded reconciliation sweep KEPT** (one-shot retry for no-op-eligible runs that missed the async window — closes the crash-window detection hole; resolves the §14.6-vs-§14.10 contradiction by choosing keep). Side table `run_evidence` + `modulo_heuristic_unverifiable_total` health alert (>20% over window).
- EvidenceProvider protocol: `git_diff_empty(run_id, node_id) -> EvidenceResult`, `sandbox_filesystem_probe(run_id, node_id) -> EvidenceResult`; `EvidenceResult = "has_work"|"verified_empty"|"unverifiable"`. Connector auth is OUT of the protocol for v1 (the §5.1 connector-critical alert stays rule-based on the typed connector error; no probe method). Real-fixture tests for the concrete probes (tiny git repos: empty/whitespace/substantive; fs with/without content; no-repo; timeout) + FakeEvidenceProvider for consumer-pipeline tests.
- `auth_probe` is scoped OUT of EvidenceProvider v1 (see above); `pr_exists` stays out (no PR-claim fabrication escalation in v1 — `agent.no_op` is warning-only).

### 15.4 A1 elevation and severity

- Elevation: `agent_status == "failed" OR outcome == "failed"` on a captured node output → run NEVER lands `complete`. Fires regardless of exit code.
- **Severity precedence (corrected):** if the node's harness-truth code is a retryable transient (`sandbox.*`, `harness.sdk_task_cancelled`, `harness.worker_failed`, transient `connector.*`) → crash class wins run `error_code` + severity (deferred to retry-exhaust); `agent.failed` preserved in `error_detail` AND surfaced as an explicit `elevation_signal` field on the run/alert/webhook so user `agent.failed` alert rules and the banner still fire. **Escalation rule:** if a RETRIED attempt again self-reports `agent_status=failed`/`outcome=failed`, promote to critical `agent.failed` (repeated coincidence is the signal, not the harness). Otherwise non-retryable-transient → `agent.failed` critical.
- Resolution runs BEFORE `_retry_after_policy` (executor.py:1709) so retry and alert filters see the same code.

### 15.5 Retry-alert compensation (fire-once, resolvable)

- Retry cancelled by supersession → the deferred critical fires ONCE per `(run_group, signal)` with `attempt_n` + reason; fire-once guard prevents re-fire across a superseded chain.
- Retry succeeds → low-severity "agent.failed then succeeded on retry" notification (not a page).
- Superseding run terminalizes success → emit `alert_resolved` for the earlier critical (so a moot critical never stays open).

### 15.6 Seeded rules (tombstones + version marker)

- Settings row `seeded_defaults_version` (a single settings-row marker — not a per-rule column) triggers re-seed.
- Upsert by `(org, signal)`: adds missing signals; never touches edited (`is_default=false`) or tombstoned.
- `deleted_defaults (org_id, signal)` tombstone; Restore-defaults skips tombstoned; per-rule "restore" endpoint clears the tombstone (specified + tested).
- Version bump force-updates only rows still `is_default=true` and never edited.

### 15.7 Superseded

- New runs WRITE `superseded`; legacy rows keep `executor_superseded`/`failed` and render via the registry.
- CHECK `ck_runs_status` (10 values incl. `stalled` [0077] and `budget_exceeded` [0090]) widened with `superseded` (0077 precedent). Downgrade: does NOT convert rows; revert gate asserts zero `superseded` rows before re-narrowing; otherwise the CHECK stays widened.
- Superseder chains: watch follows `superseder_run_id` to the LEAF; alert only on the leaf's terminal failure; chain time-box 24h (env-tunable); timeout → swept to `superseded` + note.
- Mid-execution supersession: executor cancel path tears down the sandbox; node-cancel codes recorded as node detail; elevation suppressed; run lands `superseded`. Superseded retention 30 days then purge to audit record preserving `superseder_run_id`.

### 15.8 Alerting — single evaluator, per-signal ingestion table

AlertEngine is the SOLE evaluator; notifier EVENT_* is the delivery sink. Per-signal ingestion table (each signal → ErrorEvent level + stable fingerprint + existing ingestion consumer):

| Signal | ErrorEvent level | Fingerprint (stable) | Ingestion |
|---|---|---|---|
| `agent.failed` | critical | `agent.failed:{pipeline_id}` | new writer |
| `agent.no_op` | warning | `agent.no_op:{pipeline_id}` | new writer |
| `agent.stall` | warning | `agent.stall:{pipeline_id}` | new writer |
| `harness.*`/`sandbox.*`/`connector.*` transient | per class | `error_code:{pipeline_id}` | extend existing |
| `contract.schema` | warning | `contract.schema:{pipeline_id}` | new writer |
| `superseded` | none | — (never alerts; superseder-watch is a rule condition on the leaf) | — |

Fingerprints are STABLE per (error_code, pipeline) — run_id lives in event context, NOT the fingerprint, so windowed rules (`min_count>1`) still fire. Superseder-watch is expressed as an AlertEngine rule condition (leaf-failure), not a new mechanism. One cooldown family: `(rule_id, fingerprint)` cross-run + `(rule_id, fingerprint, run_id)` per-run enumeration. Webhook payload carries `alert_id` + `group_id` + `elevation_signal` + `attempt_n` + `run_group_id`.

### 15.9 Cuts — FINAL (applied to the body, not just declared)

REMOVED for v1 (and struck from the body text): org-wide correlation (§5.3.3/§9.8), in-memory dedup fallback (§5 preamble), PR-claim fabrication escalation (§7.2.5/§12 row 7 → `agent.no_op` warning-only), flag-retirement scheduled job (manual rollout-checklist step), per-pipeline drift monitor (folded into the fast-path threshold), recovery-rate audit (deferred follow-up), `seeded_defaults_version` as a per-rule column (now a settings-row marker, §15.6), reconciliation sweep for WORK_INTACT (kept only for no-op evidence, §15.3). Chronic-failure = trailing-window badge over daily-facts (no incremental counter).

### 15.10 Rollback gates (final)

- A1: `(elevation_count > 5 AND rate > 3× trailing-14d baseline) OR fast-path (rate > 10% of a pipeline's runs in 1h → auto-disable that pipeline, auditable record)` within 24h. Sampled audit is a SEPARATE weekly precision check, never an AND condition of the automated gate.
- Heuristic: sampled audit FP > 5% of flagged runs verified as genuine work → rollback (weekly cron, N runs, human verdict in analytics-facts).
- Schema-strict: per-pipeline flip based on observed warnings; migration report auto-published.
- Superseded: revert gate asserts zero `superseded` rows; CHECK stays widened otherwise.

### 15.11 Acceptance program (final, one list)

All 12 incidents (dual-fixture #2/#7; single-fixture machine-detected), exit_code pinned per row; `(failed+exit 0)` and `(failed+exit≠0)` A1 rows; crash-class-wins precedence rows (incident #4 → harness code + deferred alert; converse → agent.failed critical); legacy `status:"failed"` compat; missing-status→unknown; flag-off→no-elevation; thread model (retry fresh, resume inherits, double-resume rejected 409, resume-of-resume chain works, migration applies over existing DB with superseded rows); group verdict (root-only run = itself; no-op-only group → no-op; accepted-run group → success; evidence=verified_empty blocks success); is_success (accept post-terminalization → aggregates move; semgrep rule bans raw status='complete'; verdict→denormalized sync); seeded rules (re-seed after edit/delete, restore-with-tombstone, per-rule restore); rollback gates (floor+baseline trip, fast-path auto-disable, sampled audit not-AND); EvidenceProvider (fake + real-fixture probes + unverifiable rows); quarantine (stall-then-late-write; adoption via `adopt_late_write` endpoint writing `run_verdict`); HITL transitions (approve→running, reject→cancelled, expire→stays awaiting_human, supersede-while-gated→superseded, delivered_manual→running, claimed-abandonment timeout, watchdog exemption node-scoped); retry-alert compensation (fire-once, alert_resolved); banner e2e (dev-mode synthetic injection) + machine-403 (operator-role token) + webhook poison-field (additionalProperties:false, CRLF assertion, agent text escaped-only).

### 15.12 Schema diff (final, authoritative)

- `runs`: `run_group_id UUID NULL` (indexed), `run_group_kind VARCHAR NULL`, `superseder_run_id UUID NULL` (FK, indexed), `accepted_as_complete BOOL NULL`, `accepted_by VARCHAR NULL`, `accepted_at TIMESTAMP NULL`, `work_intact BOOL NULL`; DROP `UNIQUE(langgraph_thread_id)`; ADD partial `UNIQUE(parent_run_id) WHERE run_group_kind='resume'` + partial `UNIQUE(langgraph_thread_id) WHERE run_group_kind IS DISTINCT FROM 'resume'`; widen `ck_runs_status` (10 values incl. `stalled` [0077] and `budget_exceeded` [0090]) with `superseded`.
- New: `run_evidence (run_id FK, node_id, evidence_state, evidence_detail, evidence_written_at, UNIQUE(run_id, node_id))`; `run_verdict (run_id FK, verdict_type, verdict_by, verdict_at, work_intact_snapshot, recovery_action)` — canonical; `runs.accepted_*` denormalized from it in one atomic transaction via hitl_manager; `deleted_defaults (org_id, signal)`.
- `run_daily_facts`: add `run_group_id`.
- `attempt_n` derived (root=1, retry/resume increment); `parent_run_id` reused for chaining.
- Backfill: `run_group_id` = true root via parent walk; `run_group_kind` = 'resume' where child shares parent thread, else NULL; `work_intact=NULL` (never retro-false); `accepted_* = NULL`.

### 15.13 Config surface (final, exhaustive)

| Setting | Value | Classification |
|---|---|---|
| Alert severities | §5.1 matrix | seeded rules (user-editable) |
| Dedup window | 6h | code-constant |
| Cross-run cooldown | `(rule_id, fingerprint)` | code-constant |
| Evidence probe bound | ≤3s | code-constant |
| Rollout flags (A1, heuristic, schema, base_contract lenient, enforce_schema_fails_run) | off/on | env-tunable (manual rollout checklist) |
| Rollback thresholds (floor>5, >3× 14d, fast-path >10%/1h) | as §15.10 | env-tunable |
| Superseder chain time-box | 24h | env-tunable |
| Superseded retention | 30 days | env-tunable |
| Unverifiable-rate health alert | >20% | code-constant |
| Chronic-failure badge N | 3 | code-constant |
| suggested_timeout formula | max(2×elapsed_to_stall, timeout+300) | code-constant |
| Default retry | `{"on":["timeout","failure"],"max_retries":1}` | code-constant (pipeline-overridable) |

### 15.14 Metrics (final)

`modulo_run_elevation_agent_failed_total`, `modulo_agent_noop_total`, `modulo_false_failure_total`, `modulo_superseder_watch_total`, `modulo_superseder_watch_timeout_total`, `modulo_late_write_quarantined_total`, `modulo_hitl_gate_rejected_total`, `modulo_hitl_gate_expired_total`, `modulo_alerts_suppressed_total`, `modulo_heuristic_errors_total`, `modulo_heuristic_unverifiable_total`, `modulo_heuristic_probe_latency`, `modulo_heuristic_probe_cost`, `modulo_alert_delivery_failed_total`, `modulo_accept_complete_total`, `modulo_run_superseded_total`, `modulo_elevation_failed_total` — each with a fires-when unit test.

### 15.15 Documentation impact (final)

PRD run-status section (`superseded`; harness-truth vs work-truth), error-code vocabulary; API reference (new fields: `error_code`, `run_group_id`, `run_group_kind`, `attempt_n`, `superseder_run_id`, `accepted_as_complete`, `work_intact`, `elevation_signal`; new endpoints: accept-as-complete, adopt_late_write, run_verdict/run_evidence resources, per-rule restore); MCP (`get_run_status` detail, `list_runs` filters, `review_hitl` terminal-verdict surface); node-chip vocabulary adds `skipped` and `delivered_manual` (= `interrupted` + result).

### 15.16 Final scope statement

The design is proportionate to the 12 observed incidents. Load-bearing per incident: A1 elevation (#2/#7), tri-state evidence + no-op detector (#7), crash-class-wins severity (#4), false-failure banner + work_intact (#1/#3/#11), `superseded` + superseder-watch (#11), schema-fails-run (#10), typed codes (#12), stall + resume (#6), timeout/runaway (#8/#9), sandbox codes (#5). Everything else in §15 is the minimal supporting machinery. The document is implementable from §15 alone; §0–§14 are design history.
