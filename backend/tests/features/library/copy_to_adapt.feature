Feature: Copy to Adapt Library Primitives
  As a pipeline author
  I want to copy community primitives to my org and adapt them
  So that I can reuse proven workflows

  Background:
    Given the organisation has 3 local primitives
    And 5 community primitives exist

  Scenario: Copy a community primitive via browser API
    Given a specific primitive exists with id "comm-001"
    When the user sends POST /api/v1/libraries/comm-001/copy
    Then the response status is 201
    And a new library primitive is created in the org
    And the new primitive has source "local"
    And the new primitive has forked_from "comm-001"

  Scenario: MCP client cannot copy community primitive
    Given a specific primitive exists with id "comm-001"
    When an MCP client sends copy_library_primitive with id "comm-001"
    Then the response status is 403
    And the response detail explains the browser UI must be used

  Scenario: Adapt a primitive to a team
    Given a specific primitive exists with id "local-001"
    And the org has a team "data-team"
    When the user sends POST /api/v1/libraries/local-001/adapt with target_team_id "data-team"
    Then the response status is 201
    And the new primitive has owner_team_id "data-team"

  Scenario: Copy non-existent primitive returns 404
    When the user sends POST /api/v1/libraries/non-existent/copy
    Then the response status is 404
