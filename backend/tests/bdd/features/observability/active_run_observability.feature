Feature: Active-Run Observability Contract
  As the Runs UI (list + detail views)
  I want the real run endpoints to expose active-run observability data
  So that the live node-progress strip, queue banner, trigger actor, heartbeat,
  work items, and child runs render without silent empty sections

# Deselected from CI until the companion backend lands. The wire shapes below
# match the Pydantic fields delivered by the backend observability PR (#1583);
# when that PR merges, remove the @awaiting-implementation tag (and this note)
# so the scenarios round-trip the REAL payload through the REAL endpoint and
# gate any future contract drift. The frontend tests (RunDetailView.spec.ts,
# RunsListView.spec.ts) mock api.GET/fetch with hand-crafted payloads and so
# cannot catch backend contract drift on their own.

@awaiting-implementation
  Scenario: Run detail exposes the active-run observability fields
    Given an active run with heartbeat, capacity, work item refs, and child runs
    When I fetch the run detail via the API
    Then the run detail response includes trigger_actor
    And the run detail response includes heartbeat_at
    And the run detail response includes a capacity object with active_runs, concurrency_limit, and waiting
    And the run detail response includes work_item_refs
    And the run detail response includes child_runs

@awaiting-implementation
  Scenario: Run event stream exposes node lifecycle events
    Given an active run with node lifecycle events
    When I fetch the run event stream via the API
    Then the event stream includes node_started events
    And the event stream includes node_completed events
    And the event stream includes node_failed events
