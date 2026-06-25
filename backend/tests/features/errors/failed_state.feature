Feature: Failed Run State
  As a pipeline operator
  I want failed runs to preserve their state for inspection
  So that I can debug what went wrong

  Background:
    Given I am authenticated in org "acme"

  Scenario: Failed run has error_detail
    Given a run that failed at node 2 of 3
    When I GET /api/runs/{run_id}
    Then the response has status "failed"
    And the response has error_detail describing the failure

  Scenario: Failed run preserves node outputs
    Given a run that failed at node 2 of 3
    When I inspect the run detail
    Then node 1 output is available
    And node 2 error is available
    And node 3 has no output

  Scenario: Failed run can be inspected via API
    Given a run that failed
    When I GET /api/runs/{run_id}
    Then the response status is 200
    And the response contains final_state
    And the response contains error_detail

  Scenario: Run failure is logged in audit trail
    Given a run that failed
    When I check the audit log
    Then an audit event exists for run failure
