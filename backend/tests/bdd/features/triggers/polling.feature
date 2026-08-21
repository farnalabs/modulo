Feature: Polling Trigger
  As a pipeline operator
  I want triggers that poll external systems on a schedule
  So that my pipelines react to conditions in third-party services

  Scenario: Polling trigger fires when condition is met
    Given org "acme" has pipeline "monitor-issues" with polling config
    And the connector returns records matching "[?status=='open']"
    When the polling scheduler runs and evaluates the condition
    Then a Run is created with trigger_type "polling"
    And a TriggerEvent is created with result "condition_met"
    And the run references the polling trigger

  Scenario: Polling trigger does not fire when condition is not met
    Given org "acme" has pipeline "monitor-issues" with polling config
    And the connector returns no matching records
    When the polling scheduler runs and evaluates the condition
    Then no Run is created
    And a TriggerEvent is created with result "no_match"

  Scenario: Polling trigger respects max_concurrent_runs
    Given org "acme" has pipeline "monitor-issues" with polling config
    And the pipeline has 5 active runs
    And the trigger max_concurrent_runs is 3
    When the polling scheduler runs
    Then no Run is created
    And a TriggerEvent is created with result "concurrency_limit_reached"

  Scenario: Polling trigger respects the daily spend limit
    Given org "acme" has pipeline "monitor-issues" with polling config
    And the trigger has a daily spend limit of 50.00
    And the pipeline has accumulated 55.00 in run costs today
    When the polling scheduler runs
    Then no Run is created
    And a TriggerEvent is created with result "spend_limit_reached"

  Scenario: Polling trigger fires when below the daily spend limit
    Given org "acme" has pipeline "monitor-issues" with polling config
    And the trigger has a daily spend limit of 100.00
    And the pipeline has accumulated 55.00 in run costs today
    When the polling scheduler runs
    Then a Run is created with trigger_type "polling"

  Scenario: Polling trigger with no daily spend limit always fires
    Given org "acme" has pipeline "monitor-issues" with polling config
    And the pipeline has accumulated 9999.00 in run costs today
    When the polling scheduler runs
    Then a Run is created with trigger_type "polling"

  Scenario: Invalid JMESPath expression logs poll_error
    Given org "acme" has pipeline "monitor-issues" with polling config
    And the condition_expression is "[invalid: syntax"
    When the polling scheduler runs
    Then no Run is created
    And a TriggerEvent is created with result "poll_error"
    And the error_detail mentions "invalid JMESPath"

  Scenario: Connector failure logs poll_error
    Given org "acme" has pipeline "monitor-issues" with polling config
    And the connector query fails
    When the polling scheduler runs
    Then no Run is created
    And a TriggerEvent is created with result "poll_error"

  Scenario: Inactive trigger is skipped
    Given org "acme" has pipeline "monitor-issues" with polling config
    And the trigger is deactivated
    When the polling scheduler runs
    Then no Run is created
    And the trigger is skipped with reason "trigger_inactive_or_missing"
