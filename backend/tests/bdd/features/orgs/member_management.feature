Feature: Member Management
  As an org admin
  I want to manage team membership
  So that users have appropriate access to teams and resources

  Scenario: Add member to team
    Given I am authenticated as an admin in org "acme"
    And a team "engineering" exists
    When I add user "alice" to team "engineering" with role "operator"
    Then the response status is 201
    And the membership has role "operator"

  Scenario: Remove member from team
    Given I am authenticated as an admin in org "acme"
    And user "bob" is a member of team "engineering"
    When I remove "bob" from team "engineering"
    Then the response status is 204
    And "bob" is no longer a member

  Scenario: Non-admin cannot add members
    Given I am authenticated as a viewer in org "acme"
    When I add user "alice" to team "engineering" with role "operator"
    Then the response status is 403

  Scenario: Team role cannot exceed org role
    Given I am authenticated as an admin in org "acme"
    And user "charlie" has org role "viewer"
    When I add user "charlie" to team "engineering" with role "operator"
    Then the response status is 422

  Scenario: Deactivate user removes access
    Given I am authenticated as an admin in org "acme"
    And user "dave" is active in the org
    When I deactivate user "dave"
    Then user "dave" is deactivated
