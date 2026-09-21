Feature: Active-Run Observability Contract
  As the Runs UI (list + detail views)
  I want the real run endpoints to expose active-run observability data
  So that the live node-progress strip, queue banner, trigger actor, heartbeat,
  work items, and child runs render without silent empty sections

  # Executing BDD surface (closed 2026-09-21): the scenarios drive the REAL
  # GET /api/v1/runs/{run_id} and GET /api/v1/runs/{run_id}/events routes with
  # only the DB-fetch seams patched (the _do_* helpers), so the full route
  # handler, authz dependency and RunResponse / RunEventsResponse serialization
  # run for real. The event-stream scenario drives the REAL per-run
  # RunEventBroker in the registry, so replay and the node-lifecycle filter are
  # asserted end to end, not hand-built.

  Scenario: Run detail exposes the active-run observability fields
    Given an active run with heartbeat, capacity, work item refs, and child runs
    When I fetch the run detail via the API
    Then the run detail response includes trigger_actor
    And the run detail response includes heartbeat_at
    And the run detail response includes a capacity object with active_runs, concurrency_limit, and waiting
    And the run detail response includes work_item_refs
    And the run detail response includes child_runs

  Scenario: Run event stream exposes node lifecycle events
    Given an active run with node lifecycle events
    When I fetch the run event stream via the API
    Then the event stream includes node_started events
    And the event stream includes node_completed events
    And the event stream includes node_failed events
