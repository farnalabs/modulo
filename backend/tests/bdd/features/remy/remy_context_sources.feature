Feature: Remy Context Sources
  As a Remy user
  I want Remy to only load the knowledge I need
  So that Remy stays token-efficient and relevant

  Background:
    Given the organisation exists
    And a configured organisation with Remy enabled

  Scenario: Always-on sources are injected into the system prompt
    Given the organisation has a product primer configured
    And the "product_primer" context source is set to "always_on"
    When Remy builds a system prompt for a new session
    Then the prompt contains a "## Product Overview" section

  Scenario: Tool-mode sources are listed as available tools
    Given the "product_docs" context source is set to "tool"
    When Remy builds a system prompt for a new session
    Then the prompt contains "get_documentation" in the "## Available Knowledge Tools" section

  Scenario: Disabled sources are excluded entirely
    Given the "integration_status" context source is set to "off"
    When Remy builds a system prompt for a new session
    Then the prompt does NOT mention "get_integration_status"

  Scenario: User override takes precedence over org default
    Given the org default sets "product_docs" to "tool"
    And the user has overridden "product_docs" to "always_on"
    When Remy builds a system prompt
    Then the prompt contains documentation content inline

  Scenario: Skill source_mode filtering
    Given an org skill "Pipeline Reference" with source_mode "off"
    When Remy builds a system prompt
    Then the prompt does not mention "Pipeline Reference"

  Scenario: get_documentation tool returns results
    Given a documentation index has been loaded
    When the user calls get_documentation with query "pipeline"
    Then results include sections matching the query

  Scenario: get_integration_status returns Markdown table
    Given the organisation has connectors and model backends configured
    When the user calls get_integration_status
    Then the result contains a Markdown table with connector names

  Scenario: get_org_config returns filtered config
    Given the organisation has Remy config set
    When the user calls get_org_config with section "remy"
    Then the result contains Remy configuration keys

  Scenario: get_available_features returns plan info
    Given the organisation is on the Community tier
    When the user calls get_available_features
    Then the result shows which features are available
