Feature: Pipeline Concurrency Control
  As a pipeline operator
  I want to control how many runs of a pipeline can execute concurrently
  So that resource limits are respected

  Background:
    Given I am authenticated as an admin in org "acme"

  # @awaiting-implementation: the per-pipeline runs endpoint (POST /api/pipelines/{id}/runs)
  # and its count_runs_by_status rejection path no longer exist. Runs are triggered via
  # POST /api/v1/runs; concurrency is enforced as admission control inside create_run/dispatch.
  @awaiting-implementation
  Scenario: Concurrent runs within limit are allowed
    Given org "acme" has pipeline "my-pipeline" with max_concurrent_runs 5
    And 2 runs are currently executing for "my-pipeline"
    When I POST /api/pipelines/my-pipeline/runs with empty run_context
    Then the response status is 202

  # @awaiting-implementation: the per-pipeline runs endpoint (POST /api/pipelines/{id}/runs)
  # and its count_runs_by_status rejection path no longer exist. Runs are triggered via
  # POST /api/v1/runs; concurrency is enforced as admission control inside create_run/dispatch.
  @awaiting-implementation
  Scenario: Concurrent runs exceeding limit are rejected
    Given org "acme" has pipeline "my-pipeline" with max_concurrent_runs 2
    And 2 runs are currently executing for "my-pipeline"
    When I POST /api/pipelines/my-pipeline/runs with empty run_context
    Then the response status is 429

  # @awaiting-implementation: the per-pipeline runs endpoint (POST /api/pipelines/{id}/runs)
  # and its count_runs_by_status rejection path no longer exist. Runs are triggered via
  # POST /api/v1/runs; concurrency is enforced as admission control inside create_run/dispatch.
  @awaiting-implementation
  Scenario: Completed run frees concurrency slot
    Given org "acme" has pipeline "my-pipeline" with max_concurrent_runs 1
    And 1 run is currently executing for "my-pipeline"
    When the executing run completes
    And I POST /api/pipelines/my-pipeline/runs with empty run_context
    Then the response status is 202

  # @awaiting-implementation: the per-pipeline runs endpoint (POST /api/pipelines/{id}/runs)
  # and its count_runs_by_status rejection path no longer exist. Runs are triggered via
  # POST /api/v1/runs; concurrency is enforced as admission control inside create_run/dispatch.
  @awaiting-implementation
  Scenario: Different pipelines do not affect each others concurrency
    Given org "acme" has pipeline "pipe-a" with max_concurrent_runs 1
    And org "acme" has pipeline "pipe-b" with max_concurrent_runs 1
    And 1 run is currently executing for "pipe-a"
    When I POST /api/pipelines/pipe-b/runs with empty run_context
    Then the response status is 202

  # @awaiting-implementation: the per-pipeline runs endpoint (POST /api/pipelines/{id}/runs)
  # and its count_runs_by_status rejection path no longer exist. Runs are triggered via
  # POST /api/v1/runs; concurrency is enforced as admission control inside create_run/dispatch.
  @awaiting-implementation
  Scenario: Concurrency limit is enforced per-org
    Given org "acme" has pipeline "my-pipeline" with max_concurrent_runs 1
    And org "othercorp" has pipeline "my-pipeline" with max_concurrent_runs 1
    And 1 run is currently executing for "my-pipeline" in org "acme"
    When I POST /api/pipelines/my-pipeline/runs with empty run_context
    Then the response status is 429
    And a run in org "othercorp" can still be created
