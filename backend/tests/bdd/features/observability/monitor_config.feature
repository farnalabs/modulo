Feature: Monitoring Configuration
  As an admin user
  I want to configure monitoring backends (Sentry, DataDog RUM, Grafana Faro)
  So that frontend errors and performance data are sent to my monitoring service

  Scenario: Admin gets default config when unconfigured
    Given I am authenticated as an admin
    And no monitoring configuration is stored
    When I request GET /api/v1/admin/monitor-config
    Then the response status is 200
    And the monitor config defaults to the "builtin" backend

  Scenario: Admin gets stored monitoring configuration
    Given I am authenticated as an admin
    And a monitoring configuration stores "sentry" and "builtin" backends
    When I request GET /api/v1/admin/monitor-config
    Then the response status is 200
    And the monitor config includes the "sentry" backend

  Scenario: Admin updates monitoring configuration
    Given I am authenticated as an admin
    When I PUT /api/v1/admin/monitor-config with backends ["datadog_rum"]
    Then the response status is 200
    And the monitor config includes the "datadog_rum" backend

  Scenario: PUT with unknown backend returns 422
    Given I am authenticated as an admin
    When I PUT /api/v1/admin/monitor-config with backends ["nope"]
    Then the response status is 422

  Scenario: PUT with empty backend list returns 422
    Given I am authenticated as an admin
    When I PUT /api/v1/admin/monitor-config with backends []
    Then the response status is 422

  Scenario: Non-admin cannot view monitoring configuration
    Given I am authenticated as a viewer in org "default"
    When I request GET /api/v1/admin/monitor-config
    Then the response status is 403
    And the error mentions "admin"

  Scenario: Missing DB table returns 501
    Given I am authenticated as an admin
    And the system_config table does not exist
    When I request GET /api/v1/admin/monitor-config
    Then the response status is 501
    And the error mentions "migration"

  Scenario: DB failure returns 503
    Given I am authenticated as an admin
    And the database is unavailable
    When I request GET /api/v1/admin/monitor-config
    Then the response status is 503
