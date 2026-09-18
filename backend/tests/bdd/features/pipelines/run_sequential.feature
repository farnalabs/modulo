Feature: Sequential Pipeline Run
  As a pipeline operator
  I want to trigger a run and have nodes execute sequentially
  So that each agent processes output from the previous node

  Background:
    Given I am authenticated in org "acme"

  Scenario: Trigger a manual run
    Given org "acme" has pipeline "my-pipeline"
    When I POST /api/pipelines/my-pipeline/runs with empty run_context
    Then the response status is 202
    And a run is created with status "pending"

  Scenario: Run progresses through nodes in order
    Given a running pipeline with 3 nodes
    When the pipeline engine picks up the run
    Then the run status becomes "running"
    And node 1 completes before node 2 starts

  Scenario: All nodes complete successfully
    Given a running pipeline with 3 nodes
    When the pipeline engine picks up the run
    And all nodes complete without error
    Then the run status becomes "completed"
    And the run has a final_state

  Scenario: Run context is passed between nodes
    Given pipeline "my-pipeline" has default run_context branch="main"
    When I trigger a run with run_context branch="feature-x"
    Then the effective run context branch is "feature-x"

  Scenario: Run respects max concurrent runs
    Given org "acme" has pipeline "my-pipeline" with max_concurrent_runs 1
    And a pending run exists for pipeline "my-pipeline"
    When I POST /api/pipelines/my-pipeline/runs with empty run_context
    Then the response status is 429
