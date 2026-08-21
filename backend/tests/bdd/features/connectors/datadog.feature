Feature: Datadog Connector
  As a pipeline author
  I want to query Datadog monitors, events, metrics, dashboards, and logs
  So that my agents can monitor and observe application infrastructure

  Background:
    Given I am authenticated in org "acme"

  Scenario: Health check validates API key
    Given a Datadog connector configured with valid credentials
    When the connector checks health
    Then the health check returns "healthy"

  Scenario: Invalid API key returns unhealthy
    Given a Datadog connector configured with invalid credentials
    When the connector checks health
    Then the health check returns "unhealthy"

  Scenario: Connector lists monitors
    Given a Datadog connector configured with valid credentials
    When the connector queries monitors
    Then the result contains Datadog monitors

  Scenario: Connector lists events
    Given a Datadog connector configured with valid credentials
    When the connector queries events
    Then the result contains Datadog events

  Scenario: Connector queries metrics
    Given a Datadog connector configured with valid credentials
    When the connector queries timeseries metrics
    Then the result contains metric data

  Scenario: Connector lists dashboards
    Given a Datadog connector configured with valid credentials
    When the connector queries dashboards
    Then the result contains dashboards

  Scenario: Connector searches logs
    Given a Datadog connector configured with valid credentials
    When the connector searches logs
    Then the result contains log events

  Scenario: Connector creates a custom event
    Given a Datadog connector configured with valid credentials
    When the connector writes an event with title "Deploy complete" and text "v2.1.0 deployed"
    Then the event is created successfully

  Scenario: Connector creates a monitor
    Given a Datadog connector configured with valid credentials
    When the connector creates a monitor with type "metric alert"
    Then the monitor is created successfully

  Scenario: Connector updates monitor status
    Given a Datadog connector configured with valid credentials
    When the connector mutes a monitor
    Then the monitor status is updated
