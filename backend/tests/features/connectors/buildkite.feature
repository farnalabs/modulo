Feature: Buildkite Connector
  As a pipeline author
  I want to interact with Buildkite pipelines
  So that my agents can trigger runs, check status, and view logs

  Background:
    Given I am authenticated in org "acme"

  Scenario: Connector triggers a pipeline run
    Given a Buildkite connector configured with pipeline "my-org/my-pipeline"
    When the connector triggers a build on branch "main"
    Then the build is created successfully

  Scenario: Connector checks build status
    Given a Buildkite connector configured with pipeline "my-org/my-pipeline"
    When the connector checks status of build "42"
    Then the build status is returned

  Scenario: Connector lists recent builds
    Given a Buildkite connector configured with pipeline "my-org/my-pipeline"
    When the connector lists recent builds
    Then the result contains builds

  Scenario: Connector fetches build logs
    Given a Buildkite connector configured with pipeline "my-org/my-pipeline"
    When the connector fetches logs for build "42"
    Then the logs contain job output

  Scenario: Invalid credentials are rejected
    Given a Buildkite connector configured with invalid credentials
    When the connector checks health
    Then the health check returns "unhealthy"
