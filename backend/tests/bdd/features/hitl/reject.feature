# FAR-1486 note: the scenarios below assert the INTENDED behaviour — "the run
# stops and is marked as rejected" — which the runtime does not yet implement.
# The steps in tests/bdd/steps/test_alpha_hitl.py are mocked/suppressed (a
# MagicMock response, and a no-op rejection_reason step), so the scenarios are
# vacuous: they do not observe a real run status. Actual current behaviour: a
# rejection routes to the gate's reject_target/reject edge when one is
# configured; when the gate declares no reject route the run CONTINUES along
# its normal edge instead of terminating. FAR-1487 changes the default so a
# rejection terminates the run. Do not treat these scenarios as evidence of
# shipped behaviour until that lands.
Feature: HITL Reject
  As an approver
  I want to reject a run waiting at a HITL review
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
