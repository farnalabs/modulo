Feature: Team Membership Management
  As a team operator or org admin
  I want to add and remove members from teams
  So that access to team-scoped resources is correctly granted and revoked

  Scenario: Admin adds a user to a team
    Given I am authenticated as an admin in org "acme"
    And a team "engineering" exists
    And a user "alice" exists
    When I add user "alice" to team "engineering" with role "operator"
    Then the response status is 200
    And user "alice" is a member of team "engineering"

  Scenario: Team operator adds a member to their own team
    Given I am authenticated as a team operator of team "engineering"
    And a team "engineering" exists
    And a user "bob" exists
    When I add user "bob" to team "engineering" with role "viewer"
    Then the response status is 200

  Scenario: Team operator cannot promote beyond their own role
    Given I am authenticated as a team operator of team "engineering"
    And a team "engineering" exists
    And a user "charlie" exists
    When I add user "charlie" to team "engineering" with role "operator"
    Then the response status is 403

  Scenario: Remove a user from a team revokes access
    Given I am authenticated as an admin in org "acme"
    And a team "engineering" exists
    And user "alice" is a member of team "engineering"
    And a team-scoped pipeline "secret-pipeline" is owned by team "engineering"
    When I remove user "alice" from team "engineering"
    Then the response status is 200
    And user "alice" cannot access team "engineering" resources

  Scenario: Add user to non-existent team returns 404
    Given I am authenticated as an admin in org "acme"
    And a user "alice" exists
    When I add user "alice" to team "00000000-0000-0000-0000-000000099999" with role "operator"
    Then the response status is 404

  Scenario: Duplicate membership is rejected
    Given I am authenticated as an admin in org "acme"
    And a team "engineering" exists
    And user "alice" is already a member of team "engineering"
    When I add user "alice" to team "engineering" with role "operator"
    Then the response status is 409
    And the error indicates user is already a member

  Scenario: View user's team memberships
    Given I am authenticated as a user in org "acme"
    And I am a member of team "engineering"
    And I am a member of team "design"
    When I request my profile
    Then the response lists my team memberships
    And each membership includes team id, team name, and role

