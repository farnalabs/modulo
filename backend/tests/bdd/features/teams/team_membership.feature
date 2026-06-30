Feature: Team Membership
  As an admin or team operator
  I want to manage team membership
  So that users can access team-private resources

  Background:
    Given I am authenticated as an admin in org "acme"

  Scenario: Admin adds a user to a team
    Given a team "docs-team" exists
    And a user "alice" exists
    When I add user "alice" to team "docs-team" with role "viewer"
    Then the response status is 201

  Scenario: Admin removes a user from a team
    Given a team "docs-team" exists
    And a user "alice" exists
    And user "alice" is already a member of team "docs-team"
    When I remove user "alice" from team "docs-team"
    Then the response status is 200

  Scenario: Adding to non-existent team returns 404
    Given a user "alice" exists
    When I add user "alice" to team "nonexistent" with role "viewer"
    Then the response status is 404

  Scenario: Adding with role exceeding org role is rejected
    Given a team "docs-team" exists
    And a user "alice" exists with org role "viewer"
    When I add user "alice" to team "docs-team" with role "operator"
    Then the response status is 422

  Scenario: Duplicate membership is rejected
    Given a team "docs-team" exists
    And a user "alice" exists
    And user "alice" is already a member of team "docs-team"
    When I add user "alice" to team "docs-team" with role "viewer"
    Then the error indicates user is already a member

  Scenario: User profile lists team memberships
    Given a team "docs-team" exists
    And a user "alice" exists
    And user "alice" is a member of team "docs-team"
    When I request my profile
    Then the response lists my team memberships
    And each membership includes team id, team name, and role
