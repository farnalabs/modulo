Feature: View As Team
  As an admin
  I want to view resources as a specific team
  So that I can verify what team members see

  Background:
    Given I am authenticated as an admin in org "acme"

  Scenario: Admin can view as team
    Given a team "docs-team" exists
    When I view resources as team "docs-team"
    Then the response contains only team-visible resources

  Scenario: View as team shows team pipelines
    Given a team "docs-team" exists
    And a pipeline "review-pipeline" is owned by team "docs-team"
    When I view resources as team "docs-team"
    Then I see pipeline "review-pipeline"
