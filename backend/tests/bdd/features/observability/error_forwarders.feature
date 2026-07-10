Feature: Error Forwarders
  As an admin user
  I want to configure error forwarding destinations
  So that errors are sent to external monitoring services

  Background:
    Given I am authenticated as an admin in org "acme"

  Scenario: List forwarders returns all known types
    When I GET "/api/v1/errors/forwarders"
    Then the response status is 200
    And the response body contains "forwarders"
    And the response includes "sentry", "datadog", "pagerduty", "rollbar", "opsgenie", "loki"

  Scenario: List forwarders shows configured status
    Given an error forwarder "sentry" is configured with DSN "https://key@sentry.io/1"
    When I GET "/api/v1/errors/forwarders"
    Then the sentry forwarder has "configured" set to true

  Scenario: Configure a forwarder
    When I PUT "/api/v1/errors/forwarders/sentry" with body {"config_json": {"dsn": "https://key@sentry.io/1"}, "enabled": true}
    Then the response status is 200
    And the response includes "forwarder_type" set to "sentry"

  Scenario: Configure unknown forwarder type returns 404
    When I PUT "/api/v1/errors/forwarders/unknown" with body {"config_json": {}}
    Then the response status is 404
    And the error mentions "unknown"

  Scenario: Non-admin cannot configure forwarders
    Given I am authenticated as a viewer in org "acme"
    When I PUT "/api/v1/errors/forwarders/sentry" with body {"config_json": {"dsn": "https://key@sentry.io/1"}}
    Then the response status is 403
    And the error mentions "admin"

  Scenario: Test connection succeeds
    Given the sentry forwarder implementation forwards successfully
    When I POST "/api/v1/errors/forwarders/sentry/test" with body {"config_json": {"dsn": "https://key@sentry.io/1"}}
    Then the response status is 200
    And the response body has "ok" set to true

  Scenario: Test connection fails with invalid config
    Given the sentry forwarder implementation fails to forward
    When I POST "/api/v1/errors/forwarders/sentry/test" with body {"config_json": {"dsn": ""}}
    Then the response status is 200
    And the response body has "ok" set to false

  Scenario: Test connection times out
    Given the sentry forwarder implementation hangs for 20 seconds
    When I POST "/api/v1/errors/forwarders/sentry/test" with body {"config_json": {"dsn": "https://key@sentry.io/1"}}
    Then the response status is 200
    And the response body has "ok" set to false
    And the response mentions "timeout"

  Scenario: Test unknown forwarder type
    When I POST "/api/v1/errors/forwarders/unknown/test" with body {"config_json": {}}
    Then the response status is 404
    And the error mentions "unknown"

  Scenario: No organisation returns 400
    Given I am authenticated without an organisation
    When I GET "/api/v1/errors/forwarders"
    Then the response status is 400
    And the error mentions "organisation"

  Scenario: Missing DB table returns 501
    Given the error_forwarder_configs table does not exist
    When I GET "/api/v1/errors/forwarders"
    Then the response status is 501
    And the error mentions "migration"

  Scenario: Feature gating returns 402 when disabled
    Given the "error_forwarders" feature is not enabled on my plan
    When I GET "/api/v1/errors/forwarders"
    Then the response status is 402
    And the error mentions "not available on your plan"
