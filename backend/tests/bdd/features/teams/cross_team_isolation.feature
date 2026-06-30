Feature: Cross-Team Isolation
  As a team member
  I want to be unable to access resources owned by another team
  So that team boundaries are enforced

  Background:
    Given I am authenticated as a user in org "acme"

  Scenario: Team member cannot access another team's pipeline
    Given a team "docs-team" exists
    And a team "legal-team" exists
    And I am a member of team "docs-team"
    And a pipeline "legal-pipeline" is owned by team "legal-team"
    When I view pipelines
    Then I do not see pipeline "legal-pipeline"
