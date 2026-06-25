Feature: Pipeline Config Validation
  As a pipeline author
  I want invalid pipeline configs to be rejected with clear errors
  So that I cannot save a broken pipeline

  Scenario: Missing required field is rejected
    Given I am authenticated as an admin in org "acme"
    When I POST /api/pipelines with config missing "nodes"
    Then the response status is 422
    And the error mentions "nodes"

  Scenario: Unknown node type is rejected
    When I POST /api/pipelines with a node of type "does_not_exist"
    Then the response status is 422

  Scenario: Cycle in node graph is rejected
    When I POST /api/pipelines with a config where node A depends on B and B depends on A
    Then the response status is 422
    And the error mentions "cycle"

  Scenario: Valid minimal pipeline is accepted
    When I POST /api/pipelines with a single LLM node config
    Then the response status is 201
