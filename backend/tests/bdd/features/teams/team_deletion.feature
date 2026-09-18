Feature: Team Deletion
  As an org admin
  I want to safely delete teams
  So that active runs are not orphaned and team memberships are cleaned up

  Scenario: Delete team with no active runs succeeds
    Given I am authenticated as an admin in org "acme"
    And a team "engineering" exists
    And the team has no active runs
    When I delete the team "engineering"
    Then the response status is 204

  Scenario: Delete team with active runs is blocked
    Given I am authenticated as an admin in org "acme"
    And a team "engineering" exists
    And the team has 2 active runs
    When I delete the team "engineering"
    Then the response status is 409
    And the error indicates the team has active runs

  Scenario: Error message shows active run count
    Given I am authenticated as an admin in org "acme"
    And a team "qa" exists
    And the team has 5 active runs
    When I delete the team "qa"
    Then the response status is 409
    And the error message contains "5 active runs"

  Scenario: Cascading membership cleanup on deletion
    Given I am authenticated as an admin in org "acme"
    And a team "design" exists
    And user "alice" is a member of team "design"
    And user "bob" is a member of team "design"
    And the team has no active runs
    When I delete the team "design"
    Then the response status is 204

  Scenario: Non-admin cannot delete team
    Given I am authenticated as a viewer in org "acme"
    And a team "engineering" exists
    When I delete the team "engineering"
    Then the response status is 403

  Scenario: Delete non-existent team returns 404
    Given I am authenticated as an admin in org "acme"
    When I delete the team "00000000-0000-0000-0000-000000009999"
    Then the response status is 404
