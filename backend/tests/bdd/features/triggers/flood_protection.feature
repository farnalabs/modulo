Feature: Webhook Flood Protection
  As a pipeline operator
  I want duplicate webhook events deduplicated and flood protection enforced
  So that my pipelines are not overwhelmed by rapid event bursts

  Background:
    Given I am authenticated as an admin in org "acme"

  Scenario: Duplicate webhook is rejected
    Given org "acme" has trigger "11111111-1111-1111-1111-111111111111" with webhook secret "shared-secret"
    When I POST /api/v1/triggers/11111111-1111-1111-1111-111111111111/webhook with payload {"id": "evt-1"} and valid HMAC raising duplicate
    Then the response status is 400
    And the error mentions "Duplicate"

  Scenario: Rapid webhooks are rate limited
    Given org "acme" has trigger "11111111-1111-1111-1111-111111111111" with webhook secret "shared-secret"
    When I POST /api/v1/triggers/11111111-1111-1111-1111-111111111111/webhook with payload {"id": "evt-1"} and valid HMAC raising rate_limit
    Then the response status is 429
