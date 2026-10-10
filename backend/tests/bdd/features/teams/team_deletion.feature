Feature: Team Deletion
  As an org admin
  I want to safely delete teams
  So that owned resources are not orphaned and team memberships are cleaned up

  Scenario: Delete team with no owned resources succeeds
    Given I am authenticated as an admin in org "acme"
    And a team "engineering" exists
    And the team owns no resources
    When I delete the team "engineering"
    Then the response status is 204

  Scenario: Delete team with owned resources is blocked
    Given I am authenticated as an admin in org "acme"
    And a team "engineering" exists
    And the team owns 2 resources
    When I delete the team "engineering"
    Then the response status is 409
    And the error indicates the team still has resources

  Scenario: Error message shows owned resource count
    Given I am authenticated as an admin in org "acme"
    And a team "qa" exists
    And the team owns 5 resources
    When I delete the team "qa"
    Then the response status is 409
    And the error message contains "5 pipeline(s)"

  Scenario: Cascading membership cleanup on deletion
    Given I am authenticated as an admin in org "acme"
    And a team "design" exists
    And user "alice" is a member of team "design"
    And user "bob" is a member of team "design"
    And the team owns no resources
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
