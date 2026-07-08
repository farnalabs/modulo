Feature: Trigger Event Log
  As a pipeline operator
  I want every trigger event recorded in an immutable log
  So that I can audit what triggered each run

  Background:
    Given I am authenticated as an admin in org "acme"

  Scenario: Manual trigger creates TriggerEvent
    Given org "acme" has pipeline "my-pipeline"
    When I POST /api/pipelines/my-pipeline/runs with empty run_context
    Then a TriggerEvent is created with type "manual"
    And the TriggerEvent references the created run

  Scenario: Webhook trigger creates TriggerEvent
    Given org "acme" has pipeline "webhook-pipeline" with webhook secret "shared-secret"
    When I POST /api/webhooks/webhook-pipeline with payload {"event": "push"} and valid HMAC
    Then a TriggerEvent is created with type "webhook"
    And the TriggerEvent has the original payload

  Scenario: TriggerEvent records user identity
    Given I am authenticated in org "acme" as "alice"
    When I trigger a manual run for pipeline "my-pipeline"
    Then the TriggerEvent has triggered_by "alice"

  Scenario: Failed trigger is also logged
    Given org "acme" has pipeline "my-pipeline" with status "paused"
    When I POST /api/pipelines/my-pipeline/runs with empty run_context
    Then a TriggerEvent is created with type "manual"
    And the TriggerEvent has status "failed"
    And the TriggerEvent has error_detail

  Scenario: TriggerEvent log is paginated
    Given org "acme" has pipeline "my-pipeline"
    And 25 manual triggers have been performed
    When I GET /api/triggers/events?limit=10
    Then the response contains 10 TriggerEvents
