Feature: Pipeline Run Variants (A/B Testing)
  As a pipeline operator
  I want to define variant groups with weighted variants
  So that I can A/B test different pipeline configurations

  Scenario: Create a variant group with weighted variants
    Given org "acme" has pipeline "deploy-service"
    And I am authenticated in org "acme"
    When I POST /api/v1/variant-groups with valid variant group configuration
    Then the response status is 201
    And the response contains id and slug

  Scenario: Trigger a run on a variant group
    Given a variant group "ab-test-1" exists for pipeline "deploy-service"
    And I am authenticated in org "acme"
    When I POST /api/v1/variant-groups/ab-test-1/run with empty input_payload
    Then the response status is 200
    And the response contains a variant_name and run_id

  Scenario: Coverage gaps are reported for a variant group
    Given a variant group "ab-test-1" exists for pipeline "deploy-service"
    And I am authenticated in org "acme"
    When I GET /api/v1/variant-groups/ab-test-1/coverage-gaps
    Then the response status is 200
    And the response lists missing eval definitions per variant

  Scenario: Non-existent variant group returns 404
    Given I am authenticated in org "acme"
    When I GET /api/v1/variant-groups/00000000-0000-0000-0000-000000000999
    Then the response status is 404

  Scenario: Run quota exceeded on variant group returns 429
    Given a variant group "ab-test-1" exists for pipeline "deploy-service" at max concurrency
    And I am authenticated in org "acme"
    When I POST /api/v1/variant-groups/ab-test-1/run with empty input_payload
    Then the response status is 429
