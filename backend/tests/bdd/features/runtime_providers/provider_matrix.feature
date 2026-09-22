Feature: Runtime provider platform matrix (ADR 003 / ADR 029)
  As a platform engineer
  I want runtime providers to register, resolve and factory-load deterministically
  So that an environment profile always dispatches to exactly the configured
  backend backend with no guessing and actionable remediation when it cannot

  Background:
    Given the runtime provider environment is clean

  Scenario: Default host registers only the local provider
    Given no runtime provider environment signals are set
    When I build the runtime provider hub from the environment
    Then the hub has a "local" provider
    And the hub has no "e2b" provider
    And the hub has no "runner_docker" provider
    And an environment profile requesting provider_type "local" resolves to provider "local"
    And an environment profile requesting provider_type "e2b" fails with ProviderNotConfiguredError mentioning "MODULO_E2B_API_KEY"
    And an environment profile requesting provider_type "runner_docker" fails with ProviderNotConfiguredError mentioning "MODULO_DOCKER_HOST"

  Scenario: MODULO_E2B_API_KEY registers the e2b provider
    Given MODULO_E2B_API_KEY is set to "test-key"
    When I build the runtime provider hub from the environment
    Then the hub has an "e2b" provider
    And an environment profile requesting provider_type "e2b" resolves to provider "e2b"

  Scenario: MODULO_DOCKER_HOST registers the runner_docker provider
    Given MODULO_DOCKER_HOST is set to "tcp://localhost:2375"
    When I build the runtime provider hub from the environment
    Then the hub has a "runner_docker" provider
    And every docker-family alias resolves to the same "runner_docker" provider

  Scenario: DOCKER_HOST registers the runner_docker provider as well
    Given DOCKER_HOST is set to "tcp://localhost:2375"
    When I build the runtime provider hub from the environment
    Then the hub has a "runner_docker" provider

  Scenario: Unrelated MODULO_RUNNER_* variables do not register Docker
    Given MODULO_RUNNER_TEMPLATE_ID is set to "opencode"
    When I build the runtime provider hub from the environment
    Then the hub has no "runner_docker" provider

  Scenario: provider_hint wins over an explicit provider_type
    Given a hub with "local" and "e2b" providers registered
    And an environment profile with provider_hint "local" and provider_type "e2b"
    When I resolve the profile against the hub
    Then the resolved provider is "local"

  Scenario: A stale provider_hint falls through to the explicit provider_type
    Given a hub with "local" and "e2b" providers registered
    And an environment profile with provider_hint "missing" and provider_type "e2b"
    When I resolve the profile against the hub
    Then the resolved provider is "e2b"

  Scenario: Known-but-unregistered provider types name the remediation env var
    Given a hub with only the "local" provider registered
    And an environment profile with provider_type "e2b" and no provider_hint
    When I resolve the profile against the hub
    Then resolve fails with ProviderNotConfiguredError for provider_type "e2b"
    And the error's env_var is "MODULO_E2B_API_KEY"

  Scenario: Unknown provider types name the valid vocabulary instead of guessing
    Given a hub with only the "local" provider registered
    And an environment profile with provider_type "kubernetes" and no provider_hint
    When I resolve the profile against the hub
    Then resolve fails with UnknownProviderTypeError for provider_type "kubernetes"
    And the error names the valid provider types

  Scenario: A profile without any provider type is unresolvable
    Given a hub with only the "local" provider registered
    And an environment profile with no provider_type and no provider_hint
    When I resolve the profile against the hub
    Then resolve fails with ProviderNotConfiguredError

  Scenario: Factory initialise loads a docker-family provider under a config name
    Given an empty runtime provider hub
    When I initialise the hub from config {"container-runtime": {"type": "local_docker"}}
    Then the hub has a "container-runtime" provider
    And resolving provider_type "docker" against the hub yields provider "runner_docker"

  Scenario: Factory initialise skips e2b providers without an api_key
    Given an empty runtime provider hub
    When I initialise the hub from config {"sandbox": {"type": "e2b"}}
    Then the hub has no "sandbox" provider

  Scenario: Factory initialise rejects an unknown provider type
    Given an empty runtime provider hub
    When I initialise the hub from config {"mystery": {"type": "not-a-provider"}}
    Then initialise fails with UnknownProviderTypeError naming "not-a-provider"