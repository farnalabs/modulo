Feature: Lifecycle Map CRUD
  As a user
  I want to create, read, update, and delete lifecycle maps
  So that I can model my organisation's SDLC workflow

  Background:
    Given I am authenticated as an admin in org "acme"

  Scenario: Create a lifecycle map
    When I POST /api/v1/lifecycle-maps with:
      | name        | SDLC Workflow |
      | description | Maps our development lifecycle |
      | visibility  | org |
    Then the response status is 201
    And the response contains a lifecycle map named "SDLC Workflow"
    And the lifecycle map has version 1

  Scenario: List lifecycle maps
    Given a lifecycle map named "SDLC Workflow" exists
    When I GET /api/v1/lifecycle-maps
    Then the response status is 200
    And the response contains 1 lifecycle map

  Scenario: Get a lifecycle map by id
    Given a lifecycle map named "SDLC Workflow" exists
    When I GET /api/v1/lifecycle-maps/{id}
    Then the response status is 200
    And the response contains a lifecycle map named "SDLC Workflow"

  Scenario: Update a lifecycle map name
    Given a lifecycle map named "SDLC Workflow" exists
    When I PUT /api/v1/lifecycle-maps/{id} with:
      | name | Updated SDLC Workflow |
    Then the response status is 200
    And the response contains a lifecycle map named "Updated SDLC Workflow"

  Scenario: Update a lifecycle map content bumps version
    Given a lifecycle map named "SDLC Workflow" exists
    When I PUT /api/v1/lifecycle-maps/{id} with content_json:
      | stages: [{id: "stage-1", name: "Dev"}] |
    Then the response status is 200
    And the lifecycle map has version 2

  Scenario: Delete a lifecycle map (soft-delete)
    Given a lifecycle map named "SDLC Workflow" exists
    When I DELETE /api/v1/lifecycle-maps/{id}
    Then the response status is 204
    When I GET /api/v1/lifecycle-maps/{id}
    Then the response status is 404

  Scenario: Get lifecycle map returns 404 for unknown id
    When I GET /api/v1/lifecycle-maps/00000000-0000-0000-0000-000000000000
    Then the response status is 404

  Scenario: Viewer cannot create lifecycle maps
    Given I am authenticated as a viewer in org "acme"
    When I POST /api/v1/lifecycle-maps with:
      | name | Unauthorized Map |
    Then the response status is 403

  Scenario: Team visibility requires owner_team_id
    When I POST /api/v1/lifecycle-maps with:
      | name       | Team Map |
      | visibility | team |
    Then the response status is 422
