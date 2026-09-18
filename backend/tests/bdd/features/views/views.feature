Feature: Saved Views
  As a user
  I want to save run list views with filters
  So that I can quickly access my common monitoring configurations

  Background:
    Given I am authenticated as an admin user

  Scenario: Create a saved view
    When I POST /api/v1/views with name "My View" and type "run_list"
    Then the response status is 201
    And the response contains a view with name "My View"

  Scenario: List saved views
    Given a saved view exists with name "My View"
    When I GET /api/v1/views
    Then the response status is 200
    And the response contains a list of views

  Scenario: Get a saved view by ID
    Given a saved view exists with name "My View"
    When I GET /api/v1/views/{view_id}
    Then the response status is 200
    And the view name is "My View"

  Scenario: Update a saved view
    Given a saved view exists with name "My View"
    When I PATCH /api/v1/views/{view_id} with name "Updated View"
    Then the response status is 200
    And the view name is "Updated View"

  Scenario: Delete a saved view
    Given a saved view exists
    When I DELETE /api/v1/views/{view_id}
    Then the response status is 204

  Scenario: Non-existent view returns 404
    When I GET /api/v1/views/00000000-0000-0000-0000-000000000000
    Then the response status is 404
