Feature: Model Backend Error Handling
  As a pipeline author
  I want clear, actionable error messages when a model backend fails
  So that I can diagnose and fix issues quickly

  Scenario: Invalid API key on invoke returns auth error
    Given a model backend configured with an invalid API key
    When the backend is invoked with a prompt
    Then an authentication error is returned
    And the error message includes "API key"

  Scenario: Network error on invoke returns service error
    Given a model backend configured with a reachable endpoint
    When the network is unreachable during invoke
    Then a service error is returned
    And the error message includes "network"

  Scenario: Rate-limited response from provider is handled
    Given a model backend configured with valid credentials
    When the provider returns a 429 rate-limit response
    Then a rate-limit error is returned
    And the error includes retry-after information

  Scenario: Timeout during invoke returns timeout error
    Given a model backend configured with a slow provider
    When the invoke exceeds the configured timeout
    Then a timeout error is returned
    And the error message includes "timeout"

  Scenario: Unknown provider returns configuration error
    Given a model backend payload with an unsupported provider "nonexistent"
    When the backend is initialized
    Then a configuration error is returned
    And the error message includes "unsupported provider"

  Scenario: Empty response from provider is handled
    Given a model backend configured with valid credentials
    When the provider returns an empty response
    Then a service error is returned
    And the error message includes "empty response"
