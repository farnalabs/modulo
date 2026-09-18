Feature: Trigger Event Log
  As a pipeline operator
  I want every trigger event recorded in an immutable log
  So that I can audit what triggered each run

  Background:
    Given I am authenticated as an admin in org "acme"

  Scenario: TriggerEvent log is paginated
    Given org "acme" has trigger "11111111-1111-1111-1111-111111111111" with webhook secret "shared-secret"
    And 25 trigger events have been recorded for the trigger
    When I GET /api/v1/triggers/11111111-1111-1111-1111-111111111111/events?limit=10
    Then the response contains 10 TriggerEvents

  Scenario: Webhook delivery is recorded as a TriggerEvent
    Given org "acme" has trigger "11111111-1111-1111-1111-111111111111" with webhook secret "shared-secret"
    When I POST /api/v1/triggers/11111111-1111-1111-1111-111111111111/webhook with payload {"event": "push"} and valid HMAC
    Then the response status is 202
    And the trigger engine was called for the delivery
