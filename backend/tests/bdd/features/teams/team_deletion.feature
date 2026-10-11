Feature: Team Deletion
  As an org admin
  I want to safely delete teams
  So that owned resources are not orphaned and the deleted team is no longer reachable
  # Deletion reality: ``delete_team`` soft-deletes (sets ``team.deleted_at``)
  # and leaves the row — and its team_memberships rows — in place, so no FK
  # cascade fires; ``get_team`` / ``list_teams`` filter the deleted row out and
  # the team 404s on lookup from then on.

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

  Scenario: Deleting a team hides it from team lookups
    # The soft delete does NOT cascade team_memberships (the FK cascade is
    # hard-delete only). The observable contract: deletion succeeds and the
    # team is no longer retrievable.
    Given I am authenticated as an admin in org "acme"
    And a team "design" exists
    And user "alice" is a member of team "design"
    When I delete the team "design"
    Then the response status is 204
    And the team "design" is no longer retrievable

  Scenario: Non-admin cannot delete team
    Given I am authenticated as a viewer in org "acme"
    And a team "engineering" exists
    When I delete the team "engineering"
    Then the response status is 403

  Scenario: Delete non-existent team returns 404
    Given I am authenticated as an admin in org "acme"
    When I delete the team "00000000-0000-0000-0000-000000009999"
    Then the response status is 404
    And the error message contains "Team not found"
