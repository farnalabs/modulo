Feature: MCP Review HITL
  As an MCP client
  I want to review and approve HITL gates via MCP
  So that AI assistants can handle HITL reviews

  Background:
    Given an MCP server is running at /mcp
    And I have a valid MCP API key with scope "hitl:review"

  Scenario: MCP lists pending gates
    Given a run is waiting at gate "pre-deploy"
    When the MCP client sends a tools/call request for "review_hitl" with action "list"
    Then the response contains the pending gate
    And the response includes run_id and gate_id

  Scenario: MCP approves a gate
    Given a run is waiting at gate "pre-deploy"
    And I have claimed the gate
    When the MCP client sends a tools/call request for "review_hitl" with action "approve"
    Then the run status becomes "running"

  Scenario: MCP rejects a gate
    Given a run is waiting at gate "pre-deploy"
    And I have claimed the gate
    When the MCP client sends a tools/call request for "review_hitl" with action "reject" and reason "Not ready"
    Then the run status becomes "rejected"
    And the run has rejection_reason "Not ready"

  Scenario: MCP cannot approve without claim
    Given a run is waiting at gate "pre-deploy"
    When the MCP client sends a tools/call request for "review_hitl" with action "approve"
    Then the response contains isError true
    And the error mentions "claim"

  Scenario: MCP without hitl:review scope is blocked
    Given the MCP API key has scope "trigger:run" only
    When the MCP client sends a tools/call request for "review_hitl"
    Then the response contains isError true
    And the error mentions "insufficient scope"
