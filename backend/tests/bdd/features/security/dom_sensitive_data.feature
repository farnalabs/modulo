Feature: DOM Sensitive Data
  Sensitive credential values must be masked as ●●●●●● in API responses
  to prevent secret exposure in the DOM or browser storage. A server-
  authenticated reveal endpoint grants temporary 30-second unmasking.

  Background:
    Given I am authenticated as an admin in org "default"

  Scenario: Credentials masked in API response
    Given a connector with config_json containing "api_key" set to "sk-123456"
    When I retrieve the connector
    Then the "api_key" field in config_json is masked
    And the "name" field retains its original value

  Scenario: Sensitive key detection
    Given a key named "api_key"
    When I check if it is a sensitive key
    Then the result should be true

  Scenario: Non-sensitive key detection
    Given a key named "description"
    When I check if it is a sensitive key
    Then the result should be false

  Scenario: Admin reveals SSO client secret
    Given an SSO provider with client_secret "sso-secret-value"
    When I request to reveal the SSO client secret
    Then the response status is 200
    And I receive the plaintext value "sso-secret-value"
    And the response includes a reveal token

  Scenario: Reveal response includes 30-second expiry
    Given an SSO provider with client_secret "test-secret"
    When I request to reveal the SSO client secret
    Then the response declares "expires_in_seconds" as 30

  Scenario: Non-admin cannot reveal sensitive values
    Given I am authenticated as a viewer in org "default"
    Given an SSO provider with client_secret "test-secret"
    When I request to reveal the SSO client secret
    Then the response status is 403

  Scenario: Unknown resource type returns 400
    When I request to reveal for resource type "unknown"
    Then the response status is 400
