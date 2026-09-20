Feature: Model Backend Health Check
  As a pipeline operator
  I want to re-check whether a model backend is healthy on demand
  So that I can detect API key or connectivity issues early without rotating credentials

  Background:
    Given I am authenticated in org "acme"

  # PRD 8.1 health check: POST /api/v1/model-backends/{id}/health-check decrypts the
  # stored credential, re-pings the provider, and persists the result (clearing a
  # sticky last_health_check_error). These scenarios drive the real route with only
  # the DB lookup seam and the core health-check seam patched.

  Scenario: Healthy model backend returns ok
    Given an OpenAI model backend configured with valid credentials
    When I check the model backend health
    Then the health check returns ok
    And the persisted health check result is healthy

  Scenario: Unhealthy model backend returns error
    Given an OpenAI model backend configured with invalid API key
    When I check the model backend health
    Then the health check returns error
    And the error describes the authentication failure

  Scenario: Health check respects org scoping
    Given org "acme" has a model backend "my-backend"
    And I authenticate as a user in "othercorp"
    When I check the health of "my-backend"
    Then the health check is not accessible for the other org

  Scenario: Stub backend always returns healthy
    Given a Stub model backend is configured
    When I check the model backend health
    Then the health check returns ok