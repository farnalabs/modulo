Feature: Pipeline Run Lifecycle
  As a user
  I want to trigger pipeline runs and observe their status
  So that I can track work done by my agentic workflows

  Scenario: Trigger a run
    Given org "acme" has pipeline "deploy-service"
    And I am authenticated in org "acme"
    When I POST /api/pipelines/deploy-service/runs with empty run_context
    Then the response status is 202
    And the run status is "pending"

  Scenario: Run transitions to running
    Given a pending run exists for pipeline "deploy-service"
    When the pipeline engine picks up the run
    Then the run status becomes "running"

  Scenario: Successful run completes
    Given a running pipeline "deploy-service" with stub model backend
    When all nodes complete without error
    Then the run status becomes "completed"
    And the run has a final_state

  Scenario: Failed run is marked failed
    Given a running pipeline "deploy-service"
    When a node raises an unhandled exception
    Then the run status becomes "failed"
    And the run has an error_detail

  Scenario: Run context is merged correctly
    Given pipeline "deploy-service" has default run_context branch="main"
    When I trigger a run with run_context branch="feature/x"
    Then the effective run context branch is "feature/x"
