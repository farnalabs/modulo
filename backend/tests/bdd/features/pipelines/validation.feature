Feature: Pipeline Validation
  As a pipeline author
  I want my pipeline configuration validated before saving
  So that errors are caught early

  Background:
    Given I am authenticated as an admin in org "acme"

  Scenario: Reject pipeline with missing required field
    When I POST /api/pipelines with config missing "name"
    Then the response status is 422
    And the error mentions "name"

  Scenario: Reject unknown node type
    When I POST /api/pipelines with a node of type "unknown_type"
    Then the response status is 422
    And the error mentions "unknown_type"

  Scenario: Reject circular dependency
    When I POST /api/pipelines with a config where node A depends on B and B depends on A
    Then the response status is 422
    And the error mentions "circular dependency"

  Scenario: Accept valid minimal config
    When I POST /api/pipelines with a single LLM node config
    Then the response status is 201

  Scenario: Reject pipeline with no nodes
    When I POST /api/pipelines with name "empty" and an empty graph
    Then the response status is 422
    And the error mentions "at least one node"

  Scenario: Reject pipeline referencing missing connector
    Given no connector named "my-connector" exists
    When I POST /api/pipelines with a node referencing connector "my-connector"
    Then the response status is 422
    And the error mentions "connector"
