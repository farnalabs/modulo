Feature: Copy primitive to adapt
  Users can copy a library primitive into their org workspace to adapt it.
  Community primitives are read-only via MCP (403) but can be adapted via the browser UI.

  Background:
    Given the organisation exists
    And a community primitive "PRD Input Schema" exists

  Scenario: Adapt a community primitive via browser succeeds
    When the user sends POST /api/v1/libraries/{community_primitive_id}/adapt
    Then the response status is 201
    And a new library primitive is created in the org
    And the new primitive has source "local"
    And the new primitive has forked_from set to the community primitive id

  Scenario: Adapt a community primitive via MCP returns 403
    When an MCP client sends copy_library_primitive with the community primitive id
    Then the response contains error "community_primitive_read_only"
    And the response detail explains the browser UI must be used

  Scenario: Adapt with team assignment
    When the user sends POST /api/v1/libraries/{primitive_id}/adapt with target_team_id
    Then the new primitive has owner_team_id set to the requested team

  Scenario: Adapt a non-existent primitive returns 404
    When the user sends POST /api/v1/libraries/00000000-0000-0000-0000-000000099999/adapt
    Then the response status is 404
