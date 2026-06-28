Feature: Jira Connector
  As a pipeline author
  I want to interact with Jira via the connector
  So that I can read and create issues

  Scenario: Query a single issue by key
    Given a Jira connector with valid credentials
    When I query resource "issue" with issue_key "PROJ-123"
    Then the result has records
    And the record contains issue fields

  Scenario: Search issues via JQL
    Given a Jira connector with valid credentials
    When I query resource "search" with JQL "project = PROJ"
    Then the result has records

  Scenario: Create an issue
    Given a Jira connector with valid credentials
    When I write resource "issue" with summary "Test issue" and project "PROJ"
    Then the write succeeds

  Scenario: Update an issue
    Given a Jira connector with valid credentials
    When I write resource "issue_update" with issue_key "PROJ-123" and updated fields
    Then the write succeeds

  Scenario: Missing issue_key on query raises an error
    Given a Jira connector with valid credentials
    When I query resource "issue" without issue_key
    Then the result is an error
