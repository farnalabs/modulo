Feature: Pipeline Creation
  As a pipeline author
  I want to create pipelines with agents, connectors, and model backend assignments
  So that I can define agentic workflows

  Background:
    Given I am authenticated as an admin in org "acme"

  Scenario: Create a minimal pipeline
    When I POST /api/pipelines with name "minimal" and valid config
    Then the response status is 201
    And the response contains id and slug

  Scenario: Create a pipeline with an LLM node
    When I POST /api/pipelines with name "llm-pipeline" and a single LLM node config
    Then the response status is 201
    And the response contains id and slug

  Scenario: Create a pipeline with a manual node
    When I POST /api/pipelines with name "manual-step" and a manual node config
    Then the response status is 201
    And the pipeline has a manual node

  Scenario: Create a pipeline with run_context defaults
    When I POST /api/pipelines with name "context-pipeline" and run_context defaults
    Then the response status is 201
    And the pipeline has run_context defaults

  Scenario: Duplicate pipeline name is rejected
    Given org "acme" has pipeline "my-pipeline"
    When I POST /api/pipelines with name "my-pipeline" and valid config
    Then the response status is 409
