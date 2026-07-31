Feature: Model Backend Hub
  As the runtime registry for model backends
  I want the ModelBackendHub to resolve, health-check, and fail over backends for a run
  So that pipelines only ever invoke healthy, registered backends

  Background:
    Given an empty ModelBackendHub

  Scenario: Healthy primary backend is served
    Given a backend "primary" is registered and healthy
    When I resolve backend "primary"
    Then the resolved backend is "primary"

  Scenario: Unhealthy primary fails over to configured fallback
    Given a backend "primary" is registered
    And a backend "fallback" is registered
    And backend "primary" is configured with fallback "fallback"
    And backend "primary" is unhealthy
    When I resolve backend "primary"
    Then the resolved backend is "fallback"

  Scenario: No healthy backend raises unavailable error
    Given a backend "primary" is registered
    And a backend "fallback" is registered
    And backend "primary" is configured with fallback "fallback"
    And backend "primary" is unhealthy
    And backend "fallback" is unhealthy
    When I resolve backend "primary"
    Then an unavailable error is raised

  Scenario: Failover emits a model_failover audit event
    Given a backend "primary" is registered
    And a backend "fallback" is registered
    And backend "primary" is configured with fallback "fallback"
    And backend "primary" is unhealthy
    And failover audit logging is enabled
    When I resolve backend "primary"
    Then a model_failover audit event records primary "primary" and fallback "fallback"

  Scenario: No configured fallback scans all registered backends
    Given a backend "primary" is registered
    And a backend "backup" is registered
    And backend "primary" is unhealthy
    And failover audit logging is enabled
    When I resolve backend "primary" with rotation
    Then the resolved backend is "backup"
    And a model_failover audit event records primary "primary" and fallback "backup"

  Scenario: Unregistered backend raises not-found error
    When I resolve backend "ghost"
    Then a not-found error is raised

  Scenario: Credentials are decrypted once per backend per run
    Given backend "primary" has encrypted credentials stored in the secret backend
    When the hub initialises with backend "primary"
    Then the secret backend was read exactly once for "primary"
    And backend "primary" is registered in the hub
