Feature: Team CRUD Operations
  As an org admin
  I want to create, read, update, and delete teams
  So that I can manage team structure within my organisation

  Background:
    Given I am authenticated as an admin in org "acme"

  Scenario: Create a team with name and description
    When I create a team with name "engineering" and description "Engineering department"
    Then the response status is 201
    And the response contains a team with name "engineering"
    And the team has an account_id

  Scenario: Create team with duplicate name returns conflict
    Given a team "engineering" already exists
    When I create a team with name "engineering" and description "Duplicate"
    Then the response status is 409

  Scenario: Non-admin cannot create a team
    Given I am authenticated as a viewer in org "acme"
    When I create a team with name "rogue" and description "Unauthorised"
    Then the response status is 403

  Scenario: Empty team name is rejected
    When I create a team with name "" and description "Empty name"
    Then the response status is 422

  Scenario: List teams returns paginated results
    When I list teams
    Then the response status is 200
    And the response contains a list of teams

  Scenario: Get team by id returns the team
    Given a team "engineering" exists
    When I get team "engineering"
    Then the response status is 200
    And the response contains a team with name "engineering"

  Scenario: Get non-existent team returns 404
    When I get team by id "00000000-0000-0000-0000-000000099999"
    Then the response status is 404

  Scenario: Update team name
    Given a team "engineering" exists
    When I rename team "engineering" to "platform"
    Then the response status is 200
    And the response contains a team with name "platform"

  Scenario: Update team to duplicate name returns conflict
    Given a team "engineering" exists
    And a team "platform" exists
    When I rename team "engineering" to "platform"
    Then the response status is 409

  Scenario: Delete team with no owned resources succeeds
    Given a team "engineering" exists
    When I delete team "engineering"
    Then the response status is 204

  Scenario: Delete non-existent team returns 404
    When I delete team by id "00000000-0000-0000-0000-000000099999"
    Then the response status is 404

  Scenario: Non-admin cannot delete a team
    Given I am authenticated as a viewer in org "acme"
    When I delete team by id "00000000-0000-0000-0000-000000000001"
    Then the response status is 403
