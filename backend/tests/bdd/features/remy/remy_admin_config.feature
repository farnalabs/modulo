Feature: Admin Remy Configuration
  As an org admin
  I want to configure the Remy AI assistant settings
  So that I can control the system prompt, access, and defaults

  Scenario: Admin can get the default Remy config when none is set
    Given I am authenticated as an admin in org "acme"
    When I GET the admin Remy config
    Then the response status is 200
    And the config has default provider "anthropic"
    And the config has default model "claude-sonnet-4-20250514"
    And the config has default context window of 200000

  Scenario: Admin can set the system prompt
    Given I am authenticated as an admin in org "acme"
    When I update the Remy config with system_prompt "You are a helpful assistant"
    Then the response status is 200
    And the config has system_prompt "You are a helpful assistant"

  Scenario: Admin can set the access list
    Given I am authenticated as an admin in org "acme"
    When I update the Remy config access_list with user_ids ["user-1", "user-2"]
    Then the response status is 200
    And the config access_list includes user_ids ["user-1", "user-2"]

  Scenario: Admin can set the default provider and model
    Given I am authenticated as an admin in org "acme"
    When I update the Remy config default_provider to "openai" and default_model to "gpt-4o"
    Then the response status is 200
    And the config has default_provider "openai"
    And the config has default_model "gpt-4o"

  Scenario: Admin can allow specific providers
    Given I am authenticated as an admin in org "acme"
    When I update the Remy config allowed_providers to ["anthropic", "openai"]
    Then the response status is 200
    And the config allowed_providers is ["anthropic", "openai"]

  Scenario: Non-admin gets 403 on get config
    Given I am authenticated as a viewer in org "acme"
    When I GET the admin Remy config
    Then the response status is 403

  Scenario: Non-admin gets 403 on update config
    Given I am authenticated as a viewer in org "acme"
    When I update the Remy config with system_prompt "hacked"
    Then the response status is 403

  Scenario: Update config persists and returns merged fields
    Given I am authenticated as an admin in org "acme"
    When I update the Remy config with additional_guidance "Be concise"
    And I GET the admin Remy config
    Then the config has additional_guidance "Be concise"

  Scenario: Admin can list available providers
    Given I am authenticated as an admin in org "acme"
    When I GET available providers
    Then the response status is 200
    And the available providers include native provider "anthropic"
    And the available providers include custom type "ollama"

  Scenario: Non-admin gets 403 on available providers
    Given I am authenticated as a viewer in org "acme"
    When I GET available providers
    Then the response status is 403

  Scenario: Admin gets 422 for unsupported allowed_provider
    Given I am authenticated as an admin in org "acme"
    When I update the Remy config allowed_providers to ["nonexistent"]
    Then the response status is 422
