Feature: HITL Approval Gate
  As a pipeline author
  I want to pause a run at a defined gate and require human approval
  So that humans remain in control of critical decisions

  Scenario: Run pauses at approval gate
    Given pipeline "deploy-service" has an approval gate at node "pre-deploy"
    When the run reaches the "pre-deploy" node
    Then the run status becomes "waiting_for_approval"
    And the approver is notified via WebSocket

  Scenario: Approved run resumes
    Given a run is waiting at gate "pre-deploy"
    And I am authenticated as an approver
    When I POST /api/runs/{run_id}/approve with decision "approved"
    Then the run status becomes "running"
    And execution resumes from "pre-deploy"

  Scenario: Rejected run stops
    Given a run is waiting at gate "pre-deploy"
    And I am authenticated as an approver
    When I POST /api/runs/{run_id}/approve with decision "rejected"
    Then the run status becomes "rejected"

  Scenario: Gate times out if not actioned
    Given a run is waiting at gate "pre-deploy" with timeout 1s
    When 1 second passes without approval
    Then the run status becomes "timed_out"

  Scenario: Non-approver cannot approve
    Given a run is waiting at gate "pre-deploy"
    And I am authenticated as a viewer (not an approver)
    When I POST /api/runs/{run_id}/approve with decision "approved"
    Then the response status is 403
