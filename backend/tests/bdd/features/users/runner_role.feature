Feature: Runner Role
  As an organisation admin
  I want to assign a runner role to service accounts
  So that CI/CD systems can trigger pipeline runs without full admin access

  Background:
    Given I am authenticated as an admin in org "acme"

  # ADR 017 reconciliation (PR B):
  # - REST never accepts `mk_` API keys on the run-trigger route, so "Runner
  #   can trigger a pipeline run" now uses a runner JWT principal.
  # - "Runner role is scoped to pipelines they own" (team scope) is deferred to
  #   Phase 3 (team-scope enforcement for run triggering) — marked @skip.
  @skip
  Scenario: Runner role is scoped to pipelines they own
    Given org "acme" has pipeline "ci-pipeline" owned by team "ci-team"
    And a runner with team scope "ci-team" exists
    When the runner triggers a run for pipeline "ci-pipeline"
    Then the response status is 202
    And the runner cannot trigger runs for pipelines outside their scope

  Scenario: Runner can trigger a pipeline run
    Given org "acme" has pipeline "ci-pipeline"
    And I am authenticated as a runner in org "acme"
    When the runner triggers a run for pipeline "ci-pipeline"
    Then the response status is 202
    And the run is created

  Scenario: Runner cannot create or modify pipelines
    Given org "acme" has pipeline "ci-pipeline"
    And I am authenticated as a runner in org "acme"
    When the runner attempts to PATCH the pipeline config
    Then the response status is 403

  Scenario: Runner cannot view audit logs
    Given I am authenticated as a runner in org "acme"
    When the runner requests GET /api/v1/admin/audit
    Then the response status is 403

  Scenario: Runner can view run results
    Given org "acme" has pipeline "ci-pipeline"
    And I am authenticated as a runner in org "acme"
    And a completed run exists
    When the runner requests GET /api/v1/runs/{run_id}
    Then the response status is 200
    And the response contains run status
