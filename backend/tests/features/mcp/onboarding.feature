Feature: MCP Onboarding
  As a new Modulo user
  I want to discover available MCP tools and their capabilities
  So that I know what I can do via MCP

  Background:
    Given an MCP server is running at /mcp

  Scenario: MCP lists available tools
    When the MCP client sends a tools/list request
    Then the response contains tool definitions
    And the tools include "trigger", "review_hitl", "library_browse", "human_only"

  Scenario: Each tool has a description and input schema
    When the MCP client sends a tools/list request
    Then the "trigger" tool has description and inputSchema
    And the "review_hitl" tool has description and inputSchema
    And the "library_browse" tool has description and inputSchema

  Scenario: MCP onboarding without auth shows public info
    Given no API key is provided
    When the MCP client sends a tools/list request
    Then the response still contains tool definitions
    But invoking any tool returns 401

  Scenario: MCP returns tool descriptions in natural language
    When the MCP client sends a tools/list request
    Then the "trigger" tool description explains how to trigger a pipeline run
    And the "review_hitl" tool description explains how to review gates
    And the "library_browse" tool description explains how to browse primitives

  Scenario: SSE transport works for tool listing
    Given the MCP server uses SSE transport
    When a client connects to /mcp with Accept: text/event-stream
    Then the connection is established
    And the client receives a tools/list response
