Feature: Copy to Adapt Library Primitives
  As a pipeline author
  I want to copy community primitives to my org and adapt them
  So that I can reuse proven workflows

  Background:
    Given the organisation has 3 local primitives
    And 5 community primitives exist

  Scenario: Copy a community primitive via browser API
    Given a specific primitive exists with id "00000000-0000-0000-0000-000000000010"
    When the user sends POST /api/v1/libraries/00000000-0000-0000-0000-000000000010/adapt
    Then the response status is 201
    And a new library primitive is created in the org
    And the new primitive has source "local"
    And the new primitive has forked_from set to the community primitive id

  Scenario: MCP client cannot copy community primitive
    Given a specific primitive exists with id "00000000-0000-0000-0000-000000000010"
    When an MCP client sends copy_library_primitive with the community primitive id
    Then the response status is 403
    And the response detail explains the browser UI must be used

  Scenario: Adapt a primitive to a team
    Given a specific primitive exists with id "00000000-0000-0000-0000-000000000010"
    When the user sends POST /api/v1/libraries/00000000-0000-0000-0000-000000000010/adapt with target_team_id
    Then the response status is 201
    And the new primitive has owner_team_id set to the requested team

  Scenario: Copy non-existent primitive returns 404
    When the user sends POST /api/v1/libraries/00000000-0000-0000-0000-00000000ffff/adapt
    Then the response status is 404
