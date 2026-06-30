Feature: Team Pipeline Visibility
  As a team member
  I want to see only the pipelines I have access to
  So that team-private resources remain private

  Background:
    Given I am authenticated as an admin in org "acme"

  Scenario: Team member sees team pipelines
    Given a team "docs-team" exists
    And I am a member of team "docs-team"
    When I view pipelines
    Then I see pipelines owned by my teams

  Scenario: Non-member does not see team-private pipelines
    Given a team "docs-team" exists
    And I am not a member of team "docs-team"
    When I view pipelines
    Then I do not see team-private pipelines from "docs-team"
