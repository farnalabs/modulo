Feature: Webhook Flood Protection
  As a pipeline operator
  I want duplicate webhook events deduplicated and flood protection enforced
  So that my pipelines are not overwhelmed by rapid event bursts

  Background:
    Given I am authenticated as an admin in org "acme"

  Scenario: Duplicate webhook is rejected
    Given org "acme" has pipeline "webhook-pipeline" with webhook secret "shared-secret"
    And I POST /api/webhooks/webhook-pipeline with payload {"id": "evt-1"} and valid HMAC
    When I POST /api/webhooks/webhook-pipeline with same payload {"id": "evt-1"} and valid HMAC
    Then the response status is 409
    And the error mentions "duplicate"

  Scenario: Rapid webhooks are rate limited
    Given org "acme" has pipeline "webhook-pipeline" with webhook secret "shared-secret"
    When I send 10 webhooks in rapid succession
    Then the 11th webhook is rate limited
    And the response status is 429

  Scenario: Deduplication uses input_hash
    Given org "acme" has pipeline "webhook-pipeline" with webhook secret "shared-secret"
    When I POST /api/webhooks/webhook-pipeline with payload {"data": "same"} and valid HMAC
    And I POST /api/webhooks/webhook-pipeline with payload {"data": "same"} and valid HMAC
    Then the second request returns 409
    And only 1 run was created

  Scenario: Different payloads are not deduplicated
    Given org "acme" has pipeline "webhook-pipeline" with webhook secret "shared-secret"
    When I POST /api/webhooks/webhook-pipeline with payload {"id": "evt-1"} and valid HMAC
    And I POST /api/webhooks/webhook-pipeline with payload {"id": "evt-2"} and valid HMAC
    Then the response status is 202 for both
    And 2 runs are created
