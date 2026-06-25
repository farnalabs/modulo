Feature: Connector Health Check
  As a pipeline operator
  I want to check whether a connector is healthy before running a pipeline
  So that I can diagnose connectivity issues early

  Background:
    Given I am authenticated in org "acme"

  Scenario: Healthy connector returns ok
    Given a GitHub connector configured with valid credentials
    When I GET /api/connectors/{connector_id}/health
    Then the response status is 200
    And the response ok is true
    And the response detail is "healthy"

  Scenario: Unhealthy connector returns error detail
    Given a GitHub connector configured with invalid credentials
    When I GET /api/connectors/{connector_id}/health
    Then the response status is 200
    And the response ok is false
    And the response detail describes the error

  Scenario: Nonexistent connector returns 404
    Given no connector exists with id "non-existent"
    When I GET /api/connectors/non-existent/health
    Then the response status is 404

  Scenario: Health check respects organisation scoping
    Given org "acme" has a connector "my-connector"
    When I authenticate as a user in "othercorp"
    And I GET /api/connectors/my-connector/health
    Then the response status is 404
