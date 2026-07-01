Feature: Error Dashboard
  As an admin
  I want to view and manage error groups
  So that I can track platform health

  Background:
    Given an organisation with 10 error groups

  Scenario: List error groups
    When I GET /api/v1/errors
    Then the response contains a paginated list of groups

  Scenario: Filter by status
    When I GET /api/v1/errors?status=new
    Then only groups with status "new" are returned

  Scenario: View error group detail
    When I GET /api/v1/errors/{group_id}
    Then the response contains the full group detail

  Scenario: Resolve an error group
    When I PATCH /api/v1/errors/{group_id} with status "resolved"
    Then the group status is updated to "resolved"

  Scenario: Non-existent group returns 404
    When I request a non-existent error group
    Then the response status is 404
