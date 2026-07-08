Feature: MCP Library Browse
  As an MCP client
  I want to browse the library via MCP
  So that AI assistants can discover reusable primitives

  Background:
    Given an MCP server is running at /mcp
    And I have a valid MCP API key with scope "library:browse"

  Scenario: MCP lists library primitives
    Given the organisation has 3 local primitives
    When the MCP client sends a tools/call request for "library_browse"
    Then the response contains the list of primitives
    And each primitive has id, name, and primitive_type

  Scenario: MCP searches library primitives
    Given the organisation has a primitive named "PRD Input Schema"
    When the MCP client sends a tools/call request for "library_browse" with search "PRD"
    Then the response contains "PRD Input Schema"

  Scenario: MCP library_browse is read-only
    Given the organisation has 3 local primitives
    When the MCP client sends a tools/call request for "library_browse" with intent to modify
    Then the response is read-only
    And no primitives are created or modified

  Scenario: MCP without library:browse scope is blocked
    Given the MCP API key has scope "trigger:run" only
    When the MCP client sends a tools/call request for "library_browse"
    Then the response contains isError true
    And the error mentions "insufficient scope"
