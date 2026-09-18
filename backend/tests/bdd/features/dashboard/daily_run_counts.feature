Feature: Daily Run Counts
  As a platform operator
  I want daily run counts broken down by status
  So that I can see day-over-day workload distribution

  `GET /api/v1/dashboard/daily-run-counts` returns the last N days
  (default 30, bounded 1..365) of run counts grouped by `(day, status)` from
  the `runs` table. The response is org-RLS-scoped and permission-gated to
  `dashboard.daily_run_counts`.

  Scenario: The daily run counts are grouped by day and status
    Given I am authenticated as an admin
    And runs exist today with 4 complete and 2 failed
    When I request GET /api/v1/dashboard/daily-run-counts
    Then the response status is 200
    And the daily counts are keyed by day with per-status counts
    And today's counts include 4 complete and 2 failed

  Scenario: Runs on the same day accumulate across statuses
    Given I am authenticated as an admin
    And runs exist today with 4 complete and 2 failed
    When I request GET /api/v1/dashboard/daily-run-counts
    Then today's counts total 6 runs

  Scenario: The daily counts respect a custom days window
    Given I am authenticated as an admin
    When I request GET /api/v1/dashboard/daily-run-counts?days=7
    Then the response status is 200
    And the response reports a days window of 7

  Scenario: The daily counts default to a 30-day window
    Given I am authenticated as an admin
    When I request GET /api/v1/dashboard/daily-run-counts
    Then the response status is 200
    And the response reports a days window of 30

  Scenario: The daily counts reject days outside the 1..365 window
    Given I am authenticated as an admin
    When I request GET /api/v1/dashboard/daily-run-counts?days=0
    Then the response status is 422

  Scenario: The daily counts reject days above the 1..365 window
    Given I am authenticated as an admin
    When I request GET /api/v1/dashboard/daily-run-counts?days=366
    Then the response status is 422
