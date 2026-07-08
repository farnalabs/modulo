Feature: Cron Triggers
  As a pipeline operator
  I want to schedule pipelines with cron expressions and timezone support
  So that pipelines run automatically on a predictable schedule

  Background:
    Given I am authenticated as an admin in org "acme"

  Scenario: Create cron trigger with full config
    Given org "acme" has pipeline "nightly-report"
    When I create a cron trigger for pipeline "nightly-report" with expression "0 6 * * *" timezone "America/New_York" and input_template {"topic": "summary", "format": "markdown"}
    Then the response status is 201
    And the trigger has cron_expression "0 6 * * *"
    And the trigger has cron_timezone "America/New_York"
    And the trigger has input_template {"topic": "summary", "format": "markdown"}
    And the trigger has a next_fire_at timestamp

  Scenario: Invalid cron expression is rejected
    Given org "acme" has pipeline "nightly-report"
    When I create a cron trigger for pipeline "nightly-report" with expression "not-a-cron"
    Then the response status is 422
    And the error mentions "Invalid cron expression"

  Scenario: Cron trigger fires and creates a run
    Given an active cron trigger exists for pipeline "nightly-report" with expression "0 6 * * *"
    When the cron scheduler fires the cron trigger
    Then a run is created with trigger_type "cron"
    And the run references the cron trigger
    And the trigger's last_fired_at is updated
    And the trigger's next_fire_at is advanced

  Scenario: Daily spend limit stops trigger from firing
    Given an active cron trigger exists for pipeline "nightly-report" with daily_spend_limit "50.00"
    And the pipeline has accumulated "55.00" in run costs today
    When the cron scheduler fires the cron trigger
    Then the trigger is skipped with reason "spend_limit"
    And a TriggerEvent is created with result "spend_limit_reached"

  Scenario: Input template populates the run input on fire
    Given an active cron trigger exists for pipeline "nightly-report" with input_template {"channel": "#alerts", "priority": "P1"}
    When the cron scheduler fires the cron trigger
    Then a run is created with input_payload {"channel": "#alerts", "priority": "P1"}
    And the run has trigger_type "cron"

  Scenario: Trigger event is logged on every fire
    Given an active cron trigger exists for pipeline "nightly-report" with expression "0 * * * *"
    When the cron scheduler fires the cron trigger
    Then a TriggerEvent is created with type "cron"
    And the TriggerEvent has result "accepted"
    And the TriggerEvent references the created run

  Scenario: Cron trigger respects specified timezone
    Given org "acme" has pipeline "daily-digest"
    When I create a cron trigger for pipeline "daily-digest" with expression "0 9 * * *" timezone "America/New_York"
    Then the response status is 201
    And the trigger has cron_timezone "America/New_York"
    And the trigger has cron_expression "0 9 * * *"

  Scenario: Disabled cron trigger does not fire
    Given a deactivated cron trigger exists for pipeline "nightly-report"
    When the cron scheduler fires the cron trigger
    Then the trigger is skipped with reason "trigger_inactive_or_missing"
    And no run is created
