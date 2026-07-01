Feature: External Error Forwarder Configuration
  As an admin
  I want to configure external error tracking integrations
  So that errors are forwarded to my existing monitoring stack

  Background:
    Given an authenticated organisation with a Team license key

  Scenario: List available forwarders
    When I GET /api/v1/errors/forwarders
    Then the response lists 6 forwarder types (sentry, datadog, pagerduty, rollbar, opsgenie, loki)
    And each shows enabled status and last test result

  Scenario: Configure Sentry forwarder
    When I PUT /api/v1/errors/forwarders/sentry with valid config
    Then the configuration is saved
    And the response masks secret values

  Scenario: Test connection fails gracefully
    When I POST /api/v1/errors/forwarders/sentry/test
    Then the response indicates success or failure
    And does not crash the application

  Scenario: Community tier cannot access forwarders
    Given a Community tier organisation
    When I GET /api/v1/errors/forwarders
    Then the response is 402 Payment Required

  Scenario: Enable/disable toggle
    When I PUT /api/v1/errors/forwarders/datadog with enabled=false
    Then the forwarder is disabled

  Scenario: Unknown forwarder type returns 404
    When I PUT /api/v1/errors/forwarders/unknown
    Then the response status is 404
