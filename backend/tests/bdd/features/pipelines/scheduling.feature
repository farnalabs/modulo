Feature: Pipeline Scheduling
  As a pipeline operator
  I want to schedule pipelines to run on a cron or polling schedule
  So that pipelines execute automatically without manual triggering

  Scenario: Create a cron trigger for a pipeline
    Given org "acme" has pipeline "nightly-report"
    And I am authenticated in org "acme"
    When I create a cron trigger for pipeline "nightly-report" with expression "0 6 * * *"
    Then the response status is 201
    And the trigger has a next_fire_at timestamp

  @awaiting-implementation
  Scenario: Cron trigger fires and creates a run
    Given an active cron trigger exists for pipeline "nightly-report"
    When the cron scheduler fires the trigger
    Then a run is created with status "pending"

  Scenario: Invalid cron expression is rejected
    Given org "acme" has pipeline "nightly-report"
    And I am authenticated in org "acme"
    When I create a cron trigger for pipeline "nightly-report" with expression "not-a-cron"
    Then the response status is 422

  @awaiting-implementation
  Scenario: Toggle trigger active state
    Given an active cron trigger exists for pipeline "nightly-report"
    And I am authenticated in org "acme"
    When I toggle the trigger active state
    Then the response status is 200
    And the trigger is no longer active

  @awaiting-implementation
  Scenario: Preview next cron fire times
    Given a cron trigger with expression "0 6 * * *" exists
    And I am authenticated in org "acme"
    When I GET the cron schedule preview with count 3
    Then the response status is 200
    And the response lists 3 future fire times
