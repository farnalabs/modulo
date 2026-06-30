Feature: View As Team - Non-Admin Rejected
  As a non-admin user
  I want to be prevented from using view_as_team
  So that I cannot bypass team visibility boundaries

  Background:
    Given I am authenticated as a user in org "acme"

  Scenario: Non-admin cannot view as team
    Given a team "docs-team" exists
    When I view resources as team "docs-team"
    Then the response status is 403
