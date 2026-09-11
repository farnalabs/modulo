---
id: feat-runs
prd: N/A
adr: []
code:
  - backend/src/modulo/api/routes/runs.py
  - backend/src/modulo/api/routes/run_ws.py
  - backend/src/modulo/db/crud/run.py
  - backend/src/modulo/core/line_diff.py
unit-tests:
  - backend/tests/unit/api/test_runs_endpoint.py
  - backend/tests/unit/api/test_run_events_endpoint.py
  - backend/tests/unit/api/test_run_ws.py
  - backend/tests/unit/api/test_run_api_key_auth.py
bdd:
  - backend/tests/bdd/features/errors
  - backend/tests/bdd/features/pipelines/run_lifecycle.feature
  - backend/tests/bdd/features/pipelines/run_sequential.feature
  - backend/tests/bdd/steps/test_pipelines.py
depends-on:
  - feat-pipelines
status: covered
---

# Run Execution, History & Detail

The `/runs` surface: triggering runs (with thread/runner identity), org-scoped run
listing, stats and heatmaps, run detail with terminal status and guardrail/gate
summaries, cancellation, node-level output and IO inspection, workspace events and
leases, live event polling, node recovery / observation / guardrail-override /
prompt-reveal actions, and error-state recovery BDD (`failed_state` / `retry` /
`recovery`).

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
- [x] Error-state handling: failed states, retries and recovery flows are covered by
      `backend/tests/bdd/features/errors/{failed_state,retry,recovery}.feature`
- [x] Run lifecycle is BDD-exercised end to end: a manual trigger creates a pending run
      (202), the engine moves it pending → running, a clean completion lands on
      `completed` with a `final_state`, an unhandled node exception lands on `failed`
      with an `error_detail`, and a mid-run cancellation is terminal (`cancelled`, no
      further nodes execute). A node that returns `None` output is a normal empty
      result — the run continues to the next node with no error — and sequential
      pipelines complete nodes strictly in order. A trigger refused by
      `max_concurrent_runs` while a pending run is already active surfaces 429
      (`run_lifecycle.feature`, `run_sequential.feature`, registered for execution by
      `steps/test_pipelines.py`)
- _Output Diff (`/runs/diff`, `POST /runs/diff`, `core/line_diff.py`) deferred from the
  MVP nav (hidden via `visibility: private_preview`). Behaviour detail removed for the
  MVP cut — restore from git history when re-enabling. See FAR-542._

## Known Gaps

- **No PRD section reference** — the run execution/detail surfaces have no single PRD
  section mapped in code or ADRs.
- **Wasm/Sandbox surfaces are split** — workspace leases/events live here, but the
  run sandbox lifecycle is tracked under `feat-environments`; cross-cutting coverage
  is not unified in one tracker.

## QA History

- 2026-09-11: **improve-architecture (product-map walk)** — extended the reverse
  testid-coverage guard (`test_mapped_route_elements_cover_owning_view_testids`) to
  `/runs/diff`: the whole-page view(s) `AgentOutputDiffView.vue` render static `data-testid`s that the
  product map `elements:` inventory already documents, but the surface was not yet
  guarded against drift. The route now maps to its owning view so a newly shipped
  testid can no longer silently stay invisible to Remy's docs indexer /
  `/api/v1/manifest`.

- 2026-09-11: **improve-architecture (product-map walk)** — closed the
  `/runs/:id` element-inventory drift for the HitlBriefing surface embedded in
  `hitl/HitlGateCard.vue`: the gate card renders `HitlBriefing.vue` (the
  gate reason/context briefing with its collapse toggle and condition-result
  detail), so its shipped static testids (`hitl-briefing*`) are part of the Run
  Detail page surface. They are now registered on `/runs/:id`, and
  `test_mapped_route_elements_cover_owning_view_testids` maps the route to
  `HitlBriefing.vue` alongside the previously-closed shared components, so a
  newly shipped briefing testid can no longer drift invisible to Remy's docs
  indexer / `/api/v1/manifest`.
- 2026-09-11: **improve-architecture (product-map walk)** — closed the
  `/runs/:id` element-inventory drift for the shared components the Run Detail
  page renders: `shared/JsonViewer.vue` (the collapsible JSON explorer used for
  IO/output/telemetry inspection), `shared/ErrorAlert.vue` (its dismiss
  affordance) and `hitl/HitlGateCard.vue` (the run-link and foreign-claim
  surfaces for a run gate). Their shipped testids (`json-viewer*`,
  `error-alert-dismiss`, `hitl-gate-run-link`, `hitl-gate-foreign-claim`) are
  now registered on `/runs/:id`, and
  `test_mapped_route_elements_cover_owning_view_testids` now maps that route to
  the layout + those shared owning components, so a newly shipped run-detail /
  json-viewer / gate testid can no longer drift invisible to Remy's docs
  indexer / `/api/v1/manifest`.
- 2026-09-09: **improve-architecture (product-map walk)** — closed the dead-BDD-file
  Known Gap recorded here on 2026-09-08: `run_lifecycle.feature` / `run_sequential.feature`
  are no longer orphaned — they were wired into `steps/test_pipelines.py` (12 scenarios)
  when the same gap was closed on the `feat-pipelines` tracker, but this entry was not
  updated. Both files are now cited in `bdd:` and the run-lifecycle / sequential-ordering
  behaviour is ticked. Status: covered.
- 2026-09-08: **improve-architecture (product-map walk)** — recorded `run_lifecycle.feature`
  / `run_sequential.feature` as a dead-BDD-file known gap (run-time surfaces owned here that
  no step module registers). Superseded by the 2026-09-09 closure above once
  `steps/test_pipelines.py` registered both files.
- 2026-08-28: **improve-architecture (product-map walk)** — added this behaviour-tracker
  for the registered manifest feature `feat-runs`, which previously had no
  `docs/product-map/` entry. Behaviours verified against `api/routes/runs.py`,
  `api/routes/run_ws.py`, `db/crud/run.py`, `core/line_diff.py` and the runs unit/BDD
  suites. Status: covered.
