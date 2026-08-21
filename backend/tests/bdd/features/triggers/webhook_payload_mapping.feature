Feature: Webhook Payload Mapping
  As a pipeline operator
  I want to map incoming webhook payload fields to run_context
  So that external event data is available to pipeline agents

  Background:
    Given I am authenticated as an admin in org "acme"

  Scenario: Webhook payload is forwarded to the trigger engine
    Given org "acme" has trigger "11111111-1111-1111-1111-111111111111" with webhook secret "shared-secret"
    When I POST /api/v1/triggers/11111111-1111-1111-1111-111111111111/webhook with payload {"ref": "refs/heads/main"} and valid HMAC
    Then the response status is 202
    And the trigger engine received the raw payload

  Scenario: Webhook payload is delivered to the pipeline run
    Given org "acme" has trigger "11111111-1111-1111-1111-111111111111" with webhook secret "shared-secret"
    When I POST /api/v1/triggers/11111111-1111-1111-1111-111111111111/webhook with payload {"event": "push"} and valid HMAC
    Then the response status is 202
    And the trigger engine was called for the delivery
