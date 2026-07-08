Feature: Team Creation
  As an org admin
  I want to create named teams within my organisation
  So that I can group users and scope resources to specific teams

  Scenario: Create a team with valid name
    Given I am authenticated as an admin in org "acme"
    When I POST /api/teams with name "engineering" and description "Engineering team"
    Then the response status is 201
    And the response contains a team with name "engineering"

  Scenario: Create team with duplicate name returns conflict
    Given I am authenticated as an admin in org "acme"
    And a team "engineering" already exists
    When I POST /api/teams with name "engineering" and description "Duplicate"
    Then the response status is 409
    And the error indicates the team name is already taken

  Scenario: Create team with empty name is rejected
    Given I am authenticated as an admin in org "acme"
    When I POST /api/teams with name "" and description "Empty name"
    Then the response status is 422

  Scenario: Create team with very long name is truncated or rejected
    Given I am authenticated as an admin in org "acme"
    When I POST /api/teams with name "a" and description "Minimal name"
    Then the response status is 201

  Scenario: Non-admin cannot create a team
    Given I am authenticated as a viewer in org "acme"
    When I POST /api/teams with name "rogue-team" and description "Unauthorised"
    Then the response status is 403

  Scenario: Team creation returns team id and metadata
    Given I am authenticated as an admin in org "acme"
    When I POST /api/teams with name "qa" and description "QA team"
    Then the response status is 201
    And the response contains id, name, description, and created_at

  Scenario: Team is created with zero members
    Given I am authenticated as an admin in org "acme"
    When I POST /api/teams with name "new-team" and description "Fresh team"
    Then the response status is 201
    And the team has 0 members

