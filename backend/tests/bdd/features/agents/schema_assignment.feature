Feature: Agent Schema Assignment
  As a pipeline author
  I want to assign input and output schemas to agents
  So that agent inputs and outputs are validated

  Background:
    Given I am authenticated as an admin in org "acme"

  Scenario: Assign input schema to agent
    Given org "acme" has agent "reviewer"
    And a schema "review-input" exists with fields title, content
    When I assign schema "review-input" as input to agent "reviewer"
    Then the agent input schema is "review-input"

  Scenario: Assign output schema to agent
    Given org "acme" has agent "reviewer"
    And a schema "review-output" exists with fields score, summary
    When I assign schema "review-output" as output to agent "reviewer"
    Then the agent output schema is "review-output"

  Scenario: Agent validates output against schema
    Given agent "reviewer" has output schema "review-output"
    When the agent produces output matching the schema
    Then the output is accepted
    When the agent produces output violating the schema
    Then the output is rejected with a validation error

  Scenario: Schema assignment persists across pipeline save
    Given agent "reviewer" has input schema "review-input"
    When I save and reload the pipeline
    Then the agent still has input schema "review-input"

  Scenario: Remove schema assignment
    Given agent "reviewer" has input schema "review-input"
    When I remove the input schema assignment from agent "reviewer"
    Then the agent has no input schema
