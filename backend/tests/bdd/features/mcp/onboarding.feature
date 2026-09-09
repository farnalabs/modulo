Feature: MCP Onboarding — discoverable tool surface (ADR 017)
  As a new MCP client
  I want to discover the MCP tool stack and the authentication contract that gates it
  So that I can drive Modulo through the MCP protocol

  Background:
    Given the MCP server is mounted at /mcp

  Scenario: The MCP server advertises a documented tool inventory
    When the MCP tool registry is inspected
    Then the tool inventory contains definitions
    And the "trigger_pipeline" tool has a description and inputSchema
    And the "review_hitl" tool has a description and inputSchema
    And the "search_library" tool has a description and inputSchema
    And the "list_schemas" tool has a description and inputSchema

  Scenario: Every advertised tool carries the onboarding contract
    When the MCP tool registry is inspected
    Then the tool inventory contains at least 20 tools
    And every tool definition carries a name, a description and an inputSchema

  Scenario: Unauthenticated introspection fails closed
    When an unauthenticated request reaches the MCP auth middleware
    Then the MCP request is rejected with status 401

  Scenario: An invalid credential fails closed
    Given a request with an invalid bearer token
    When the request reaches the MCP auth middleware
    Then the MCP request is rejected with status 401
