Feature: PagerDuty Connector
  As a pipeline author
  I want to manage PagerDuty incidents, services, teams, users, escalation policies, schedules, and on-calls
  So that my agents can handle incident management workflows

  Background:
    Given I am authenticated in org "acme"

  Scenario: Health check validates token
    Given a PagerDuty connector configured with valid credentials
    When the connector checks health
    Then the health check returns "healthy"

  Scenario: Invalid token returns unhealthy
    Given a PagerDuty connector configured with invalid credentials
    When the connector checks health
    Then the health check returns "unhealthy"

  Scenario: Connector lists incidents
    Given a PagerDuty connector configured with valid credentials
    When the connector queries incidents
    Then the result contains PagerDuty incidents

  Scenario: Connector lists services
    Given a PagerDuty connector configured with valid credentials
    When the connector queries services
    Then the result contains PagerDuty services

  Scenario: Connector triggers a new incident
    Given a PagerDuty connector configured with valid credentials
    When the connector triggers an incident with title "Test incident" and service "ABC123"
    Then the incident is triggered successfully

  Scenario: Connector acknowledges an incident
    Given a PagerDuty connector configured with valid credentials
    When the connector acknowledges incident "INCIDENT_ID"
    Then the incident is acknowledged

  Scenario: Connector resolves an incident
    Given a PagerDuty connector configured with valid credentials
    When the connector resolves incident "INCIDENT_ID"
    Then the incident is resolved
