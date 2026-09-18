Feature: CircleCI Connector
  As a pipeline author
  I want to interact with CircleCI pipelines
  So that my agents can trigger runs, check status, and view logs

  Background:
    Given I am authenticated in org "acme"

  Scenario: Connector triggers a pipeline run
    Given a CircleCI connector configured with project "gh/owner/repo"
    When the connector triggers a pipeline on branch "main"
    Then the pipeline run is created successfully

  Scenario: Connector checks pipeline status
    Given a CircleCI connector configured with project "gh/owner/repo"
    When the connector checks status of pipeline "pipe-uuid-123"
    Then the pipeline status is returned

  Scenario: Connector lists recent runs
    Given a CircleCI connector configured with project "gh/owner/repo"
    When the connector lists recent pipeline runs
    Then the result contains pipeline runs

  Scenario: Connector fetches pipeline logs
    Given a CircleCI connector configured with project "gh/owner/repo"
    When the connector fetches logs for pipeline "pipe-uuid-123"
    Then the logs contain workflow and job output

  Scenario: Invalid credentials are rejected
    Given a CircleCI connector configured with invalid credentials
    When the connector checks health
    Then the health check returns "unhealthy"
