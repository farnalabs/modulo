Feature: HITL Claim
  As an approver
  I want to claim a HITL gate so that others know I am reviewing it
  So that multiple people do not approve/reject the same gate

  Background:
    Given I am authenticated as an approver in org "acme"

  Scenario: Claim an unclaimed gate
    Given a run is waiting at gate "pre-deploy"
    When I POST /api/runs/{run_id}/claim
    Then the response status is 200
    And I am the claimant of gate "pre-deploy"

  Scenario: Cannot claim an already claimed gate
    Given a run is waiting at gate "pre-deploy"
    And another user has claimed gate "pre-deploy"
    When I POST /api/runs/{run_id}/claim
    Then the response status is 409
    And the error mentions "already claimed"

  Scenario: Claim has a TTL
    Given a run is waiting at gate "pre-deploy"
    When I POST /api/runs/{run_id}/claim
    And 15 minutes pass
    Then my claim expires
    And another user can claim the gate

  Scenario: Claim token is returned
    Given a run is waiting at gate "pre-deploy"
    When I POST /api/runs/{run_id}/claim
    Then the response contains a claim_token
