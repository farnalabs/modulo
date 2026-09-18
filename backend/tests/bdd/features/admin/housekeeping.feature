Feature: Admin Housekeeping
  As an org admin
  I want to scan for and delete unused or orphaned resources
  So that I can keep my organisation clean

  Background:
    Given I am authenticated as an admin

  Scenario: Admin scans for cleanup candidates
    When I request GET /api/v1/admin/housekeeping
    Then the response status is 200
    And the housekeeping response contains categories and total_count
    And every candidate includes an entity_type

  Scenario: Scan is org-scoped and RLS is applied
    When I request GET /api/v1/admin/housekeeping
    Then the response status is 200
    And the housekeeping scan applies the organisation RLS context

  Scenario: Non-admin access returns 403
    Given I am authenticated as a non-admin user
    When I request GET /api/v1/admin/housekeeping
    Then the response status is 403

  Scenario: Unauthenticated access returns 401
    Given I am not authenticated
    When I request GET /api/v1/admin/housekeeping
    Then the response status is 401

  Scenario: Admin deletes selected candidates
    When I cleanup housekeeping items with entity types secret,connector
    Then the response status is 200
    And the cleanup response reports the deleted count
    And the cleanup deletes items scoped to the organisation

  Scenario: Cleanup rejects unknown entity types without aborting
    When I cleanup housekeeping items with entity types does_not_exist
    Then the response status is 200
    And the cleanup response reports an unknown entity type error
