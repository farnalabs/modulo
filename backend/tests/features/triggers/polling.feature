Feature: Polling Trigger
  As a pipeline operator
  I want triggers that poll external systems on a schedule
  So that my pipelines react to conditions in third-party services

  Background:
    Given I am authenticated as an admin in org "acme"

  Scenario: Create polling trigger with basic config
    Given org "acme" has pipeline "my-pipeline"
    When I PATCH /api/v1/triggers/{trigger_id}/polling with:
      | key                     | value                         |
      | connector_instance_id   | "ci-github-issues"            |
      | poll_query              | "select * from issues"        |
      | condition_expression    | "[?status=='open']"           |
      | poll_interval_seconds   | 60                            |
      | snapshot_id             | "snap-001"                    |
    Then the response status is 200
    And the trigger config contains "connector_instance_id" with value "ci-github-issues"
    And the trigger config contains "poll_query" with value "select * from issues"
    And the trigger config contains "condition_expression" with value "[?status=='open']"
    And the trigger config contains "poll_interval_seconds" with value 60

  Scenario: Polling trigger fires when condition is met
    Given org "acme" has pipeline "polling-pipeline" with polling config
    When the polling scheduler runs and evaluates the condition
    And the connector returns records matching "[?status=='open']"
    Then a Run is created with trigger_type "polling"
    And a TriggerEvent is created with result "condition_met"
    And the run references the polling trigger

  Scenario: Polling trigger does not fire when condition is not met
    Given org "acme" has pipeline "polling-pipeline" with polling config
    When the polling scheduler runs and evaluates the condition
    And the connector returns no matching records
    Then no Run is created
    And a TriggerEvent is created with result "no_match"

  Scenario: Polling trigger respects max_concurrent_runs
    Given org "acme" has pipeline "polling-pipeline" with polling config
    And the pipeline has 5 active runs
    And the trigger max_concurrent_runs is 3
    When the polling scheduler runs
    Then the trigger is skipped due to concurrency limit
    And a TriggerEvent is created with result "concurrency_limit_reached"

  Scenario: Invalid JMESPath expression logs poll_error
    Given org "acme" has pipeline "polling-pipeline" with polling config
    When the polling scheduler runs
    And the condition_expression is "[invalid: syntax"
    Then no Run is created
    And a TriggerEvent is created with result "poll_error"
    And the error_detail mentions "invalid JMESPath"

  Scenario: Connector failure logs poll_error
    Given org "acme" has pipeline "polling-pipeline" with polling config
    When the polling scheduler runs
    And the connector query fails
    Then no Run is created
    And a TriggerEvent is created with result "poll_error"

  Scenario: Inactive trigger is skipped
    Given org "acme" has pipeline "polling-pipeline" with polling config
    And the trigger is deactivated
    When the polling scheduler runs
    Then no Run is created
    And the trigger is skipped with reason "trigger_inactive_or_missing"

  Scenario: Test polling query without creating a run
    Given org "acme" has pipeline "my-pipeline" with polling trigger
    When I POST /api/v1/triggers/{trigger_id}/polling/test with:
      | key                   | value                  |
      | connector_instance_id | "ci-github-issues"     |
      | poll_query            | "select * from issues" |
    Then the response returns status "condition_met" or "no_match"
    And the response includes the matching records
    And no Run is created

  Scenario: Polling interval minimum is enforced
    Given org "acme" has pipeline "my-pipeline"
    When I PATCH /api/v1/triggers/{trigger_id}/polling with:
      | key                   | value                  |
      | poll_interval_seconds | 5                      |
    Then the response status is 422
    And the error mentions "poll_interval_seconds"
