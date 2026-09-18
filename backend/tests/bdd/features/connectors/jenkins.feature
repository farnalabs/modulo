Feature: Jenkins Connector
  As a pipeline author
  I want to interact with Jenkins CI/CD jobs
  So that my agents can trigger builds, check status, and view logs

  Background:
    Given I am authenticated in org "acme"

  Scenario: Connector triggers a build
    Given a Jenkins connector configured with job "my-job"
    When the connector triggers a build
    Then the build is queued successfully

  Scenario: Connector triggers a parameterized build
    Given a Jenkins connector configured with job "my-job"
    When the connector triggers a build with parameters
    Then the build is queued with parameters

  Scenario: Connector checks build status
    Given a Jenkins connector configured with job "my-job"
    When the connector checks status of build "42"
    Then the build status is returned

  Scenario: Connector lists recent builds
    Given a Jenkins connector configured with job "my-job"
    When the connector lists recent builds
    Then the result contains builds

  Scenario: Connector fetches build logs
    Given a Jenkins connector configured with job "my-job"
    When the connector fetches logs for build "42"
    Then the logs contain console output

  Scenario: Invalid credentials are rejected
    Given a Jenkins connector configured with invalid credentials
    When the connector checks health
    Then the health check returns "unhealthy"
