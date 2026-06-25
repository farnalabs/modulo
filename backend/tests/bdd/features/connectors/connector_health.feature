Feature: Connector Health Check
  As a pipeline author
  I want to verify that a connector is reachable and credentials are valid
  So that I can diagnose connectivity issues before running pipelines

  Scenario: Healthy connector returns ok
    Given a GitHub connector configured with valid credentials
    When I GET /api/connectors/{connector_id}/health
    Then the response status is 200
    And the response ok is true

  Scenario: Unreachable connector returns error
    Given a GitHub connector configured with invalid credentials
    When I GET /api/connectors/{connector_id}/health
    Then the response status is 200
    And the response ok is false
    And the response detail describes the error

  Scenario: Credentials are encrypted at rest
    Given a connector with API key "super-secret-key"
    When I inspect the database directly
    Then the API key is not stored in plaintext
