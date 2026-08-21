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

  Scenario: Toggle trigger active state
    Given org "acme" has pipeline "nightly-report"
    And an active cron trigger exists for pipeline "nightly-report" with expression "0 6 * * *"
    And I am authenticated in org "acme"
    When I toggle the trigger active state
    Then the response status is 200
    And the trigger is no longer active

  Scenario: Preview next cron fire times
    Given org "acme" has pipeline "nightly-report"
    And an active cron trigger exists for pipeline "nightly-report" with expression "0 6 * * *"
    And I am authenticated in org "acme"
    When I fetch the cron schedule preview with count 3
    Then the response status is 200
    And the response lists 3 future fire times

  @awaiting-implementation
  Scenario: Polling trigger fires when condition is met
    Given org "acme" has pipeline "monitor-issues"
    And an active polling trigger exists for pipeline "monitor-issues" with poll query "select * from issues"
    When the poll scheduler fires the trigger
    Then a run is created with trigger_type "polling"
    And a TriggerEvent is recorded with result "condition_met"

  @awaiting-implementation
  Scenario: Polling trigger does not fire when condition not met
    Given org "acme" has pipeline "monitor-issues"
    And an active polling trigger exists for pipeline "monitor-issues" with poll query "select * from issues"
    And the condition expression evaluates to false
    When the poll scheduler fires the trigger
    Then no run is created
    And a TriggerEvent is recorded with result "no_match"

  @awaiting-implementation
  Scenario: Polling trigger logs error when connector fails
    Given org "acme" has pipeline "monitor-issues"
    And an active polling trigger exists for pipeline "monitor-issues" with poll query "select * from issues"
    And the connector instance is not found
    When the poll scheduler fires the trigger
    Then no run is created
    And a TriggerEvent is recorded with result "poll_error"
