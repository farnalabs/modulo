Feature: Webhook Trigger with HMAC Verification
  As a pipeline operator
  I want to trigger pipeline runs via webhook with HMAC-SHA256 signature verification
  So that external systems can securely trigger workflows

  Background:
    Given I am authenticated as an admin in org "acme"

  Scenario: Valid HMAC triggers a run
    Given org "acme" has pipeline "webhook-pipeline" with webhook secret "shared-secret"
    When I POST /api/webhooks/webhook-pipeline with payload {"event": "push"} and valid HMAC
    Then the response status is 202
    And a run is created with trigger_type "webhook"

  Scenario: Invalid HMAC is rejected
    Given org "acme" has pipeline "webhook-pipeline" with webhook secret "shared-secret"
    When I POST /api/webhooks/webhook-pipeline with payload {"event": "push"} and invalid HMAC
    Then the response status is 401
    And the error mentions "invalid signature"

  Scenario: Missing HMAC header is rejected
    Given org "acme" has pipeline "webhook-pipeline" with webhook secret "shared-secret"
    When I POST /api/webhooks/webhook-pipeline with payload {"event": "push"} and no HMAC
    Then the response status is 401

  Scenario: Webhook without secret configured is rejected
    Given org "acme" has pipeline "webhook-pipeline" with no webhook secret
    When I POST /api/webhooks/webhook-pipeline with payload {"event": "push"} and valid HMAC
    Then the response status is 404

  Scenario: Webhook payload is recorded in trigger event log
    Given org "acme" has pipeline "webhook-pipeline" with webhook secret "shared-secret"
    When I POST /api/webhooks/webhook-pipeline with payload {"event": "push"} and valid HMAC
    Then a TriggerEvent is created with type "webhook"
    And the TriggerEvent has payload {"event": "push"}
