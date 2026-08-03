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

  Scenario: Query with unsupported resource returns error
    Given a Linear connector with valid API key
    When I query resource "unknown_resource" with id "x"
    Then the result is an error

  Scenario: Write with unsupported resource returns error
    Given a Linear connector with valid API key
    When I write resource "delete" with title "nope" and team "ENG"
    Then the write fails

  Scenario: Write create issue returns error on API failure
    Given a Linear connector that returns API errors
    When I write resource "issue" with title "Failing issue" and team "ENG"
    Then the write fails

  Scenario: Transition an issue to a workflow state by name
    Given a Linear connector with valid API key
    When I transition Linear issue "uuid-1234" to state "In Progress" in team "ENG"
    Then the write succeeds
    And the issue state is "In Progress"

  Scenario: Transition an issue to a workflow state by raw state ID
    Given a Linear connector with valid API key
    When I transition Linear issue "uuid-1234" to state id "state-9"
    Then the write succeeds

  Scenario: Assign an issue to a cycle by name
    Given a Linear connector with valid API key
    When I assign Linear issue "uuid-1234" to cycle "Sprint 24" in team "ENG"
    Then the write succeeds

  Scenario: Assign an issue to a cycle by raw cycle ID
    Given a Linear connector with valid API key
    When I assign Linear issue "uuid-1234" to cycle id "cy-1"
    Then the write succeeds

  Scenario: Remove an issue from a cycle
    Given a Linear connector with valid API key
    When I remove Linear issue "uuid-1234" from its cycle
    Then the write succeeds

  Scenario: Create a label
    Given a Linear connector with valid API key
    When I create Linear label "bug" in team "ENG"
    Then the write succeeds

  Scenario: Rename a label
    Given a Linear connector with valid API key
    When I update Linear label "lb-1" to name "critical"
    Then the write succeeds

  Scenario: Delete a label
    Given a Linear connector with valid API key
    When I delete Linear label "lb-1"
    Then the write succeeds

  Scenario: State transition with missing team raises an error
    Given a Linear connector with valid API key
    When I transition Linear issue "uuid-1234" to state "In Progress" without a team
    Then the write fails
