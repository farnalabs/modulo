Feature: Input Sanitization
  As a security-conscious platform
  I want to validate and sanitize all user inputs
  So that injection attacks and malformed data are rejected

  Scenario: Pydantic validates required fields on pipeline create
    Given I am authenticated in org "acme"
    When I POST /api/pipelines with empty JSON body
    Then the response status is 422
    And the error mentions "body.name"

  Scenario: Invalid cron expression is rejected
    Given I am authenticated in org "acme"
    When I create a cron trigger with expression "bad-cron"
    Then the response status is 422
    And the error mentions "cron"

  Scenario: Pipeline graph with no nodes is rejected
    Given org "acme" has pipeline "empty-pipeline"
    And I am authenticated in org "acme"
    When I trigger a run on a pipeline with an empty graph
    Then the response status is 422

  Scenario: Pipeline graph with a cycle is rejected
    Given org "acme" has pipeline "cyclic-pipeline"
    And I am authenticated in org "acme"
    When I trigger a run on a pipeline with a cyclic graph
    Then the response status is 422
    And the error mentions "cycle"

  Scenario: Weak password is rejected by password policy
    Given I am authenticated as an admin in org "acme"
    When I set a weak password "123"
    Then the response status is 422
    And the error mentions "password"
