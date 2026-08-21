Feature: Model Backend Rotation
  As a pipeline operator
  I want to define multiple backends and have the system rotate between them
  So that we have fallback if a provider is unavailable

  Background:
    Given I am authenticated in org "acme"

  Scenario: Primary backend is used when healthy
    Given org "acme" has model backends "gpt-4-primary" and "gpt-4-fallback"
    And "gpt-4-primary" is healthy
    When I trigger a run with model backend assignment
    Then the run uses "gpt-4-primary"

  Scenario: Fallback backend is used when primary is unhealthy
    Given org "acme" has model backends "gpt-4-primary" and "gpt-4-fallback"
    And "gpt-4-primary" is unhealthy
    When I trigger a run with model backend assignment
    Then the run uses "gpt-4-fallback"

  Scenario: All unhealthy backends return error
    Given org "acme" has model backends "gpt-4-primary" and "gpt-4-fallback"
    And "gpt-4-primary" is unhealthy
    And "gpt-4-fallback" is unhealthy
    When I trigger a run with model backend assignment
    Then the run fails with "no_healthy_backend"

  Scenario: Health check is performed before each run
    Given org "acme" has model backend "gpt-4"
    When the backend health is checked
    Then the health check result determines whether the backend is selected
