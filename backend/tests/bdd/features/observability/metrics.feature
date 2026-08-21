Feature: Observability Metrics
  As a platform operator
  I want to expose runtime metrics via the observability settings
  So that I can monitor pipeline health and resource usage

  Scenario: Get observability settings returns defaults
    Given the observability module is active
    When I request GET /api/v1/settings/observability
    Then the response status is 200
    And the response contains OTLP endpoint and export interval

  Scenario: Update OTLP endpoint saves configuration
    Given I am authenticated as an admin
    When I PUT /api/v1/settings/observability with a valid OTLP endpoint
    Then the response status is 200
    And the OTLP endpoint is updated

  Scenario: Test connection with valid endpoint returns success
    Given I configure a valid OTLP endpoint
    When I POST /api/v1/settings/observability/test
    Then the test result indicates success or connection error

  Scenario: Preview export shows sample span
    Given observability settings are configured
    When I request GET /api/v1/settings/observability/preview
    Then the response contains a sample span and config
