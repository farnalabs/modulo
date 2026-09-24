Feature: MCP Human-Only Interaction
  As an MCP client
  I want to be blocked from human-only operations
  So that critical decisions remain with human operators

  Following the `trigger.feature` (2026-09-22) and `review_hitl.feature`
  (2026-09-24) re-anchors, these scenarios call the REAL `review_hitl` and
  `list_pending_hitl` tool handler functions directly with the request
  ContextVars hydrated by hand — exercising the REAL `_check_human_only_gate`
  policy hook (the shared `human_only_denial` verdict with its fail-closed
  semantics), the REAL pending-gate serialisation (per-gate `human_only`
  flag), and the REAL decision-audit `client_type` attribution (browser vs
  mcp) — network-free and DB-free with only the auth re-validation and DB /
  config-resolution / HITLManager seams patched. The FastMCP invoke/dispatch
  layer itself is not exercised here.

  Background:
    Given an MCP server is running at /mcp
    And I have a valid MCP API key

  Scenario: MCP cannot bypass human-only gate
    Given pipeline "human-pipeline" has a human-only node "final-signoff"
    And a run is waiting at human node "final-signoff"
    When the MCP client approves the human-only gate
    Then the response carries an error
    And the tool returns error "human_only_gate"
    And the error mentions "human_only gate requires browser authentication"
    And the denial appends a "hitl.human_only_denied" audit event

  Scenario: MCP can list but not act on human-only gates
    Given a run is waiting at human node "final-signoff"
    When the MCP client lists pending HITL gates
    Then the response contains the pending gate
    And the pending gate indicates "human_only" true

  Scenario: Audit logs distinguish MCP vs human actions
    Given a run is waiting at gate "pre-deploy"
    And I have claimed the gate
    And an MCP API key with role "operator"
    When the MCP client approves the gate
    Then the tool reports HITL decision "approved" for the gate
    And the decision audit event records actor type "mcp"
    And the decision audit event records actor type "browser"
