Feature: Human-Only Gate
  As a pipeline author
  I want to mark nodes as human-only so that AI cannot make certain decisions
  So that critical steps always require human judgement

  Background:
    Given I am authenticated as an admin in org "acme"

  Scenario: Human-only gate pauses for human input
    Given pipeline "human-pipeline" has a human-only node "manual-approval"
    When the run reaches the "manual-approval" node
    Then the run status becomes "waiting_for_human"
    And no AI agent can process this node

  Scenario: Human submits data to human-only node
    Given a run is waiting at human node "manual-approval"
    When I POST /api/runs/{run_id}/human-input with data {"approved": true}
    Then the response status is 200
    And the run status becomes "running"

  Scenario: Human-only gate prevents AI auto-resolve
    Given pipeline "human-pipeline" has a human-only node "final-signoff"
    When the pipeline engine tries to auto-resolve the gate
    Then the auto-resolve is blocked
    And the run remains "waiting_for_human"

  Scenario: Human-only node has input validation
    Given a run is waiting at human node "manual-approval"
    When I POST /api/runs/{run_id}/human-input with invalid data {}
    Then the response status is 422
    And the error mentions "required field"
