Feature: Plugin Registry
  As an administrator
  I want to discover and monitor installed Modulo plugins
  So that I can manage my instance's extension capabilities

  @awaiting-implementation
  Scenario: Discover installed plugins
    Given 2 plugins are registered
    When I GET /api/plugins
    Then the response status is 200
    And the response contains 2 plugins
    And each plugin has PLUGIN_ID, display_name, version, and capabilities

  @awaiting-implementation
  Scenario: Get plugin detail
    Given a plugin "modulo-connector-slack" is registered
    When I GET /api/plugins/modulo-connector-slack
    Then the response status is 200
    And the response has PLUGIN_ID "modulo-connector-slack"
    And the response includes display_name, description, version, and capabilities

  Scenario: Plugin health check
    Given a plugin "modulo-connector-slack" is registered
    When I GET /api/plugins/modulo-connector-slack/health
    Then the response status is 200
    And the response contains health_ok and detail

  Scenario: Plugin not found
    When I GET /api/plugins/unknown-plugin/health
    Then the response status is 404
    And the response detail says "Plugin not found"

  @awaiting-implementation
  Scenario: Plugin discovery on startup
    Given no plugins are initially registered
    When the plugin registry discovers plugins
    Then entry points in "modulo.connectors" and "modulo.model_backends" are scanned
    And discovered plugins are available via list_plugins

  @awaiting-implementation
  Scenario: Plugin manifest validation
    Given an entry point references a package with missing metadata
    When the registry processes the entry point
    Then the plugin is marked with health_ok false
    And the detail describes the failure

  Scenario: Plugin capabilities advertised
    Given a plugin "modulo-connector-slack" is registered with connector type "slack"
    When I GET /api/plugins
    Then the response includes a plugin with capabilities containing "connector_type"
