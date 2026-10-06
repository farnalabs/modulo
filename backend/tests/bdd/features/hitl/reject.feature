# FAR-1487 note: the runtime now implements "the run stops and is marked as
# rejected": a rejection with no reject route (and no explicit
# `on_reject: proceed`) ENDS the run with the terminal `rejected` status
# (`error_code=hitl.rejected`; `hitl.superseded` for coalesced-supersede system
# rejections). A reject route still routes; `on_reject: proceed` still
# continues. HOWEVER the steps in tests/bdd/steps/test_alpha_hitl.py are still
# mocked (a MagicMock HTTP response and a no-op rejection_reason step), so
# these scenarios remain a vacuous wire-shape check - they do NOT observe a real
# run status. The REAL status/error_code assertions live in
# backend/tests/integration/test_hitl_resume_roundtrip.py (terminate, supersede,
# on_reject: proceed) and backend/tests/unit/pipeline_engine/
# test_hitl_reject_terminate.py (routing, finalize trap, briefing).
@far-1487
Feature: HITL Reject
  As an approver
  I want to reject a run waiting at a HITL review
  So that the run stops and is marked as rejected

  Background:
    Given I am authenticated as an approver in org "acme"

  @far-1487
  Scenario: Reject a claimed gate
    Given a run is waiting at gate "pre-deploy"
    And I have claimed gate "pre-deploy"
    When I POST /api/runs/{run_id}/approve with claim_token and decision "rejected"
    Then the response status is 200
    And the run status becomes "rejected"

  @far-1487
  Scenario: Rejected run includes rejection reason
    Given a run is waiting at gate "pre-deploy"
    And I have claimed gate "pre-deploy"
    When I POST /api/runs/{run_id}/approve with claim_token and decision "rejected" and reason "Not ready"
    Then the run status becomes "rejected"
    And the run has rejection_reason "Not ready"

  @far-1487
  Scenario: Rejected run cannot be approved later
    Given a run is waiting at gate "pre-deploy"
    And I have claimed gate "pre-deploy"
    When I POST /api/runs/{run_id}/approve with claim_token and decision "rejected"
    Then the response status is 200
    When I POST /api/runs/{run_id}/approve with claim_token and decision "approved"
    Then the response status is 409
    And the error mentions "already rejected"
