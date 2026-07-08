Feature: Ownership Picker
  As a user creating resources
  I want to choose org-wide or team-scoped visibility
  So that I can control access to my resources without silent defaults

  Scenario: Create resource with org visibility
    Given a team "engineering" exists
    And I am authenticated as an admin in org "acme"
    When I POST /api/stages with name "ci-pipeline" and org visibility
    Then the response status is 201
    And the response visibility is "org"
    And the response owner_team_id is null

  Scenario: Create resource with team visibility
    Given a team "engineering" exists
    And I am authenticated as an admin in org "acme"
    When I POST /api/stages with name "deploy-stage" owned by team "engineering"
    Then the response status is 201
    And the response visibility is "team"
    And the response owner_team_id is set

  Scenario: Missing visibility defaults to org
    Given a team "engineering" exists
    And I am authenticated as an admin in org "acme"
    When I POST /api/stages with name "defaulted-stage" and no visibility
    Then the response status is 201
    And the response visibility is "org"

  Scenario: Ownership shown in resource detail response
    Given a team "engineering" exists
    And I am authenticated as an admin in org "acme"
    When I POST /api/stages with name "audit-stage" owned by team "engineering"
    And I GET /api/stages/audit-stage
    Then the response status is 200
    And the response visibility is "team"
    And the response owner_team_id matches the owning team

  Scenario: Non-member cannot access team-scoped resource
    Given a team "engineering" exists
    And user "alice" is a member of team "engineering"
    And user "bob" is not a member of team "engineering"
    And stage "secret-stage" is owned by team "engineering"
    When user "bob" requests GET /api/stages/secret-stage
    Then the response status is 404

  Scenario: Team member can access team-scoped resource
    Given a team "engineering" exists
    And user "alice" is a member of team "engineering"
    And stage "secret-stage" is owned by team "engineering"
    When user "alice" requests GET /api/stages/secret-stage
    Then the response status is 200
    And the response visibility is "team"

  Scenario: Admin bypasses team isolation
    Given a team "engineering" exists
    And user "alice" is a member of team "engineering"
    And stage "secret-stage" is owned by team "engineering"
    When I am authenticated as an admin in org "acme"
    And I GET /api/stages/secret-stage
    Then the response status is 200
    And the response visibility is "team"
