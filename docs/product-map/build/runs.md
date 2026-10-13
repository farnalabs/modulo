---
id: feat-runs
prd: N/A
adr: []
code:
  - backend/src/modulo/api/routes/runs.py
  - backend/src/modulo/api/routes/run_ws.py
  - backend/src/modulo/api/routes/viewmodel.py
  - backend/src/modulo/api/routes/dashboard.py
  - backend/src/modulo/api/routes/lifecycle_maps.py
  - backend/src/modulo/api/routes/analytics.py
  - backend/src/modulo/db/crud/run.py
  - backend/src/modulo/core/pipeline_engine/classify.py
  - backend/src/modulo/core/pipeline_execution.py
  - backend/src/modulo/core/exceptions.py
  - backend/src/modulo/db/models/run.py
  - backend/src/modulo/db/migrations/versions/0280_runs_node_deadline_watchdog_fired_count.py
  - backend/src/modulo/db/migrations/versions/0288_runs_execution_origin.py
  - backend/src/modulo/db/migrations/versions/0302_runs_run_grouping.py
  - backend/src/modulo/core/line_diff.py
  - backend/src/modulo/core/cost_controller
  - frontend/src/utils/runUtils.ts
  - frontend/src/lib/api/runs.ts
unit-tests:
  - backend/tests/unit/api/test_runs_endpoint.py
  - backend/tests/unit/api/test_runs_rerun_endpoint.py
  - backend/tests/integration/test_pipeline_state_gate_rls.py
  - backend/tests/unit/api/test_run_events_endpoint.py
  - backend/tests/unit/api/test_run_ws.py
  - backend/tests/unit/api/test_run_api_key_auth.py
  - backend/tests/unit/api/test_runs_team_scope.py
  - backend/tests/unit/pipeline_engine/test_run_classification.py
  - backend/tests/unit/core/test_watchdog_firing_record_writer.py
  - backend/tests/unit/core/test_pipeline_execution_watchdog_retry.py
  - backend/tests/unit/core/cost_controller/test_run_warnings.py
  - backend/tests/unit/core/cost_controller/test_cost_aggregate.py
  - backend/tests/unit/db/test_run_grouping_resume_child.py
  - backend/tests/unit/db/test_migration_0302_runs_run_grouping.py
bdd:
  - backend/tests/bdd/features/errors
  - backend/tests/bdd/features/pipelines/run_lifecycle.feature
  - backend/tests/bdd/features/pipelines/run_sequential.feature
  - backend/tests/bdd/features/users/runner_role.feature
  - backend/tests/bdd/features/observability/active_run_observability.feature
  - backend/tests/bdd/steps/test_pipelines.py
  - backend/tests/bdd/steps/test_alpha_users.py
depends-on:
  - feat-pipelines
status: covered
---

# Run Execution, History & Detail

The `/runs` surface: triggering runs (with thread/runner identity), org-scoped run
listing, stats and heatmaps, run detail with terminal status and guardrail/gate
summaries, cancellation, node-level output and IO inspection, workspace events and
leases, live event polling, node recovery / observation / guardrail-override /
prompt-reveal actions, and error-state recovery BDD (`failed_state` / `recovery`).

## Behaviours

- [x] `POST /api/v1/runs` triggers a run (202) carrying `thread_id`; 404 for a missing
      pipeline, 409 for a deleted org, 422 for unknown fields, and 429 on
      rate-limit/capacity conflict (`test_runs_endpoint.py`)
- [x] `GET /api/v1/runs` lists org-scoped runs with actor/trigger labels and
      pagination; `GET /stats` and `GET /stats/heatmap` power the run stats/heatmap
      views
- [x] `GET /runs/{id}` returns run detail including current status, blocked partial
      summaries (with `null` for non-dicts), guardrail summaries (absent/malformed →
      `null`), `gate_fired` (idempotency gate, email classification, marker delivery
      and success-path markers) and serialized error detail
- [x] `POST /runs/{id}/cancel` cancels a run (202); already-terminal and
      `budget_exceeded` runs conflict (409); missing runs 404
- [x] `GET /runs/{id}/io`, `GET /runs/{id}/export-fixture`, `GET
      /runs/{id}/nodes/{node_id}/output` expose input/output and fixture export;
      `GET /workspace-lease` and `GET /workspace-events` track the run workspace
