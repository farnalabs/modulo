Feature: Team Ownership Picker
  As a user creating a resource
  I want to choose its owner team and visibility
  So that I can scope resources to my team

  Background:
    Given I am authenticated as an admin in org "acme"

  Scenario: Set owner_team_id when creating a pipeline
    Given a team "docs-team" exists
    When I create a pipeline with owner_team_id of "docs-team"
    Then the pipeline is owned by team "docs-team"

  Scenario: Set visibility to team when creating a connector
    Given a team "docs-team" exists
    When I create a connector with owner_team_id of "docs-team" and visibility "team"
    Then the connector is team-private
