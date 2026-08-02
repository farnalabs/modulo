Feature: Admin Sandbox Concurrency Limit
  As an admin user
  I want to read and configure the organisation's sandbox concurrency limit
  So that concurrent sandbox agent runs can be capped per organisation

  Scenario: Admin reads the default sandbox concurrency limit
    Given I am authenticated as an admin
    When I request GET /api/v1/admin/org/sandbox-concurrency
    Then the response status is 200
    And the sandbox concurrency limit is null

  Scenario: Admin sets the sandbox concurrency limit
    Given I am authenticated as an admin
    When I PUT /api/v1/admin/org/sandbox-concurrency with limit 3
    Then the response status is 200
    And the sandbox concurrency limit is 3

  Scenario: Admin clears the sandbox concurrency limit
    Given the organisation sandbox concurrency limit is 3
    And I am authenticated as an admin
    When I PUT /api/v1/admin/org/sandbox-concurrency with limit null
    Then the response status is 200
    And the sandbox concurrency limit is null

  Scenario: Admin sets an out-of-range limit
    Given I am authenticated as an admin
    When I PUT /api/v1/admin/org/sandbox-concurrency with limit 0
    Then the response status is 422

  Scenario: Non-admin cannot read the sandbox concurrency limit
    Given I am authenticated as a viewer in org "default"
    When I request GET /api/v1/admin/org/sandbox-concurrency
    Then the response status is 403

  Scenario: Updating the limit preserves other organisation settings
    Given the organisation settings include a license key
    And I am authenticated as an admin
    When I PUT /api/v1/admin/org/sandbox-concurrency with limit 5
    Then the response status is 200
    And the organisation still has its license key
