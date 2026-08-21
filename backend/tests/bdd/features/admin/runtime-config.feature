Feature: Admin Runtime Configuration
  As an admin user
  I want to inspect and override runtime configuration
  So that I can manage settings without restarting the server

  Scenario: Admin can get runtime config
    Given I am authenticated as an admin
    When I request GET /api/v1/admin/runtime-config
    Then the response status is 200
    And the response contains items array and has_drift flag

  Scenario: Admin can override a config key
    Given I am authenticated as an admin
    When I PUT /api/v1/admin/runtime-config with override for "MODULO_LOG_LEVEL"
    Then the response status is 200
    And the response includes the updated config items

  Scenario: Admin can clear an override
    Given I am authenticated as an admin
    When I PUT /api/v1/admin/runtime-config with clear for "MODULO_LOG_LEVEL"
    Then the response status is 200
    And the response includes the updated config items

  Scenario: Admin can reload config from environment
    Given I am authenticated as an admin
    When I POST /api/v1/admin/runtime-config/reload
    Then the response status is 200
    And the response contains items array and has_drift flag

  Scenario: Unknown key in override returns 400
    Given I am authenticated as an admin
    When I PUT /api/v1/admin/runtime-config with unknown key "NONEXISTENT_KEY"
    Then the response status is 400

  Scenario: Non-admin user gets 403
    Given I am authenticated as a viewer in org "default"
    When I request GET /api/v1/admin/runtime-config
    Then the response status is 403

  Scenario: Unauthenticated access returns 401
    Given I am not authenticated
    When I request GET /api/v1/admin/runtime-config
    Then the response status is 401
