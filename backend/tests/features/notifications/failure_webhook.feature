Feature: Failure Webhook Notification
  As a pipeline operator
  I want to receive webhook notifications when a pipeline run fails
  So that I can respond to failures quickly

  Background:
    Given I am authenticated as an admin in org "acme"

  Scenario: Webhook is sent on run failure
    Given pipeline "my-pipeline" has a failure webhook configured at "https://hooks.example.com/fail"
    And a running pipeline
    When a node raises an unhandled exception
    Then a webhook POST is sent to "https://hooks.example.com/fail"
    And the webhook body contains the run_id and error_detail

  Scenario: Webhook payload includes failure context
    Given pipeline "my-pipeline" has a failure webhook configured
    And a running pipeline
    When a node raises an unhandled exception
    Then the webhook payload includes the failed node name
    And the webhook payload includes the error message

  Scenario: Failure webhook is retried
    Given pipeline "my-pipeline" has a failure webhook configured
    And the failure webhook endpoint returns 500
    When a node raises an unhandled exception
    Then the webhook is retried up to 3 times

  Scenario: Endpoint auto-disabled after repeated failures
    Given pipeline "my-pipeline" has a failure webhook configured
    And the failure webhook endpoint has failed 5 consecutive times
    When a new failure occurs
    Then the webhook endpoint is disabled
    And an alert is logged
