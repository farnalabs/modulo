Feature: Home Dashboard Summary
  As a platform operator
  I want an org-level dashboard summary of runs, teams, eval pass rate, and 7-day trend
  So that I can monitor health at a glance without querying the database directly

  `GET /api/v1/dashboard/summary` aggregates the whole org: total runs (with
  pending/claimed slot-holders folded into `idle` so each run is counted once),
  active pipelines, per-status run counts, per-team metrics, non-guardrail eval
  pass rate with per-pipeline detail, a 7-day run/eval/spend trend, recent runs,
  and non-blocking config warnings. Responses are org-RLS-scoped, permission
  gated to `dashboard.summary`, and a `days` query (1..90) adds a `period`
  block with `{current, previous, delta_pct}` per metric.

  Scenario: The summary returns the org's dashboard widgets
    Given I am authenticated as an admin
    When I request GET /api/v1/dashboard/summary
    Then the response status is 200
    And the summary contains total_runs, active_pipelines, and run_counts_by_status
    And the summary contains per-team metrics with run counts
    And the summary contains an eval pass rate with per-pipeline detail
    And the summary contains a 7-day trend with run counts and spend
    And the summary contains recent runs and config warnings

  Scenario: Status counts fold idle slot-holders into idle and count each run once
    Given the org has runs with statuses pending=2, claimed=3, running=4, failed=1
    And I am authenticated as an admin
    When I request GET /api/v1/dashboard/summary
    Then the response status is 200
    And the summary run_counts_by_status has running=4, failed=1, and idle=5
    And total_runs counts pending/claimed only once and equals 10

  Scenario: The summary carries an additive period block when days is requested
    Given I am authenticated as an admin
    When I request GET /api/v1/dashboard/summary?days=30
    Then the response status is 200
    And the summary period block reports days=30 with current/previous/delta_pct metrics

  Scenario: The summary rejects days outside the 1..90 window
    Given I am authenticated as an admin
    When I request GET /api/v1/dashboard/summary?days=0
    Then the response status is 422

  Scenario: The summary rejects days above the 1..90 window
    Given I am authenticated as an admin
    When I request GET /api/v1/dashboard/summary?days=91
    Then the response status is 422
