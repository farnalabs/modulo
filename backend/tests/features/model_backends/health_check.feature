Feature: Model Backend Health Check
  As a pipeline operator
  I want to check whether a model backend is healthy before running a pipeline
  So that I can detect API key or connectivity issues early

  Background:
    Given I am authenticated in org "acme"

  Scenario: Healthy model backend returns ok
    Given an OpenAI model backend configured with valid credentials
    When I check the model backend health
    Then the health check returns ok

  Scenario: Unhealthy model backend returns error
    Given an OpenAI model backend configured with invalid API key
    When I check the model backend health
    Then the health check returns error
    And the error describes the authentication failure

  Scenario: Health check respects org scoping
    Given org "acme" has a model backend "my-backend"
    When I authenticate as a user in "othercorp"
    And I check the health of "my-backend"
    Then the health check is not accessible

  Scenario: Stub backend always returns healthy
    Given a Stub model backend is configured
    When I check the model backend health
    Then the health check returns ok