- [x] `GET /runs/{id}/events` streams chunked run events since a sequence number
      (404 when the run is unknown, node-id filterable) with a WebSocket event surface
      under `run_ws.py` (`test_run_events_endpoint.py`, `test_run_ws.py`)
- [x] Run actions: `POST /runs/{id}/nodes/{node_id}/observe` records observations,
      `POST /nodes/{node_id}/recover` recovers a failed node, `POST
      /guardrail-override` overrides a fired guardrail gate, and `POST
      /nodes/{node_id}/prompt/reveal` reveals a node's prompt
- [x] Only authenticated/authorized principals can trigger/inspect runs; api-key
      principals are scoped per key policy (`test_run_api_key_auth.py`)
- [x] `POST /api/v1/runs` team-scope gate (FAR-946): team-private pipelines
      (`visibility='team'`) require membership in the owning team or org-admin
      role; org-visible pipelines remain open to any runner.  Uses the shared
      `require_team_membership_or_admin_any_credential` gate with a body-based
      resolver that reads `pipeline_id` from the JSON request (`test_runs_team_scope.py`,
      `runner_role.feature`)
- [x] Run-level cost warnings (missing self-report surfacing, FAR-1305): a
      `self_reported` cost component with an eligible sandbox node but no
      accepted agent report stays visible in the run-detail breakdown – never
      rendered as a phantom `$0.000000` money line – and the response's
      structured `warnings` list (GET /api/v1/runs/{id}) carries a
      `missing_self_report` entry whose `missing_self_report_reason`
      distinguishes the THREE missing states truthfully: `agent_not_reported`
      (no cost key ever presented), `zero_report_unproven` (a node DID
      present an explicit `model_cost_usd: 0.0` but the trust boundary
      refused it as unproven – token usage not all-zero), and
      `sub_floor_rejected` (FAR-1308: a node PRESENTED a positive value
      below the countable minimum and the trust boundary refused it – the
      agent DID report, it was simply not countable). The run-detail,
      compute-run-warnings and MCP compact-line copy render the three states
      distinctly (a rejected zero or sub-floor report is never described as
      "not reported by the agent"), the reason rides the MCP breakdown wire
      (`_MCP_BREAKDOWN_KEYS`), and GET
      /api/v1/runs carries `warnings_count` (a deferred single
      `cost_breakdown` load, never N+1) so the list renders a warning badge
      (`core/cost_controller/breakdown/params.py`,
      `core/cost_controller/breakdown/aggregate.py`,
      `test_run_warnings.py`, `test_runs_endpoint.py`,
      `test_mcp_server_coverage_gaps.py`)
- [x] Run-execution service identity (ADR 038): a run executes with the
      pipeline owner's authority (service identity scoped to `owner_team_id`),
      not the triggering user's grants. Referenced resources (schema, connector,
      model backend, agent) are usable by the run regardless of the triggerer's
      direct access; the only user-facing check is "can you trigger this
      pipeline?" (`trigger_run` team gate). Secrets are brokered – injected by
      the engine, never readable by the user. `runs.owner_team_id` is metadata
      (dashboard aggregation), not a security control.
- [x] Error-state handling: failed states and node recovery flows are covered by
      `backend/tests/bdd/features/errors/{failed_state,recovery}.feature`. The
      recovery scenarios drive the REAL `POST /runs/{id}/nodes/{node_id}/recover`
      route (replay-with-`input_data` 200 / skip 200 / HITL-gate-node 422 /
      node-missing 404 / already-completed 409 / non-recoverable-state 409 /
      concurrent-recovery 409 / failed-resume-enqueue 500 / non-operator 403)
      with only the `recover_node` DB seam and the `dispatch_run` resume seam
      patched (`steps/test_alpha_errors.py`)
