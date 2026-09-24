Feature: MCP Review HITL
  As an MCP client
  I want to review and approve HITL gates via MCP
  So that AI assistants can handle HITL reviews

  The MCP server speaks JSON-RPC over the StreamableHTTP transport at POST /mcp —
  the legacy /mcp/tools/call HTTP surface no longer exists. Following the
  trigger.feature re-anchor (2026-09-22), these scenarios call the REAL
  `list_pending_hitl` and `review_hitl` tool handler functions directly with the
  request ContextVars hydrated by hand, exercising the REAL `_check_agent_tool_scope`
  scope-gate chokepoint (role-hierarchy denials), the REAL claim-token parse guard,
  and the REAL HITLManager decision dispatch — network-free and DB-free with only
  the auth re-validation, DB and HITLManager seams patched. The FastMCP
  invoke/dispatch layer itself is not exercised here.

  Background:
    Given an MCP server is running at /mcp

  Scenario: MCP lists pending gates
    Given a run is waiting at gate "pre-deploy"
    When the MCP client lists pending HITL gates
    Then the response contains the pending gate
    And the response includes run_id and gate_id

  Scenario: MCP approves a gate
    Given a run is waiting at gate "pre-deploy"
    And I have claimed the gate
    And an MCP API key with role "operator"
    When the MCP client approves the gate
    Then the tool reports HITL decision "approved" for the gate

  Scenario: MCP rejects a gate
    Given a run is waiting at gate "pre-deploy"
    And I have claimed the gate
    And an MCP API key with role "operator"
    When the MCP client rejects the gate with reason "Not ready"
    Then the tool reports HITL decision "rejected" for the gate

  Scenario: MCP cannot approve without claim
    Given a run is waiting at gate "pre-deploy"
    And an MCP API key with role "operator"
    When the MCP client approves a gate without a claim token
    Then the response carries an error
    And the tool returns error "claim_token_required"

  Scenario: MCP without hitl:review scope is blocked
    Given a run is waiting at gate "pre-deploy"
    And I have claimed the gate
    And an MCP API key with role "runner"
    When the MCP client attempts to approve the gate
    Then the response carries an error
    And the tool returns error "insufficient_scope"