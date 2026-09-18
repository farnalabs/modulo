Feature: Webhook Trigger with HMAC Verification
  As a pipeline operator
  I want to trigger pipeline runs via webhook with HMAC-SHA256 signature verification
  So that external systems can securely trigger workflows

  Background:
    Given I am authenticated as an admin in org "acme"

  Scenario: Valid HMAC triggers a run
    Given org "acme" has trigger "11111111-1111-1111-1111-111111111111" with webhook secret "shared-secret"
    When I POST /api/v1/triggers/11111111-1111-1111-1111-111111111111/webhook with payload {"event": "push"} and valid HMAC
    Then the response status is 202
    And the webhook is accepted

  Scenario: Invalid HMAC is rejected
    Given org "acme" has trigger "11111111-1111-1111-1111-111111111111" with webhook secret "shared-secret"
    When I POST /api/v1/triggers/11111111-1111-1111-1111-111111111111/webhook with payload {"event": "push"} and invalid HMAC
    Then the response status is 401
    And the error mentions "HMAC"

  Scenario: Missing HMAC header is rejected
    Given org "acme" has trigger "11111111-1111-1111-1111-111111111111" with webhook secret "shared-secret"
    When I POST /api/v1/triggers/11111111-1111-1111-1111-111111111111/webhook with payload {"event": "push"} and no HMAC
    Then the response status is 401

  Scenario: Webhook for a non-existent trigger is rejected
    Given no trigger exists with id "22222222-2222-2222-2222-222222222222"
    When I POST /api/v1/triggers/22222222-2222-2222-2222-222222222222/webhook with payload {"event": "push"} and valid HMAC
    Then the response status is 404
