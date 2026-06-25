Feature: Model Backend Configuration
  As a pipeline author
  I want to configure model backends with provider, model, and API key
  So that my pipelines can use different LLM providers

  Background:
    Given I am authenticated as an admin in org "acme"

  Scenario: Configure an OpenAI backend
    Given I configure an OpenAI model backend with model "gpt-4" and API key "sk-..."
    When I GET /api/model-backends
    Then the response contains a backend with provider "openai" and model "gpt-4"

  Scenario: Configure an Anthropic backend
    Given I configure an Anthropic model backend with model "claude-3-opus" and API key "sk-ant-..."
    When I GET /api/model-backends
    Then the response contains a backend with provider "anthropic" and model "claude-3-opus"

  Scenario: Configure a Stub backend for testing
    Given I configure a Stub model backend with fixture map
    When I GET /api/model-backends
    Then the response contains a backend with provider "stub"

  Scenario: API key is encrypted at rest
    Given I configure an OpenAI model backend with API key "sk-secret"
    When I inspect the database directly
    Then the API key is not stored in plaintext

  Scenario: Update existing backend configuration
    Given org "acme" has a model backend "my-backend" with model "gpt-4"
    When I PATCH /api/model-backends/my-backend with model "gpt-4-turbo"
    Then the response status is 200
    And the model is updated to "gpt-4-turbo"
