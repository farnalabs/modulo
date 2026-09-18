Feature: Agent CRUD — /api/v1/agents
  As a pipeline author
  I want to manage agents via the REST API
  So that I can create, read, update, and delete agent definitions

  Background:
    Given I am authenticated as an admin in org "acme"

  Scenario: List agents returns paginated results
    When I GET /api/v1/agents
    Then the response status is 200
    And the response contains a list of agents

  Scenario: Create an executable generic agent succeeds
    When I create agent "code-review" with description "Reviews pull requests"
    Then the response status is 201
    And the agent name is "code-review"

  Scenario: Create generic agent without description returns 422
    When I create generic agent "bad-agent" without a description
    Then the response status is 422
    And the error mentions "description"

  Scenario: Create library-sourced agent without description succeeds
    When I create a library agent without a description
    Then the response status is 201

  Scenario: Get agent by ID returns the agent
    Given an agent exists with name "test-agent"
    When I GET the agent by ID
    Then the response status is 200
    And the agent name is "test-agent"

  Scenario: Get non-existent agent returns 404
    Given a non-existent agent ID
    When I GET the agent by ID
    Then the response status is 404

  Scenario: Update agent name succeeds
    Given an agent exists with name "old-name"
    When I update the agent name to "new-name"
    Then the response status is 200
    And the agent name is "new-name"

  Scenario: Delete agent returns 204
    Given an agent exists with name "delete-me"
    When I delete the agent
    Then the response status is 204

  Scenario: Delete non-existent agent returns 404
    Given a non-existent agent ID
    When I delete the agent
    Then the response status is 404
