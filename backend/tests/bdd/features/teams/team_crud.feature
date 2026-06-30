Feature: Team CRUD
  As an admin
  I want to create, read, update, and delete teams
  So that I can organise users and resources

  Background:
    Given I am authenticated as an admin in org "acme"

  Scenario: Admin creates a team
    Given a team "docs-team" does not exist
    When I create a team with name "docs-team" and description "Documentation pipelines"
    Then the response status is 201
    And the response contains a team with name "docs-team"

  Scenario: Admin lists teams
    Given a team "docs-team" exists
    When I list teams
    Then the response status is 200
    And the response contains a list of teams

  Scenario: Admin gets a team by ID
    Given a team "docs-team" exists
    When I get team "docs-team"
    Then the response status is 200
    And the response contains a team with name "docs-team"

  Scenario: Non-admin cannot create a team
    Given I am authenticated as a viewer in org "acme"
    When I create a team with name "docs-team" and description "x"
    Then the response status is 403

  Scenario: Duplicate team name is rejected
    Given a team "docs-team" exists
    When I create a team with name "docs-team" and description "duplicate"
    Then the response status is 409

  Scenario: Empty team name is rejected
    When I create a team with name "" and description "empty"
    Then the response status is 422

  Scenario: Admin renames a team
    Given a team "docs-team" exists
    When I rename team "docs-team" to "documentation-team"
    Then the response status is 200
    And the response contains a team with name "documentation-team"

  Scenario: Rename to duplicate name is rejected
    Given a team "docs-team" exists
    And a team "legal-team" exists
    When I rename team "docs-team" to "legal-team"
    Then the response status is 409

  Scenario: Admin deletes a team
    Given a team "docs-team" exists
    And the team has no resources
    When I delete the team "docs-team"
    Then the response status is 204

  Scenario: Fetching non-existent team returns 404
    When I get team by id "00000000-0000-0000-0000-000000000099"
    Then the response status is 404

  Scenario: Deleting non-existent team returns 404
    When I delete team by id "00000000-0000-0000-0000-000000000099"
    Then the response status is 404
