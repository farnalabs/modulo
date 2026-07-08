Feature: HITL Approve
  As an approver
  I want to approve a run waiting at a HITL gate
  So that execution proceeds past the gate

  Background:
    Given I am authenticated as an approver in org "acme"

  Scenario: Approve a claimed gate
    Given a run is waiting at gate "pre-deploy"
    And I have claimed gate "pre-deploy"
    When I POST /api/runs/{run_id}/approve with claim_token and decision "approved"
    Then the response status is 200
    And the run status becomes "running"
    And execution resumes from "pre-deploy"

  Scenario: Approve without claim_token is rejected
    Given a run is waiting at gate "pre-deploy"
    When I POST /api/runs/{run_id}/approve with decision "approved" and no claim_token
    Then the response status is 422

  Scenario: Approve with expired claim_token is rejected
    Given a run is waiting at gate "pre-deploy"
    And I have claimed gate "pre-deploy"
    And the claim token expires
    When I POST /api/runs/{run_id}/approve with expired claim_token and decision "approved"
    Then the response status is 410

  Scenario: Approve a gate claimed by another user is rejected
    Given a run is waiting at gate "pre-deploy"
    And another user has claimed gate "pre-deploy" with claim_token "other-token"
    When I POST /api/runs/{run_id}/approve with claim_token "other-token" and decision "approved"
    Then the response status is 403
