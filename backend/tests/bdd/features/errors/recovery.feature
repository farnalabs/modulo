Feature: Recovery from Failed State
  As a pipeline operator
  I want to recover failed runs by retrying or resuming
  So that I can fix issues and continue without data loss

  Background:
    Given I am authenticated as an admin in org "acme"

  Scenario: Resume from checkpoint after failure
    Given a run that failed at node 2 of 3
    When I POST /api/runs/{run_id}/resume
    Then the response status is 200
    And the run status becomes "running"
    And execution resumes from node 2

  Scenario: Manual fix then resume
    Given a run that failed at node 2 with a configuration error
    When I fix the configuration
    And I POST /api/runs/{run_id}/resume
    Then the run status becomes "running"
    And node 2 completes without error

  Scenario: Recovery preserves node 1 output
    Given a run that failed at node 2 of 3
    When I POST /api/runs/{run_id}/resume
    Then node 1 output is preserved in the resumed run

  Scenario: Already running run cannot be recovered
    Given a running pipeline
    When I POST /api/runs/{run_id}/resume
    Then the response status is 409
    And the error mentions "already running"

  Scenario: Recovery with modified run_context
    Given a run that failed at node 2 of 3
    When I POST /api/runs/{run_id}/resume with run_context {"retry_count": 1}
    Then the run context includes retry_count 1
