Feature: Manual Trigger
  As a pipeline operator
  I want to manually trigger a pipeline run with optional run_context
  So that I can start a workflow on demand

  Background:
    Given I am authenticated in org "acme"

  Scenario: Trigger a run manually
    Given org "acme" has pipeline "my-pipeline"
    When I POST /api/pipelines/my-pipeline/runs with empty run_context
    Then the response status is 202
    And a run is created with status "pending"

  Scenario: Trigger with run_context
    Given org "acme" has pipeline "my-pipeline"
    When I POST /api/pipelines/my-pipeline/runs with run_context branch="main"
    Then the response status is 202
    And the run has run_context with branch "main"

  Scenario: Trigger non-existent pipeline returns 404
    Given no pipeline exists with slug "ghost"
    When I POST /api/pipelines/ghost/runs with empty run_context
    Then the response status is 404

  Scenario: Paused pipeline cannot be triggered
    Given org "acme" has pipeline "my-pipeline" with status "paused"
    When I POST /api/pipelines/my-pipeline/runs with empty run_context
    Then the response status is 409
    And the error mentions "paused"

  Scenario: Trigger type is recorded as "manual"
    Given org "acme" has pipeline "my-pipeline"
    When I POST /api/pipelines/my-pipeline/runs with empty run_context
    Then the run has trigger_type "manual"