- [x] Run lifecycle is BDD-exercised end to end: a manual trigger creates a pending run
      (202), the engine moves it pending → running, a clean completion lands on
      the terminal `complete` status, an unhandled node exception lands on `failed`
      with an `error_detail`, and a mid-run cancellation is terminal (`cancelled`, no
      further nodes execute). A node that returns `None` output is a normal empty
      result – the run continues to the next node with no error – and sequential
      pipelines complete nodes strictly in order. A rate-limit conflict on the
      manual-trigger route surfaces 429 through the typed `RateLimitConflictError`
      path, while the pipeline `max_concurrent_runs` cap is enforced at dispatch (an
      over-capacity run parks with the `pipeline_capacity` marker rather than 429 at
      trigger time) (`run_lifecycle.feature`, `run_sequential.feature`, registered for
      execution by `steps/test_pipelines.py`)
- [x] Run-outcome delivery signal (FAR-189/228): every terminal run carries a
      `run_classification` JSON record written atomically with terminalization by
      the shared fenced terminal write – `value` (`delivered` / `no_delivery` /
      `excluded` / `benign` / `unclassified`), a stored `reason` (`pr_delivered` /
      `email_delivered` / `no_work` / `no_delivery` / `needs_human` /
      `source_error` / `parse_error` / `operator_or_hitl_cancelled` /
      `hitl_timeout` / `budget_exceeded` / `compensation_failed` /
      `router_no_match` / `hitl_rejected` / `hitl_superseded` /
      `benign_outcome` / `classifier_error`), `delivered_pr_urls`, `computed_at`,
      and the `work_intact` / `declared_success_nodes` terminalization-fact
      metadata. A classifier/persist failure writes a fail-closed `unclassified`
      marker (`crud/run.py` `_write_unclassified_classification`, SAVEPOINT-fenced
      and bounded) – a terminal run NEVER commits with a NULL record – and the
      periodic reconciliation sweep (`reconcile_missing_classifications`, wired into
      `dispatcher_reconcile`) backfills raw-SQL terminalizers within a minute
      (`pipeline_engine/classify.py`, `test_run_classification.py`)
- [x] Business-expected run outcomes (FAR-707): a pipeline may declare explicit
      benign signatures (`benign_signatures` JSON column on `pipelines`, migration
      0296 – a list of `{"error_code": "<exact runs.error_code>", "error_detail_contains":
      "<case-insensitive substring, optional>"}` entries) via
      `PATCH /api/v1/pipelines/{id}`, whose wire validation is STRICT: a non-list
      value, a missing/non-string `error_code`, a non-string detail, or any key
      outside the accepted pair is a 422 (a typo'd key must never silently widen
      a signature to match any detail); `null` clears the config. At
      terminalization a countable failure (`failed` / `eval_failed` / `stalled` /
      `compensation_failed`) whose `(error_code, error_detail)` matches a
      signature classifies `benign` (reason `benign_outcome`) INSTEAD of
      `no_delivery` – checked only in that branch, so `excluded` / `delivered` /
      non-terminal verdicts are never re-routed by a signature. A pipeline with
      no signatures (NULL / `[]`) classifies identically to the pre-FAR-707
      decision table; the stored config is read leniently at match time
      (`normalize_benign_signatures` drops malformed entries and logs – a config
      defect degrades to "no benign outcomes", never blocking terminalization).
      The verdict is copied to `run_daily_facts.benign` (`record_run_facts` /
      `_fact_is_benign`; NULL = written before the classification landed, read as
      "not benign" fail-open) so analytics failure readers drop benign runs from
      the rate's numerator AND denominator and from error-code failure clusters
      without joining `runs` (ADR 020) – volume/cost/token totals still include
      them. The reconciliation sweep re-records the fact when it lands a `benign`
      verdict, because raw-SQL terminalizers write the fact before the
      classification exists (`pipeline_engine/classify.py`
      `classify_run`/`matches_benign_signature`/`_reconcile_classify_run`,
      `api/routes/pipelines.py` `PipelineUpdate`, `core/analytics/`
      `record_run_facts`, `core/analytics/builder.py`, `db/crud/run.py` run-stats
      paths, migration `0296_benign_run_outcomes.py`;
      `test_run_classification.py`, `test_pipelines_endpoint.py`,
      `test_analytics_builder.py`, `test_analytics_record_facts.py`,
      `test_runs_stats.py`)
