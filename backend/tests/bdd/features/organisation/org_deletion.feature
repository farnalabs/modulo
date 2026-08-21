Feature: Organisation Deletion
  As an org admin
  I want to safely delete my organisation
  So that data is exported, users are notified, and resources are cleaned up

  Background:
    Given I am authenticated as an admin in org "acme"

  Scenario: Admin requests org deletion
    When I POST /api/v1/admin/org/deletion-request
    Then the response status is 202
    And a deletion token is returned
    And the token expires in 24 hours

  Scenario: Export data during grace period
    Given the org has a pending deletion
    When I GET /api/v1/admin/org/export
    Then the response status is 200
    And the export bundle contains organisation info
    And the export bundle contains users, pipelines, and runs

  Scenario: Cancel deletion within window
    Given the org has a pending deletion
    When I PATCH /api/v1/admin/org/deletion-cancel
    Then the response status is 200
    And the org status is restored to "active"

  Scenario: Confirm hard deletion with valid token
    Given the org has a pending deletion with token "abc123"
    When I POST /api/v1/admin/org/deletion-confirm with token "abc123"
    Then the response status is 200
    And the organisation is permanently deleted

  Scenario: Access denied during deletion window
    Given the org has a pending deletion
    When I GET /api/v1/pipelines
    Then the response status is 403

  Scenario: Audit event logged on deletion request
    When I POST /api/v1/admin/org/deletion-request
    Then an audit event "org_deletion_requested" is recorded

  Scenario: Cascading resource cleanup on hard delete
    Given the org has 3 pipelines and 15 runs
    When I POST /api/v1/admin/org/deletion-confirm with a valid token
    Then all associated pipelines are removed
    And all runs are removed
    And all users are removed from the org

  Scenario: Non-admin cannot request deletion
    Given I am authenticated as a viewer in org "acme"
    When I POST /api/v1/admin/org/deletion-request
    Then the response status is 403
