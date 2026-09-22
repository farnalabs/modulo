Feature: MCP triggers a pipeline run
  As an MCP client (e.g. Claude Desktop)
  I want to trigger a pipeline run via the MCP protocol
  So that AI assistants can start workflows directly

  The MCP server speaks JSON-RPC over the StreamableHTTP transport at POST /mcp —
  the legacy /mcp/tools/call HTTP surface no longer exists. These scenarios drive
  the REAL trigger_pipeline / review_hitl tool handlers (through the real FastMCP
  tool + scope-gate layer) and the REAL McpAuthMiddleware gate, network-free and
  DB-free, with only the auth re-validation and DB/dispatch seams mocked.

  Background:
    Given an MCP server is running at /mcp

  Scenario: MCP client triggers a run
    Given org "acme" has pipeline "my-pipeline"
    And an MCP API key with role "runner"
    When the MCP client calls "trigger_pipeline" for the pipeline
    Then the response contains run_id
    And the tool reports run status "pending"

  Scenario: MCP trigger with run_context
    Given org "acme" has pipeline "my-pipeline"
    And an MCP API key with role "runner"
    When the MCP client calls "trigger_pipeline" with input_payload {"branch": "main"}
    Then the run is created with input_payload carrying branch "main"

  Scenario: MCP trigger without auth is rejected
    When an unauthenticated request reaches the MCP server
    Then the MCP auth gate rejects the request with status code 401

  Scenario: MCP trigger for non-existent pipeline returns error
    Given an MCP API key with role "runner"
    When the MCP client calls "trigger_pipeline" for an unknown pipeline
    Then the response carries an error
    And the tool returns error "pipeline_not_found"

  Scenario: MCP trigger respects scope limits
    Given org "acme" has pipeline "my-pipeline"
    And an MCP API key with role "runner"
    When the MCP client calls "trigger_pipeline" for the pipeline
    Then the response contains run_id
    When the MCP client calls "review_hitl" with action "approve"
    Then the response carries an error
    And the tool returns error "insufficient_scope"