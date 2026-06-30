Feature: Admin Override
  As an admin
  I want to bypass team restrictions
  So that I can manage all resources in the organisation

  Background:
    Given I am authenticated as an admin in org "acme"

  Scenario: Admin sees all team-private resources
    Given a team "docs-team" exists
    And a pipeline "review-pipeline" is owned by team "docs-team"
    When I view pipelines
    Then I see pipeline "review-pipeline"

  Scenario: Admin can reassign team resources
    Given a team "docs-team" exists
    And a pipeline "review-pipeline" is owned by team "docs-team"
    When I reassign all resources from team "docs-team" to org-wide
    Then the pipeline "review-pipeline" is no longer team-owned
