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

- 2026-08-28: **improve-architecture (product-map walk)** — added this behaviour-tracker
  for the registered manifest feature `feat-runs`, which previously had no
  `docs/product-map/` entry. Behaviours verified against `api/routes/runs.py`,
  `api/routes/run_ws.py`, `db/crud/run.py`, `core/line_diff.py` and the runs unit/BDD
  suites. Status: covered.
