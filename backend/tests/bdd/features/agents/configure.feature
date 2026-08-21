Feature: Agent Configuration
  As a pipeline author
  I want to configure agents with prompts, schemas, and model backends
  So that each node in my pipeline has the right instructions

  Background:
    Given I am authenticated as an admin in org "acme"

  Scenario: Create an agent with a system prompt
    Given I create an agent named "reviewer" with system prompt "Review the code for bugs"
    When I GET /api/agents
    Then the response contains agent "reviewer"
    And the agent has system prompt "Review the code for bugs"

  Scenario: Update agent prompt
    Given org "acme" has agent "reviewer" with prompt "Old prompt"
    When I PATCH /api/agents/reviewer with prompt "New prompt"
    Then the response status is 200
    And the agent prompt is "New prompt"

  Scenario: Assign a schema to an agent
    Given org "acme" has agent "reviewer"
    And org "acme" has schema "code-review-schema"
    When I assign schema "code-review-schema" to agent "reviewer"
    Then the agent has schema "code-review-schema"

  Scenario: Delete an agent
    Given org "acme" has agent "temporary-agent"
    When I DELETE /api/agents/temporary-agent
    Then the response status is 204
    And the agent no longer exists

  Scenario: Agent with no schema can still run
    Given org "acme" has agent "freeform" with prompt "Answer freely"
    When I inspect the agent configuration
    Then the agent has no input schema
