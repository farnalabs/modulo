Feature: Organisation Deletion
  As an organisation member
  I want self-service organisation deletion to be refused
  So that the organisation I am signed in to can never be destroyed from inside

  Background:
    Given I am authenticated as an admin in org "acme"

  Scenario: Admin cannot request deletion of their own organisation
    When I POST /api/v1/admin/org/deletion-request
    Then the response status is 403
    And the deletion refusal names the system-admin route

  Scenario: Admin cannot confirm deletion of their own organisation
    When I POST /api/v1/admin/org/deletion-confirm with token "abc123"
    Then the response status is 403
    And the deletion refusal names the system-admin route

  Scenario: Export data remains available
    When I GET /api/v1/admin/org/export
    Then the response status is 200
    And the export bundle contains organisation info
    And the export bundle contains users, pipelines, and runs

  Scenario: Cancel a pending deletion remains available as a safety valve
    Given the org has a pending deletion
    When I PATCH /api/v1/admin/org/deletion-cancel
    Then the response status is 200
    And the org status is restored to "active"

  Scenario: Non-admin cannot request deletion
    Given I am authenticated as a viewer in org "acme"
    When I POST /api/v1/admin/org/deletion-request
    Then the response status is 403
