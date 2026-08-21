Feature: HITL Modify-Then-Approve
  As an approver
  I want to modify the agent's output before approving a HITL gate
  So that the modified output flows to downstream nodes

  Background:
    Given I am authenticated as an approver in org "acme"

  Scenario: Approve with modified output
    Given a run is waiting at gate "review-output"
    And I have claimed gate "review-output"
    When I POST /api/runs/{run_id}/hitl/{gate_id}/approve-with-modification with modified output
    Then the response status is 200
    And the run status becomes "running"
    And the modified output is written into state for downstream nodes

  Scenario: Approve with modification without claim_token is rejected
    Given a run is waiting at gate "review-output"
    When I POST /api/runs/{run_id}/hitl/{gate_id}/approve-with-modification without claim_token
    Then the response status is 403

  Scenario: Approve with modification using expired claim_token is rejected
    Given a run is waiting at gate "review-output"
    And I have claimed gate "review-output"
    And the claim token expires
    When I POST /api/runs/{run_id}/hitl/{gate_id}/approve-with-modification with expired claim_token
    Then the response status is 410

  Scenario: Approve with modification on already-decided gate is rejected
    Given a run is waiting at gate "review-output"
    And the gate has already been decided
    When I POST /api/runs/{run_id}/hitl/{gate_id}/approve-with-modification with valid claim_token
    Then the response status is 409
