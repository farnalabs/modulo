Feature: Pipeline Concurrency Control
  As a pipeline operator
  I want to control how many runs of a pipeline can execute concurrently
  So that resource limits are respected

  Background:
    Given I am authenticated as an admin in org "acme"

  Scenario: Concurrent runs within limit are allowed
    Given org "acme" has pipeline "my-pipeline" with max_concurrent_runs 5
    And 2 runs are currently executing for "my-pipeline"
    When I POST /api/pipelines/my-pipeline/runs with empty run_context
    Then the response status is 202

  Scenario: Concurrent runs exceeding limit are rejected
    Given org "acme" has pipeline "my-pipeline" with max_concurrent_runs 2
    And 2 runs are currently executing for "my-pipeline"
    When I POST /api/pipelines/my-pipeline/runs with empty run_context
    Then the response status is 429

  Scenario: Completed run frees concurrency slot
    Given org "acme" has pipeline "my-pipeline" with max_concurrent_runs 1
    And 1 run is currently executing for "my-pipeline"
    When the executing run completes
    And I POST /api/pipelines/my-pipeline/runs with empty run_context
    Then the response status is 202

  Scenario: Different pipelines do not affect each others concurrency
    Given org "acme" has pipeline "pipe-a" with max_concurrent_runs 1
    And org "acme" has pipeline "pipe-b" with max_concurrent_runs 1
    And 1 run is currently executing for "pipe-a"
    When I POST /api/pipelines/pipe-b/runs with empty run_context
    Then the response status is 202

  Scenario: Concurrency limit is enforced per-org
    Given org "acme" has pipeline "my-pipeline" with max_concurrent_runs 1
    And org "othercorp" has pipeline "my-pipeline" with max_concurrent_runs 1
    And 1 run is currently executing for "my-pipeline" in org "acme"
    When I POST /api/pipelines/my-pipeline/runs with empty run_context
    Then the response status is 429
    And a run in org "othercorp" can still be created
