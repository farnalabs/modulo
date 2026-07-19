Feature: Lifecycle Map CRUD
  As a user
  I want to create, read, update, and delete lifecycle maps
  So that I can model my organisation's SDLC workflow

  Background:
    Given I am authenticated as an admin in org "acme"

  Scenario: Create a lifecycle map
    When I create a lifecycle map named "SDLC Workflow" with visibility "org"
    Then the response status is 201
    And the response contains a lifecycle map named "SDLC Workflow"
    And the lifecycle map has version 1

  Scenario: List lifecycle maps
    Given a lifecycle map named "SDLC Workflow" exists
    When I list lifecycle maps
    Then the response status is 200
    And the response contains 1 lifecycle map

  Scenario: Get a lifecycle map by id
    Given a lifecycle map named "SDLC Workflow" exists
    When I get the lifecycle map by id
    Then the response status is 200
    And the response contains a lifecycle map named "SDLC Workflow"

  Scenario: Update a lifecycle map name
    Given a lifecycle map named "SDLC Workflow" exists
    When I update the lifecycle map name to "Updated SDLC Workflow"
    Then the response status is 200
    And the response contains a lifecycle map named "Updated SDLC Workflow"

  Scenario: Update a lifecycle map content bumps version
    Given a lifecycle map named "SDLC Workflow" exists
    When I update the lifecycle map content to include 1 stage
    Then the response status is 200
    And the lifecycle map has version 2

  Scenario: Delete a lifecycle map
    Given a lifecycle map named "SDLC Workflow" exists
    When I delete the lifecycle map
    Then the response status is 204
    When I get the deleted lifecycle map by id
    Then the response status is 404

  Scenario: Get lifecycle map returns 404 for unknown id
    When I get lifecycle map by id "00000000-0000-0000-0000-000000000000"
    Then the response status is 404

  Scenario: Team visibility requires owner_team_id
    When I create a lifecycle map named "Team Map" with visibility "team"
    Then the response status is 422
