Feature: Admin Audit Export
  As an admin user
  I want to export audit events and verify chain integrity
  So that I can produce SOC 2 compliance evidence

  Scenario: Paginated CSV export loads events
    Given I am authenticated as an admin
    When I request GET /api/v1/admin/audit/export
    Then the response status is 200
    And the response contains items, total, page, and page_size

  Scenario: Export respects event_type filter
    Given I am authenticated as an admin
    When I request GET /api/v1/admin/audit/export?event_type=pipeline.run
    Then the response status is 200
    And the export endpoint receives event_type filter "pipeline.run"

  Scenario: Chain verification reports valid chain
    Given I am authenticated as an admin
    When I request GET /api/v1/admin/audit/verify
    Then the response status is 200
    And the verify response contains event_count field

  Scenario: Unauthenticated access returns 401
    Given I am not authenticated
    When I request GET /api/v1/admin/audit/export
    Then the response status is 401
