Feature: SonarQube Connector
  As a pipeline author
  I want to interact with SonarQube via the connector
  So that I can query projects, measures, issues, quality gates, and manage findings

  Scenario: Health check returns GREEN
    Given a SonarQube connector with valid token
    When I perform a health check
    Then the health result is ok

  Scenario: Health check returns RED
    Given a SonarQube connector with valid token
    And the SonarQube API returns unhealthy status
    When I perform a health check
    Then the health result is not ok

  Scenario: Query projects returns results
    Given a SonarQube connector with valid token
    When I query resource "projects" with limit 10
    Then the result has records

  Scenario: Query project analyses returns results
    Given a SonarQube connector with valid token
    When I query resource "project_analyses" with project "my-project"
    Then the result has records

  Scenario: Query measures for a component returns results
    Given a SonarQube connector with valid token
    When I query resource "measures" with component "my-project" and metricKeys "coverage,bugs"
    Then the result has records

  Scenario: Query issues returns results
    Given a SonarQube connector with valid token
    When I query resource "issues" with component "my-project"
    Then the result has records

  Scenario: Query quality gates returns results
    Given a SonarQube connector with valid token
    When I query resource "quality_gates" with limit 10
    Then the result has records

  Scenario: Query a specific quality gate returns detail
    Given a SonarQube connector with valid token
    When I query resource "quality_gate" with id "1"
    Then the result has records

  Scenario: Query installed plugins returns results
    Given a SonarQube connector with valid token
    When I query resource "plugins" with limit 10
    Then the result has records

  Scenario: Query security hotspots returns results
    Given a SonarQube connector with valid token
    When I query resource "hotspots" with project "my-project"
    Then the result has records

  Scenario: Write an issue comment succeeds
    Given a SonarQube connector with valid token
    When I write SonarQube resource "issue_comment" with issue "ISSUE1" and text "Looking into this"
    Then the write succeeds

  Scenario: Transition issue status resolves it
    Given a SonarQube connector with valid token
    When I write SonarQube resource "issue_status" with issue "ISSUE1" and transition "resolve"
    Then the write succeeds

  Scenario: Create a quality gate succeeds
    Given a SonarQube connector with valid token
    When I write SonarQube resource "gate" with name "Strict Gate"
    Then the write succeeds

  Scenario: Missing project filter for hotspots raises an error
    Given a SonarQube connector with valid token
    When I query resource "hotspots" without project filter
    Then the result is an error