- [x] The delivery signal is agent-reported and honestly labelled (FAR-1336,
      vocabulary renamed from `self_reported` by FAR-1388):
      `delivered_pr_urls` are harvested in one walk from each node's structured
      return, the node telemetry value, and every FAR-188 raw-output marker's
      `pr_url` (deduplicated, validated) – and nothing cross-checks the URLs
      against an SCM of record. The record's `pr_url_provenance` map records per
      URL how it entered the record (`declared` = the run's output contract
      asserted it; `matched` = it merely appears in emitted output; `declared`
      wins when both routes see the URL) and `delivery_confidence` states plainly
      that every record written today is `agent_reported` (`verified` is reserved
      for a future SCM-confirmed source). Rows stored before the FAR-1388 rename
      carry the deprecated `self_reported` alias, are never backfilled, and
      readers tolerate either spelling (that old spelling must not be confused
      with the unrelated cost-provenance `self_reported` component kind).
      Both keys are ADDITIVE metadata – they
      never change the verdict – and the eight-key shape is forward-only:
      pre-FAR-1336 six-key rows are never backfilled and readers treat an absent
      key as legacy/unknown, never an error (`classify.py`, `test_run_classification.py`)
- [x] Run detail serializes the stored classification record and the derived
      gate-fired flag (FAR-228): `GET /api/v1/runs/{id}` returns
      `run_classification` (defensive-coerced – a non-dict degrades to null, never
      a 500) and `gate_fired` (True when the idempotency gate suppressed a delivery
      retry, the classification reason is `email_delivered`, or a raw-output marker
      carries `delivery_done`) (`test_runs_endpoint.py`)
- [x] Node-deadline watchdog firings are recorded distinctly from terminal
      failures (FAR-1463): `pipeline_execution._fail_overdue_node` durably
      increments `runs.node_deadline_watchdog_fired_count` on its OWN
      connection BEFORE the shared retry consult, so BOTH outcomes count — the
      re-dispatch (which otherwise leaves ZERO analytics fingerprint, because
      the fenced pending-reset nulls `error_code`) and the terminal fail. The
      marker deliberately survives the re-dispatch (neither the atomic claim
      nor the pending-reset names the column), so a run that never dispatched a
      node reads 0 while `error_code` separates a genuine terminal failure from
      a firing. Internal/analytics-only — never projected onto `RunResponse` or
      the MCP run payloads; migration 0280 adds the column NOT NULL DEFAULT 0
      and it is copied onto `run_daily_facts` at finalize so the analytics read
      path never joins `runs` (ADR 020) and the marker outlives the run purge
      (`core/pipeline_execution.py`, `db/models/run.py`,
      `test_watchdog_firing_record_writer.py`,
      `test_pipeline_execution_watchdog_retry.py`)
- [x] A run is refused for a pipeline that may not run (FAR-1528, extended by
      FAR-1530): `create_run` raises `PipelineNotRunnableError` when the
      pipeline is archived, soft-deleted OR paused (`run_enabled=false` — the
      FAR-1530 Paused execution state), and every REST entry point that can
      reach it (manual trigger, rerun, test trigger, webhook, replay, slack,
      variant runs, feedback correction) maps the refusal to 409 Conflict
      through the shared `pipeline_not_runnable_http` helper naming the pipeline
      id and state — never a generic 500 — while the cron/polling/ongoing fire
      jobs handle the same refusal without crashing. The archived / soft-deleted
      / paused precedence is resolved by the shared state classifier (deleted
      outranks archived outranks paused) and the state gate reads org-scoped and
      is RLS-fenced. A pipeline that does not exist is still refused upstream by
      the route's own 404 entry filter, so the state gate only ever turns an
      existing-but-not-runnable pipeline into a 409 (`api/routes/runs.py`,
      `db/crud/run.py` state classifier, `core/exceptions.py`,
      `test_runs_endpoint.py`, `test_runs_rerun_endpoint.py`,
      `test_pipeline_state_gate.py`, `test_pipeline_state_gate_rls.py`)
