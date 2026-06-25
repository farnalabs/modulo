Feature: MCP Trigger Pipeline Run
  As an MCP client (e.g. Claude Desktop)
  I want to trigger a pipeline run via the MCP protocol
  So that AI assistants can start workflows directly

  Background:
    Given an MCP server is running at /mcp
    And I have a valid MCP API key

  Scenario: MCP client triggers a run
    Given org "acme" has pipeline "my-pipeline"
    When the MCP client sends a tools/call request for "trigger" with pipeline "my-pipeline"
    Then the response status is 200
    And the response contains run_id
    And a run is created with status "pending"

  Scenario: MCP trigger with run_context
    Given org "acme" has pipeline "my-pipeline"
    When the MCP client sends a tools/call request for "trigger" with run_context {"branch": "main"}
    Then the run has run_context with branch "main"

  Scenario: MCP trigger without auth is rejected
    When the MCP client sends a tools/call request for "trigger" without API key
    Then the response status is 401

  Scenario: MCP trigger for non-existent pipeline returns error
    When the MCP client sends a tools/call request for "trigger" with pipeline "ghost"
    Then the response contains isError true
    And the error message mentions "not found"

  Scenario: MCP trigger respects scope limits
    Given the MCP API key has scope "trigger:run" only
    When the MCP client sends a tools/call request for "trigger"
    Then the request is allowed
    When the MCP client sends a tools/call request for "review_hitl"
    Then the response contains isError true
    And the error mentions "forbidden"
