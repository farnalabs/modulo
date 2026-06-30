Feature: Team Deletion
  As an admin
  I want to delete teams
  So that I can clean up unused teams

  Background:
    Given I am authenticated as an admin in org "acme"

  Scenario: Delete team with no resources succeeds
    Given a team "docs-team" exists
    And the team has no resources
    When I delete the team "docs-team"
    Then the response status is 204

  Scenario: Delete non-existent team returns 404
    When I delete team by id "00000000-0000-0000-0000-000000000099"
    Then the response status is 404

  Scenario: Non-admin cannot delete a team
    Given I am authenticated as a viewer in org "acme"
    When I delete team by id "00000000-0000-0000-0000-000000000099"
    Then the response status is 403
