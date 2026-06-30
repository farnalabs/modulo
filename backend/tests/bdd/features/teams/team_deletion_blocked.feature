Feature: Team Deletion Blocked
  As an admin
  I want to be blocked from deleting a team that still owns resources
  So that I do not orphan pipelines, connectors, stages, or model backends

  Background:
    Given I am authenticated as an admin in org "acme"

  Scenario: Delete blocked when pipeline is owned by team
    Given a team "docs-team" exists
    And a pipeline "review-pipeline" is owned by team "docs-team"
    When I delete the team "docs-team"
    Then the response status is 409
    And the error indicates the team still has resources

  Scenario: Delete blocked when connector is owned by team
    Given a team "docs-team" exists
    And connector "slack-connector" is owned by team "docs-team"
    When I delete the team "docs-team"
    Then the response status is 409
    And the error indicates the team still has resources

  Scenario: Delete succeeds when team has no resources
    Given a team "docs-team" exists
    And the team has no resources
    When I delete the team "docs-team"
    Then the response status is 204
