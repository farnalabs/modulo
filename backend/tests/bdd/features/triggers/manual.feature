Feature: Manual Trigger
  As a pipeline operator
  I want to manually trigger a pipeline run with optional run_context
  So that I can start a workflow on demand

  Background:
    Given I am authenticated in org "acme"

  Scenario: Trigger a run manually
    Given org "acme" has pipeline "my-pipeline"
    When I POST /api/v1/runs for "my-pipeline" with empty run_context
    Then the response status is 202
    And a run is created with status "pending"

  Scenario: Trigger with run_context
    Given org "acme" has pipeline "my-pipeline"
    When I POST /api/v1/runs for "my-pipeline" with run_context branch="main"
    Then the response status is 202
    And the run has run_context with branch "main"

  Scenario: Trigger non-existent pipeline returns 404
    Given no pipeline exists with slug "ghost"
    When I POST /api/v1/runs for a non-existent pipeline
    Then the response status is 404

  Scenario: Trigger type is recorded as "manual"
    Given org "acme" has pipeline "my-pipeline"
    When I POST /api/v1/runs for "my-pipeline" with empty run_context
    Then the run is created with trigger_type "manual"
