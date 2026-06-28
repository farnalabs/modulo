Feature: Linear Connector
  As a pipeline author
  I want to interact with Linear via the connector
  So that I can read, search, create, and update issues

  Scenario: Query a single issue by ID
    Given a Linear connector with valid API key
    When I query resource "issue" with id "uuid-1234"
    Then the result has records
    And the record contains issue fields

  Scenario: Search issues by text
    Given a Linear connector with valid API key
    When I query resource "search" with query "bug"
    Then the result has records

  Scenario: Create an issue
    Given a Linear connector with valid API key
    When I write resource "issue" with title "New bug" and team "ENG"
    Then the write succeeds

  Scenario: Update an issue
    Given a Linear connector with valid API key
    When I write resource "issue_update" with id "uuid-1234" and new title
    Then the write succeeds

  Scenario: Health check returns valid response
    Given a Linear connector with valid API key
    When I perform a health check
    Then the health result is ok
