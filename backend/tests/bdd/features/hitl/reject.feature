Feature: HITL Reject
  As an approver
  I want to reject a run waiting at a HITL gate
  So that the run stops and is marked as rejected

  Background:
    Given I am authenticated as an approver in org "acme"

  Scenario: Reject a claimed gate
    Given a run is waiting at gate "pre-deploy"
    And I have claimed gate "pre-deploy"
    When I POST /api/runs/{run_id}/approve with claim_token and decision "rejected"
    Then the response status is 200
    And the run status becomes "rejected"

  Scenario: Rejected run includes rejection reason
    Given a run is waiting at gate "pre-deploy"
    And I have claimed gate "pre-deploy"
    When I POST /api/runs/{run_id}/approve with claim_token and decision "rejected" and reason "Not ready"
    Then the run status becomes "rejected"
    And the run has rejection_reason "Not ready"

  Scenario: Rejected run cannot be approved later
    Given a run is waiting at gate "pre-deploy"
    And I have claimed gate "pre-deploy"
    When I POST /api/runs/{run_id}/approve with claim_token and decision "rejected"
    Then the response status is 200
    When I POST /api/runs/{run_id}/approve with claim_token and decision "approved"
    Then the response status is 409
    And the error mentions "already rejected"
