Feature: Sentry Connector
  As a pipeline author
  I want to query Sentry issues, events, projects, releases, teams, and issue events
  So that my agents can monitor and manage error tracking

  Background:
    Given I am authenticated in org "acme"

  Scenario: Health check validates token
    Given a Sentry connector configured with valid credentials
    When the connector checks health
    Then the health check returns "healthy"

  Scenario: Invalid token returns unhealthy
    Given a Sentry connector configured with invalid credentials
    When the connector checks health
    Then the health check returns "unhealthy"

  Scenario: Connector lists issues
    Given a Sentry connector configured with valid credentials
    When the connector queries issues
    Then the result contains Sentry issues

  Scenario: Connector lists projects
    Given a Sentry connector configured with valid credentials
    When the connector queries projects
    Then the result contains Sentry projects

  Scenario: Connector writes issue status update
    Given a Sentry connector configured with valid credentials
    When the connector updates issue status to "resolved"
    Then the issue status is updated

  Scenario: Connector creates a release
    Given a Sentry connector configured with valid credentials
    When the connector creates a release with version "1.0.0"
    Then the release is created successfully
