Feature: Prompt Versioning
  As a pipeline author
  I want to version my agent prompts
  So that I can iterate on prompts without breaking existing runs

  Background:
    Given I am authenticated as an admin in org "acme"

  Scenario: Prompt changes create a new version
    Given org "acme" has agent "reviewer" with prompt "Version 1"
    When I update the agent prompt to "Version 2"
    Then the agent has prompt version 2

  Scenario: Pipeline snapshot pins prompt version
    Given org "acme" has agent "reviewer" with prompt "Version 1"
    And the pipeline is published with snapshot
    When I update the agent prompt to "Version 2"
    And I trigger a run using the pinned snapshot
    Then the run uses prompt "Version 1"

  Scenario: Latest prompt version used for new runs
    Given org "acme" has agent "reviewer" with prompt "Version 1"
    When I update the agent prompt to "Version 2"
    And I trigger a new run
    Then the run uses prompt "Version 2"

  Scenario: Prompt version history is preserved
    Given org "acme" has agent "reviewer" with prompt "Version 1"
    When I update the agent prompt to "Version 2"
    And I GET /api/agents/reviewer/versions
    Then the response contains 2 prompt versions
