Feature: Grafana Connector
  As a pipeline author
  I want to query Grafana dashboards, alerts, datasources, folders, organizations, users, and annotations
  So that my agents can monitor infrastructure and observability data

  Background:
    Given I am authenticated in org "acme"

  Scenario: Health check validates token
    Given a Grafana connector configured with valid credentials
    When the connector checks health
    Then the health check returns "healthy"

  Scenario: Invalid token returns unhealthy
    Given a Grafana connector configured with invalid credentials
    When the connector checks health
    Then the health check returns "unhealthy"

  Scenario: Connector lists dashboards
    Given a Grafana connector configured with valid credentials
    When the connector queries dashboards
    Then the result contains Grafana dashboards

  Scenario: Connector gets a dashboard by UID
    Given a Grafana connector configured with valid credentials
    When the connector queries dashboard with uid "abc123"
    Then the result contains the Grafana dashboard

  Scenario: Connector lists alert rules
    Given a Grafana connector configured with valid credentials
    When the connector queries alert rules
    Then the result contains Grafana alert rules

  Scenario: Connector lists datasources
    Given a Grafana connector configured with valid credentials
    When the connector queries datasources
    Then the result contains Grafana datasources

  Scenario: Connector creates an annotation
    Given a Grafana connector configured with valid credentials
    When the connector creates an annotation with text "Deploy completed"
    Then the annotation is created successfully
