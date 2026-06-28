Feature: Webhook Trigger
  As a pipeline operator
  I want to trigger pipeline runs via webhook with HMAC authentication
  So that external systems can securely trigger pipelines

  Scenario: Webhook with valid HMAC creates a run
    Given org "acme" has pipeline "ci-pipeline" with webhook secret "s3cr3t"
    And I am authenticated in org "acme"
    When I POST a webhook with valid HMAC and timestamp to trigger "ci-trigger"
    Then the response status is 202
    And a run is created with status "pending"

  Scenario: Webhook with invalid HMAC is rejected
    Given org "acme" has pipeline "ci-pipeline" with webhook secret "s3cr3t"
    And I am authenticated in org "acme"
    When I POST a webhook with invalid HMAC to trigger "ci-trigger"
    Then the response status is 401
    And the error mentions "HMAC"

  Scenario: Webhook with expired timestamp is rejected
    Given org "acme" has pipeline "ci-pipeline" with webhook secret "s3cr3t"
    And I am authenticated in org "acme"
    When I POST a webhook with expired timestamp to trigger "ci-trigger"
    Then the response status is 400
    And the error mentions "timestamp"

  Scenario: Duplicate webhook payload is rejected
    Given org "acme" has pipeline "ci-pipeline" with webhook secret "s3cr3t"
    And I am authenticated in org "acme"
    When I POST a duplicate webhook payload to trigger "ci-trigger"
    Then the response status is 400
    And the error mentions "Duplicate"

  Scenario: Flood protection rejects when at max concurrent runs
    Given org "acme" has pipeline "ci-pipeline" with webhook secret "s3cr3t"
    And the pipeline is at max concurrent runs
    And I am authenticated in org "acme"
    When I POST a webhook with valid HMAC and timestamp to trigger "ci-trigger"
    Then the response status is 429
    And the error mentions "Concurrent run limit"
