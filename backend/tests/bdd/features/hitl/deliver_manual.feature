Feature: HITL Deliver Manual
  As a human reviewer at a HITL gate
  I want to manually supply output directly
  So that I can bypass the agent's output without routing to a correction run

  Scenario: Deliver manual output at a HITL gate
    Given a run is waiting at HITL gate "pre-deploy"
    And I have claimed gate "pre-deploy"
    When I POST /api/runs/{run_id}/hitl/{gate_id}/deliver-manual with claim_token and manual output
    Then the response status is 200
    And the run status becomes "running"
    And the manual output is passed to the pipeline

  Scenario: Deliver manual without claim_token is rejected
    Given a run is waiting at HITL gate "pre-deploy"
    When I POST /api/runs/{run_id}/hitl/{gate_id}/deliver-manual with no claim_token and manual output
    Then the response status is 403

  Scenario: Deliver manual with expired claim_token is rejected
    Given a run is waiting at HITL gate "pre-deploy"
    And I have claimed gate "pre-deploy"
    And the claim token expires
    When I POST /api/runs/{run_id}/hitl/{gate_id}/deliver-manual with expired claim_token and manual output
    Then the response status is 403

  Scenario: Deliver manual with empty output is rejected
    Given a run is waiting at HITL gate "pre-deploy"
    And I have claimed gate "pre-deploy"
    When I POST /api/runs/{run_id}/hitl/{gate_id}/deliver-manual with claim_token and empty output
    Then the response status is 422

  Scenario: Audit event is logged for manual delivery
    Given a run is waiting at HITL gate "pre-deploy"
    And I have claimed gate "pre-deploy"
    When I POST /api/runs/{run_id}/hitl/{gate_id}/deliver-manual with claim_token and manual output
    Then the response status is 200
    And a "hitl.manual_delivery" audit event is logged
    And the audit event contains the manual output
