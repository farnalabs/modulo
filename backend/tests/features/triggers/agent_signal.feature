Feature: Agent Signal Triggers
  As a pipeline operator
  I want agent signals to fire child pipeline runs when a watched node completes
  So that cross-pipeline workflows are automated

  Background:
    Given I am authenticated as an admin in org "acme"
    And a source pipeline "pipeline-a" exists

  Scenario: Fire agent signal on node completion
    Given pipeline "child-pipeline" has an agent_signal trigger watching source pipeline "pipeline-a" node "extract"
    When node "extract" completes in pipeline "pipeline-a" with output {"result": "ok"}
    Then a child run is created for pipeline "child-pipeline"
    And the child run has trigger_type "agent_signal"
    And a TriggerEvent is recorded with result "signal_fired"

  Scenario: No matching trigger
    Given no agent_signal trigger watches source pipeline "pipeline-a" node "extract"
    When node "extract" completes in pipeline "pipeline-a"
    Then the result is empty
    And no child run is created

  Scenario: Concurrency limit reached
    Given pipeline "child-pipeline" has an agent_signal trigger with max_concurrent_runs 1
    And pipeline "child-pipeline" has 1 active run
    When node "extract" completes in pipeline "pipeline-a"
    Then the signal is skipped with reason "concurrency_limit"
    And no child run is created
    And a TriggerEvent is recorded with result "concurrency_limit_reached"

  Scenario: Multiple active triggers for same node
    Given pipeline "pipeline-b" has an agent_signal trigger watching source pipeline "pipeline-a" node "extract"
    And pipeline "pipeline-c" has an agent_signal trigger watching source pipeline "pipeline-a" node "extract"
    When node "extract" completes in pipeline "pipeline-a"
    Then 2 child runs are created
    And both results have status "fired"

  Scenario: Inactive trigger is ignored
    Given pipeline "child-pipeline" has an inactive agent_signal trigger watching source pipeline "pipeline-a" node "extract"
    When node "extract" completes in pipeline "pipeline-a"
    Then no child run is created

  Scenario: Different org isolation
    Given org "other-corp" has an agent_signal trigger watching source pipeline "pipeline-a" node "extract"
    When node "extract" completes in pipeline "pipeline-a"
    Then no child run is created in org "other-corp"

  Scenario: Child run inherits input payload
    Given pipeline "child-pipeline" has an agent_signal trigger watching source pipeline "pipeline-a" node "extract"
    When node "extract" completes with output {"key": "value"}
    Then the child run input_payload contains "node_output"
    And the child run input_payload contains "source_run_id"

  Scenario: Invalid snapshot_id in config
    Given pipeline "child-pipeline" has an agent_signal trigger with snapshot_id "not-a-uuid"
    When node "extract" completes in pipeline "pipeline-a"
    Then a child run is created with a valid UUID snapshot_id

  Scenario: TriggerEvent logged on all outcomes
    Given pipeline "child-pipeline" has an agent_signal trigger watching source pipeline "pipeline-a" node "extract"
    When node "extract" completes in pipeline "pipeline-a"
    Then a TriggerEvent is recorded for the fire attempt
