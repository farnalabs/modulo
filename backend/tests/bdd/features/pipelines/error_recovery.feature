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