- [x] Every run carries WHERE its work was executed (FAR-1141, ADR-042):
      `runs.execution_origin` (migration `0288_runs_execution_origin`)
      is stamped `dispatched` by `create_run` when the run's frozen snapshot
      graph contains at least one `dispatch` node — part of the work ran
      OUTSIDE Modulo and was only witnessed/triggered by it (`db/models/run.py`
      single-sources the closed `EXECUTION_ORIGIN_DISPATCHED` vocabulary);
      `NULL` means Modulo-executed or legacy (never backfilled). The provenance
      rides every run-rendering surface — RunResponse, the `/api/v1/viewmodel`
      runs, the dashboard and lifecycle-map rows, the runs-list detail, and the
      analytics `execution_origin` dimension whose label utility keeps the raw
      key out of the table/chart — and the run-detail page renders a
      "Dispatched" badge (`frontend/src/utils/runUtils.ts`,
      `frontend/src/lib/api/runs.ts`, `api/routes/{runs,viewmodel,dashboard,
      lifecycle_maps,analytics}.py`)
- [x] The governed-run provenance VOCABULARY is ONE taxonomy (FAR-1693,
      ADR-042 unified 2026-10-12): `core/provenance.py` single-sources the
      closed four-class set — `observed` (Modulo observed it through a
      credential it holds), `derived` (Modulo computed it from observed facts),
      `reported` (arrived from ANY external surface, incl. a dispatched runtime,
      stamped `witnessed_via: <connector/channel>`), `attested` (governance
      events Modulo records natively). The dispatch arm's `witnessed_via` is the
      CHANNEL STAMP of a `reported` datum, not a class of its own: a dispatch
      node's folded-back data is stamped `provenance: reported` (NEVER
      `attested` — the binding foreclosure is that substrate data stays the
      substrate's report, and no governed badge implies anything about a
      customer's CI internals). Every claim-ready run payload composes the
      `provenance` class alongside `execution_origin` from the ONE serializer
      (`core/run_provenance.py`), engine-derived (`reported` for a dispatched
      run, `NULL` for an executed/legacy run — never a guessed `observed`); the
      class is engine-assigned, never self-asserted, so a prompt-injected
      substrate result cannot claim a stronger provenance
       (`core/provenance.py`, `core/run_provenance.py`,
       `core/pipeline_engine/node_runner.py`, `core/node_output_split.py`,
       `api/routes/runs.py`, `tests/unit/core/test_provenance_taxonomy.py`)
