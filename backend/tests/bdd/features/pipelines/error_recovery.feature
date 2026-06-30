Feature: Pipeline Error Recovery
  As a pipeline author
  I want the pipeline engine to handle errors gracefully
  So that I can recover from failures without losing progress

  Scenario: NodeInterrupt transitions to awaiting_human
    Given a running pipeline "deploy-service"
    When a HITL gate raises NodeInterrupt
    Then the run status becomes "awaiting_human"

  Scenario: Unhandled exception marks run failed
    Given a running pipeline "deploy-service"
    When a node raises an unhandled exception
    Then the run status becomes "failed"
    And the run has an error_detail

  Scenario: Capacity timeout fails the run
    Given a pipeline with max_concurrent_runs of 1
    And another run is already active
    When the lock wait timeout expires
    Then the run status becomes "failed"
    And the error_code is "lock_timeout"

  Scenario: Eval suite threshold blocks completion
    Given a running pipeline "deploy-service" with eval suite configured
    When post-completion eval thresholds are not met
    Then the run status becomes "failed"
    And the error_code is "eval_suite_blocked"

  Scenario: Run can be resumed from awaiting_human
    Given a run that is awaiting human decision
    When the human approves the gate
    Then the run status becomes "running"
    And execution resumes from the interrupted node

  Scenario: Runaway run (exceeds max steps) is terminated
    Given a pipeline with max_steps of 2
    And a running pipeline with 3 nodes
    When the third node starts
    Then the run status becomes "failed"
    And the error_code is "runaway"

  Scenario: Runaway run (exceeds duration) is terminated
    Given a pipeline with max_duration_seconds of 1
    And a running pipeline with a slow node
    When the node runs longer than 1 second
    Then the run status becomes "failed"
    And the error_code is "runaway"

  Scenario: Output rejected by injection filter
    Given a running pipeline with output injection filter enabled
    When a node produces output containing "ignore all previous instructions"
    Then the run status becomes "output_rejected"

  Scenario: Run can be resumed from checkpoint after server restart
    Given a run that checkpointed after node 1 of 3
    When the server restarts
    And I POST /api/runs/{run_id}/resume
    Then the run restarts from node 2
    And node 1 is not re-executed
