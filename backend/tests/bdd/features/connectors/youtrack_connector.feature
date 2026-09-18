Feature: YouTrack Connector
  As a pipeline author
  I want to interact with YouTrack via the connector
  So that I can read and create issues

  Scenario: Query issues with a search query
    Given a YouTrack connector with valid credentials
    When I query YouTrack resource "issues" with query "project: PRJ"
    Then the result has records

  Scenario: Query a single issue by id
    Given a YouTrack connector with valid credentials
    When I query YouTrack resource "issue" with issue_id "PRJ-42"
    Then the result has records
    And the record contains issue fields

  Scenario: Query projects
    Given a YouTrack connector with valid credentials
    When I query YouTrack resource "projects"
    Then the result has records

  Scenario: Query users
    Given a YouTrack connector with valid credentials
    When I query YouTrack resource "users"
    Then the result has records

  Scenario: Create an issue
    Given a YouTrack connector with valid credentials
    When I write YouTrack resource "issue" with summary "Test issue" and project "PRJ"
    Then the write succeeds

  Scenario: Update an issue
    Given a YouTrack connector with valid credentials
    When I write YouTrack resource "issue_update" with issue_id "PRJ-42" and updated fields
    Then the write succeeds

  Scenario: Add a comment to an issue
    Given a YouTrack connector with valid credentials
    When I write YouTrack resource "comment" with issue_id "PRJ-42" and text "Looking into it"
    Then the write succeeds

  Scenario: Missing issue_id on query raises an error
    Given a YouTrack connector with valid credentials
    When I query YouTrack resource "issue" without issue_id
    Then the result is an error
