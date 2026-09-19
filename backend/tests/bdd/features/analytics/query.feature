Feature: Analytics Query Surface
  As a platform operator
  I want a bucketed, filters-aware analytics API over run facts
  So that I can report on runs, costs, concurrency and guardrails without querying the database directly

  `GET /api/v1/analytics/query` returns a zero-filled, day/hour/week-bucketed
  series over `run_daily_facts` (FAR-102 / ADR 020), scoped to the caller's
  org, permission-gated to `analytics.query` and feature-gated to
  `analytics_page`. `/concurrency` reports slot utilisation,
  `/guardrails` an advisory guardrail scorecard, and `/export` streams raw
  fact rows as JSON or CSV. The route is a thin adapter over
  `modulo.core.analytics.service` and maps the service's typed errors to
  HTTP status codes.

  Background:
    Given I am an authenticated org admin

  Scenario: The bucketed query returns the org's day series
    Given the analytics service reports one bucket with 3 runs on 2026-07-01
    When I request GET /api/v1/analytics/query?date_from=2026-07-01&date_to=2026-07-07
    Then the response status is 200
    And the response carries the day-series envelope with 1 bucket
    And the bucket for 2026-07-01 counts 3 runs

  Scenario: A repeated pipeline_id composes an A/B comparison
    Given the analytics service reports a trigger_type dimension
    And I filter on two distinct pipelines
    When I request GET /api/v1/analytics/query?dimension=trigger_type
    Then the response status is 200
    And the query surfaced both pipeline ids to the analytics service
    And the response echoes the trigger_type dimension

  Scenario: A malformed date_from is rejected with 422
    When I request GET /api/v1/analytics/query?date_from=not-a-date
    Then the response status is 422

  Scenario: A limit beyond the 1..1000 bound is rejected with 422
    When I request GET /api/v1/analytics/query?limit=9999
    Then the response status is 422

  Scenario: An inverted date range maps the typed validation error to 422
    Given the analytics service rejects the query as invalid
    When I request GET /api/v1/analytics/query
    Then the response status is 422

  Scenario: A rate-limited org maps the typed error to 429
    Given the analytics service is rate limited
    When I request GET /api/v1/analytics/query
    Then the response status is 429

  Scenario: A statement timeout maps the typed error to 503
    Given the analytics service times out
    When I request GET /api/v1/analytics/query
    Then the response status is 503

  Scenario: The disabled analytics_page feature gate refuses the surface with 402
    Given the analytics_page feature is disabled
    When I request GET /api/v1/analytics/query
    Then the response status is 402

  Scenario: A principal without an organisational context is refused with 403
    Given I am signed in without an organisation context
    When I request GET /api/v1/analytics/query
    Then the response status is 403

  Scenario: An unauthenticated caller is refused with 401
    Given an unauthenticated caller
    When I request GET /api/v1/analytics/query
    Then the response status is 401

  Scenario: The export surface returns paginated JSON rows
    Given the export service reports 2 fact rows with a total of 2
    When I request GET /api/v1/analytics/export?limit=10&date_from=2026-07-01&date_to=2026-07-07
    Then the response status is 200
    And the export response carries 2 items and a total of 2

  Scenario: The export surface streams CSV with an attachment disposition
    When I request GET /api/v1/analytics/export?format=csv&date_from=2026-07-01&date_to=2026-07-07
    Then the response status is 200
    And the response is a CSV attachment

  Scenario: The concurrency endpoint reports slot utilisation
    Given the concurrency service reports one bucket with 2 active and 1 queued on 2026-07-01
    When I request GET /api/v1/analytics/concurrency?date_from=2026-07-01&date_to=2026-07-07
    Then the response status is 200
    And the concurrency response carries the pooled bucket series

  Scenario: The guardrail scorecard is advisory-only
    Given the guardrail scorecard reports 1 blocked run
    When I request GET /api/v1/analytics/guardrails?date_from=2026-07-01&date_to=2026-07-07
    Then the response status is 200
    And the scorecard is labelled advisory_only