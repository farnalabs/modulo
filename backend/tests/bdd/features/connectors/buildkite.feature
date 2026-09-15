Feature: Buildkite Connector
  As a pipeline author
  I want to trigger and observe Buildkite pipeline builds
  So that my agents can run CI/CD pipelines and inspect their results

  Background:
    Given I am authenticated in org "acme"

  Scenario: Health check validates token
    Given a Buildkite connector configured with valid credentials
    When the connector checks health
    Then the health check returns "healthy"

  Scenario: Invalid token returns unhealthy
    Given a Buildkite connector configured with invalid credentials
    When the connector checks health
    Then the health check returns "unhealthy"

  Scenario: Connector triggers a pipeline build
    Given a Buildkite connector configured with valid credentials
    When the connector triggers a build on branch "main"
    Then a build is created successfully

  Scenario: Connector checks build status
    Given a Buildkite connector configured with valid credentials
    When the connector checks status of build "42"
    Then the build status is returned

  Scenario: Connector lists recent builds
    Given a Buildkite connector configured with valid credentials
    When the connector lists recent builds
    Then the result contains builds

  Scenario: Connector fetches build logs
    Given a Buildkite connector configured with valid credentials
    When the connector fetches logs for build "42"
    Then the logs contain job output
