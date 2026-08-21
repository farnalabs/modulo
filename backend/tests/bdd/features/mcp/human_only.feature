Feature: MCP Human-Only Interaction
  As an MCP client
  I want to be blocked from human-only operations
  So that critical decisions remain with human operators

  Background:
    Given an MCP server is running at /mcp
    And I have a valid MCP API key

  # @awaiting-implementation: the legacy /mcp/tools/call HTTP surface no longer exists.
  # The MCP server now speaks JSON-RPC over StreamableHTTP (POST /mcp).
  @awaiting-implementation
  Scenario: MCP cannot bypass human-only gate
    Given pipeline "human-pipeline" has a human-only node "final-signoff"
    And a run is waiting at human node "final-signoff"
    When the MCP client sends a tools/call request for "review_hitl" with action "approve"
    Then the response contains isError true
    And the error mentions "human-only"

  # @awaiting-implementation: the legacy /mcp/tools/call HTTP surface no longer exists.
  # The MCP server now speaks JSON-RPC over StreamableHTTP (POST /mcp).
  @awaiting-implementation
  Scenario: MCP can list but not act on human-only gates
    Given a run is waiting at human node "final-signoff"
    When the MCP client sends a tools/call request for "review_hitl" with action "list"
    Then the response includes the human-only gate
    And the response indicates "requires_human" true

  # @awaiting-implementation: the legacy /mcp/tools/call HTTP surface no longer exists.
  # The MCP server now speaks JSON-RPC over StreamableHTTP (POST /mcp).
  @awaiting-implementation
  Scenario: Audit logs distinguish MCP vs human actions
    Given a run is waiting at human node "final-signoff"
    When a human operator approves via the browser UI
    Then the audit event shows actor type "human"
    When an MCP client approves a non-human gate
    Then the audit event shows actor type "mcp"
