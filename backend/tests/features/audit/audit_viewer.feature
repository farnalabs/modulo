Feature: Audit Viewer
  As a security administrator
  I want to browse, filter, verify, and export the immutable audit trail
  So that I can review who did what, when, and ensure chain integrity

  Background:
    Given I am authenticated as an admin in org "acme"

  Scenario: List audit events with default pagination
    Given 3 audit events exist
    When I GET /api/v1/admin/audit
    Then the response status is 200
    And the response contains 3 audit events
    And the response has a next_cursor field
    And the response has a total field of 3

  Scenario: Filter audit events by event type
    Given audit events exist with types "pipeline.run", "connector.sync", "pipeline.run"
    When I GET /api/v1/admin/audit?event_type=pipeline.run
    Then the response status is 200
    And the response contains only events with event_type "pipeline.run"

  Scenario: Filter audit events by date range
    Given audit events exist from "2025-01-01T00:00:00Z" to "2025-06-01T00:00:00Z"
    When I GET /api/v1/admin/audit?from_date=2025-01-01T00:00:00Z&to_date=2025-06-01T00:00:00Z
    Then the response status is 200
    And all returned events are within the date range

  Scenario: Filter by actor user
    Given audit events exist for user "00000000-0000-0000-0000-000000000002"
    When I GET /api/v1/admin/audit?user_id=00000000-0000-0000-0000-000000000002
    Then the response status is 200
    And all returned events have actor_user_id "00000000-0000-0000-0000-000000000002"

  Scenario: Batch detail returns full event details
    Given 3 audit events exist with IDs "e1", "e2", "e3"
    When I POST /api/v1/admin/audit/batch-detail with event_ids "e1", "e2"
    Then the response status is 200
    And the response contains full details for 2 events
    And the response includes payload_json for each event

  Scenario: Verify chain integrity
    Given the audit chain contains 5 events with valid hashes
    When I GET /api/v1/admin/audit/verify
    Then the response status is 200
    And the chain verification result is "valid"

  Scenario: Export audit chain with pagination
    Given 150 audit events exist
    When I GET /api/v1/admin/audit/export?page=1&page_size=50
    Then the response status is 200
    And the export contains 50 events
    And the response has a total field
    And the response has a page field of 1
    And the response has a page_size field of 50

  Scenario: Team gate blocks non-admin
    Given the audit_viewer feature is disabled
    When I GET /api/v1/admin/audit
    Then the response status is 402
    And the response detail mentions "audit_viewer"

  Scenario: Empty audit log
    Given no audit events exist
    When I GET /api/v1/admin/audit
    Then the response status is 200
    And the response contains 0 audit events
    And the response has a total field of 0

  Scenario: Cross-org isolation
    Given org "acme" has 3 audit events
    When I authenticate as a user in org "other-corp"
    And I GET /api/v1/admin/audit
    Then the response status is 200
    And the response contains 0 audit events