- [x] Retry/resume attempts are grouped rows, not fresh runs (FAR-153,
      §15.1 agent-failure UX v6): `runs.run_group_id` names the group's ROOT
      run id and `runs.run_group_kind` names WHY the row joined the group –
      the closed `{'retry','resume'}` vocabulary single-sourced as
      `RUN_GROUP_KIND_VALUES` in `db/models/run.py` (NULL for roots, signal /
      correction / work-item children, and legacy rows, which keep their
      existing rollup behaviour). A retry child runs its graph CLEAN on a
      fresh `langgraph_thread_id`; a resume child inherits its parent's
      thread via `create_run(thread_id=...)` so it continues the parent's
      checkpoint – the resume-as-child thread model. The contract is enforced
      at the `create_run` persist boundary (`_validate_run_grouping_kwargs` /
      `_validate_resume_thread_override` in `db/crud/run.py`): an unknown kind,
      a grouped row without `parent_run_id` or `run_group_id`, a resume
      without a thread override, and a resume whose thread is not the parent's
      – even a thread owned by a run in ANOTHER org – are all `ValueError`
      before any write (the parent read is org-scoped, so a resume never
      crosses orgs). Two partial unique indexes replace the former global
      `UNIQUE(langgraph_thread_id)` (migration `0302_runs_run_grouping`):
      exactly ONE resume child per parent (`uq_runs_resume_parent` – a second
      resume of the same stalled parent is rejected at the DB, while a resume
      child that itself stalls can be resumed again because its parent is a
      different row, so resume-of-resume chains work) and global thread
      uniqueness for every NON-resume row (`uq_runs_thread_non_resume` – two
      roots cannot share a thread and a retry can never inherit its parent's).
      No FK from `run_group_id` to the root row: the root may be purged by
      retention while a younger attempt survives, and a dangling group id is
      the intended read. Additive columns, never backfilled (`db/models/run.py`,
      `db/crud/run.py`, migration `0302_runs_run_grouping.py`,
      `test_run_grouping_resume_child.py`,
      `test_migration_0302_runs_run_grouping.py`)
- _Output Diff (`/runs/diff`, `POST /runs/diff`, `core/line_diff.py`) deferred from the
  MVP nav (hidden via `visibility: private_preview`). Behaviour detail removed for the
  MVP cut – restore from git history when re-enabling. See FAR-542._

## Known Gaps

- **No PRD section reference** – the run execution/detail surfaces have no single PRD
  section mapped in code or ADRs.
- **Wasm/Sandbox surfaces are split** – workspace leases/events live here, but the
  run sandbox lifecycle is tracked under `feat-environments`; cross-cutting coverage
  is not unified in one tracker.

## QA History

- 2026-10-13: **Improve Architecture product-map walk** – closed the untracked
  FAR-153 sub-surface: run grouping + the resume-as-child thread model
  (`runs.run_group_id` / `runs.run_group_kind`, the closed retry/resume
  vocabulary, the `create_run` persist-boundary contract, and the two partial
  unique indexes replacing the global thread unique) shipped in PR #1668
  (migration 0302) with unit coverage (`test_run_grouping_resume_child.py`,
  `test_migration_0302_runs_run_grouping.py`) but was described by NEITHER
  product-map layer – invisible to the feature graph and to Assistant's
  `search_documentation` indexer. Added the checked behaviour line plus the
  `db/models/run.py` / `db/crud/run.py` / migration-0302 code citations and
  the two unit-test citations (frontmatter + manifest `feat-runs` registry).
  `_ORPHANED_BDD_FEATURES` stays empty.
- 2026-10-08: **Improve Architecture product-map walk** – closed the untracked
  FAR-1141 run-provenance half: `runs.execution_origin` (ADR-042) and the
  `dispatched` stamp shipped in PR #1370 with only a testid in the product map,
  nowhere in the feature registry or this tracker. Added the checked behaviour
  line (closed vocabulary, frozen-graph derivation, legacy-never-backfilled,
  the run-rendering surfaces and the analytics dimension) plus the
  `db/models/run.py` / migration `0288_runs_execution_origin` /
  `frontend/src/utils/runUtils.ts` citations already in the frontmatter.
  `_ORPHANED_BDD_FEATURES` stays empty.
- 2026-10-07: **Improve Architecture product-map walk (follow-up)** – closed the
  `feat-runs` tracker gap for FAR-1530: the manifest `feat-runs` registry entry
  was extended to name the `paused` state (`run_enabled=false`), but this
  tracker still described only archived / soft-deleted pipelines. Extended the
  checked behaviour line to include the paused refusal, the
  deleted-outranks-archived-outranks-paused state classifier
  (`db/crud/run.py`), the RLS-fenced org-scoped state gate, and the
  `test_pipeline_state_gate.py` citation, matching the manifest layer.
  `_ORPHANED_BDD_FEATURES` stays empty.
- 2026-10-07: **Improve Architecture product-map walk** – closed the untracked
  FAR-1528 sub-surface: `create_run` now refuses an archived or soft-deleted
  pipeline with a typed `PipelineNotRunnableError` mapped to 409 Conflict
  across every REST entry point (manual trigger, rerun, test trigger, webhook,
  replay, slack, variant runs, feedback correction) through the shared
  `pipeline_not_runnable_http` helper, and the cron/polling/ongoing fire jobs
  handle the same refusal without crashing. Added the checked behaviour line,
  the `core/exceptions.py` code citation and the `test_runs_rerun_endpoint.py`
  / `test_pipeline_state_gate_rls.py` citations. `_ORPHANED_BDD_FEATURES`
  stays empty.
- 2026-10-05: **Improve Architecture product-map walk** – closed the untracked
  FAR-1463 sub-surface: the node-deadline watchdog now durably records each
  firing on `runs.node_deadline_watchdog_fired_count` (and copies it onto
  `run_daily_facts`), so a firing is observable in analytics even when the kill
  re-dispatches and nulls `error_code`, and the count is distinct from a genuine
  terminal failure. Added the checked behaviour line plus the
  `core/pipeline_execution.py` / `db/models/run.py` / migration-0280 code
  citations and the `test_watchdog_firing_record_writer.py` /
  `test_pipeline_execution_watchdog_retry.py` unit citations. The analytics
  read surface (bucket metric + export field) is cited under `feat-analytics`.
  `_ORPHANED_BDD_FEATURES` stays empty.
- 2026-10-04: **FAR-1308 three-state missing-cost truth** – a positive
  self-reported cost below the countable floor was being labelled
  `agent_not_reported` ("the agent did not report"), which is false: the
  node DID report, the trust boundary refused the value as sub-floor.
  Added the third `missing_self_report_reason` value `sub_floor_rejected`
  alongside `zero_report_unproven` and `agent_not_reported`, surfaced it in
  the run-detail warnings strip, the compute-run-warnings copy, the manifest
  `feat-runs` registry entry, and the MCP compact-line renderer
  (`_format_breakdown_line` now prints
  "(reported a value below the countable minimum)" instead of
  "(not reported)").
- 2026-10-01: **Improve Architecture product-map walk** – closed the
  sub-surface gap left by FAR-1336 (delivery-signal provenance/confidence,
  merged as run classification record addenda) and FAR-228: the run-outcome
  delivery signal (`run_classification` record, fail-closed `unclassified`
  marker + reconciliation sweep, run-detail serialization + derived
  `gate_fired`) had NO product-map home in either layer – invisible to the
  feature graph and to Assistant's `search_documentation` indexer. Added the
  three checked behaviour lines above plus the `pipeline_engine/classify.py`
  code and `test_run_classification.py` unit-test citations. The FAR-1373
  streak-readout half is tracked under `feat-triggers`.
  `_ORPHANED_BDD_FEATURES` stays empty.
- 2026-09-30: **Improve Architecture product-map walk** – reconciled this
  tracker with the shipped FAR-1305 missing-cost surface (merged 2026-09-30):
  the manifest `feat-runs` registry tracks the truthful
  `missing_self_report_reason` (at the time two states:
  `agent_not_reported` vs `zero_report_unproven`; a third,
  `sub_floor_rejected`, was added 2026-10-04 by FAR-1308 – see above)
  but the human-readable graph entry was stale. Added the checked behaviour
  line and the `core/cost_controller/` code + `test_run_warnings.py` /
  `test_cost_aggregate.py` / `test_runs_endpoint.py` unit citations.
  `_ORPHANED_BDD_FEATURES` stays empty.
- 2026-09-24: **product-map walk** – closed the deferred run recovery/retry
  BDD drafts (`build/runs.md` error-state coverage). The run-level `/resume` /
  `/retry` endpoints those scenarios targeted never shipped – recovery is
  per-node via `POST /runs/{id}/nodes/{node_id}/recover` – so
  `errors/retry.feature` was deleted (its retry-from-node / retry-on-success
  semantics are the replay / already-completed-409 cases now locked by the
  recovery surface) and `errors/recovery.feature` was rewritten to drive the
  REAL recover-node route with only the `recover_node` + `dispatch_run` seams
  patched: replay/skip resume (200 with the resume dispatch carrying the
  recovery output), the HITL-gate-node refusal (422), node-missing (404),
  already-completed (409), non-recoverable state (409), concurrent recovery
  (409), failed-resume-enqueue (500) and the non-operator 403 gate. Removed
  both files from `PINNED_AWAITING_IMPLEMENTATION`; the scenarios now execute
  in CI. `_ORPHANED_BDD_FEATURES` stays empty.

- 2026-09-21: **product-map walk** – closed the active-run observability BDD gap
  (tracked under `feat-observability`): `active_run_observability.feature` is no
  longer `@awaiting-implementation`. The two scenarios now drive the REAL
  `GET /api/v1/runs/{id}` / `GET /api/v1/runs/{id}/events` routes with only the
  `_do_*` DB-fetch seams patched, asserting the detail contract
  (`trigger_actor` / `heartbeat_at` / `capacity` / `work_item_refs` /
  `child_runs`) and the node lifecycle events end to end (real `RunEventBroker`
  replay + filter). Cited here and in `observability.md`.

- 2026-09-12: **product-map review pass** – registered the shared
  `JsonViewer` surface (`components/shared/JsonViewer.vue` static testids
  `json-viewer` / `json-viewer-{copy,expand-all,collapse-all,string-expand,string-collapse}`)
  in the manifest `elements:` inventory for `/runs/diff`: the compared run-output
  legs render inline with `<JsonViewer :show-toolbar="true">`
  (`AgentOutputDiffView.vue`), so the viewer shipped in the DOM while staying
  invisible to Assistant's docs indexer / `/api/v1/manifest`. The component is now part
  of the route's reverse testid-coverage guard
  (`test_mapped_route_elements_cover_owning_view_testids`).

- 2026-09-12: **product-map review pass** – registered the shared
  `PageHeader` right-slot surface (`components/shared/PageHeader.vue`, static testid
  `page-header-right`) in the manifest `elements:` inventory for `/runs`, which
  renders the header's `#right` action slot, and wired the component into the reverse
  testid-coverage guard (`test_mapped_route_elements_cover_owning_view_testids`) so the
  header action surface stays visible to Assistant's docs indexer / `/api/v1/manifest`.

- 2026-09-11: **product-map review pass** – registered the shared
  search-bar surface (`components/shared/FilterBar.vue` static testids
  `filter-bar-search` / `filter-bar-search-wrapper`) in the `/runs` manifest
  `elements:` inventory and wired the component into the reverse testid-coverage
  guard, so the runs-list search control the page ships stays visible to Assistant's
  docs indexer and `/api/v1/manifest`.

- 2026-09-11: **product-map review pass** – extended the reverse
  testid-coverage guard (`test_mapped_route_elements_cover_owning_view_testids`) to
  `/runs/diff`: the whole-page view(s) `AgentOutputDiffView.vue` render static `data-testid`s that the
  product map `elements:` inventory already documents, but the surface was not yet
  guarded against drift. The route now maps to its owning view so a newly shipped
  testid can no longer silently stay invisible to Assistant's docs indexer /
  `/api/v1/manifest`.

- 2026-09-11: **product-map review pass** – closed the
  `/runs/:id` element-inventory drift for the HitlBriefing surface embedded in
  `hitl/HitlReviewCard.vue`: the gate card renders `HitlBriefing.vue` (the
  gate reason/context briefing with its collapse toggle and condition-result
  detail), so its shipped static testids (`hitl-briefing*`) are part of the Run
  Detail page surface. They are now registered on `/runs/:id`, and
  `test_mapped_route_elements_cover_owning_view_testids` maps the route to
  `HitlBriefing.vue` alongside the previously-closed shared components, so a
  newly shipped briefing testid can no longer drift invisible to Assistant's docs
  indexer / `/api/v1/manifest`.
- 2026-09-11: **product-map review pass** – closed the
  `/runs/:id` element-inventory drift for the shared components the Run Detail
  page renders: `shared/JsonViewer.vue` (the collapsible JSON explorer used for
  IO/output/telemetry inspection), `shared/ErrorAlert.vue` (its dismiss
  affordance) and `hitl/HitlReviewCard.vue` (the run-link and foreign-claim
  surfaces for a run gate). Their shipped testids (`json-viewer*`,
  `error-alert-dismiss`, `hitl-gate-run-link`, `hitl-gate-foreign-claim`) are
  now registered on `/runs/:id`, and
  `test_mapped_route_elements_cover_owning_view_testids` now maps that route to
  the layout + those shared owning components, so a newly shipped run-detail /
  json-viewer / gate testid can no longer drift invisible to Assistant's docs
  indexer / `/api/v1/manifest`.
- 2026-09-09: **product-map review pass** – closed the dead-BDD-file
  Known Gap recorded here on 2026-09-08: `run_lifecycle.feature` / `run_sequential.feature`
  are no longer orphaned – they were wired into `steps/test_pipelines.py` (12 scenarios)
  when the same gap was closed on the `feat-pipelines` tracker, but this entry was not
  updated. Both files are now cited in `bdd:` and the run-lifecycle / sequential-ordering
  behaviour is ticked. Status: covered.
- 2026-09-08: **product-map review pass** – recorded `run_lifecycle.feature`
  / `run_sequential.feature` as a dead-BDD-file known gap (run-time surfaces owned here that
  no step module registers). Superseded by the 2026-09-09 closure above once
  `steps/test_pipelines.py` registered both files.
- 2026-08-28: **product-map review pass** – added this behaviour-tracker
  for the registered manifest feature `feat-runs`, which previously had no
  `docs/product-map/` entry. Behaviours verified against `api/routes/runs.py`,
  `api/routes/run_ws.py`, `db/crud/run.py`, `core/line_diff.py` and the runs unit/BDD
  suites. Status: covered.
