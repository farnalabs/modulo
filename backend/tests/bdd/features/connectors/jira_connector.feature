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

  Scenario: Query fails with HTTP error
    Given a Jira connector that returns API errors
    When I query resource "issue" with issue_key "NONEXISTENT"
    Then the result is an error

  Scenario: Write fails with invalid data
    Given a Jira connector that returns API errors
    When I write resource "issue" with empty data
    Then the result is an error

  Scenario: Query results expose rate-limit headers
    Given a Jira connector that reports rate-limit headers
    When I query resource "issue" with issue_key "PROJ-123"
    Then the result has records
    And the result reports rate-limit metadata

  Scenario: Rate-limited query fails with quota details
    Given a Jira connector that is rate limited
    When I query resource "issue" with issue_key "PROJ-123"
    Then the result is an error
    And the error reports the Jira rate-limit quota

  Scenario: Assign an issue to an account
    Given a Jira connector with valid credentials
    When I assign issue "PROJ-123" to account "712020:abc123"
    Then the write succeeds
    And the write returns assignee for issue "PROJ-123"

  Scenario: Unassign an issue
    Given a Jira connector with valid credentials
    When I unassign issue "PROJ-123"
    Then the write succeeds
    And the write returns no assignee

  Scenario: Add and remove issue labels
    Given a Jira connector with valid credentials
    When I update labels on issue "PROJ-123" adding "backend" and removing "stale"
    Then the write succeeds
    And the write returns the updated labels

  Scenario: Delete an issue
    Given a Jira connector with valid credentials
    When I delete issue "PROJ-123"
    Then the write succeeds
    And the write returns deletion confirmation for "PROJ-123"

  Scenario: Discover field metadata for a project
    Given a Jira connector with valid credentials
    When I query field metadata for project "PROJ"
    Then the result has records
    And the records list issue types with create fields

  Scenario: Missing project filter for field metadata raises an error
    Given a Jira connector with valid credentials
    When I query field metadata without a project
    Then the result is an error

  Scenario: Query all available fields
    Given a Jira connector with valid credentials
    When I query all Jira fields
    Then the result has records
    And the records include custom fields

  Scenario: Query project statuses
    Given a Jira connector with valid credentials
    When I query statuses for project "PROJ"
    Then the result has records
    And the records list issue type statuses

  Scenario: Missing project filter for statuses raises an error
    Given a Jira connector with valid credentials
    When I query statuses without a project
    Then the result is an error

  Scenario: List attachments on an issue
    Given a Jira connector with valid credentials
    When I query attachments on issue "PROJ-123"
    Then the result has records
    And the result lists issue attachments

  Scenario: Upload an attachment to an issue
    Given a Jira connector with valid credentials
    When I upload attachment "notes.txt" to issue "PROJ-123" with content
    Then the write succeeds
    And the write returns an uploaded attachment

  Scenario: Missing content on attachment upload raises an error
    Given a Jira connector with valid credentials
    When I upload an attachment to issue "PROJ-123" without content
    Then the result is an error

  Scenario: List remote links on an issue
    Given a Jira connector with valid credentials
    When I list remote links on issue "PROJ-123"
    Then the result has records
    And the records list issue remote links

  Scenario: Add a remote link to an issue
    Given a Jira connector with valid credentials
    When I add remote link "https://example.com/pr/42" to issue "PROJ-123"
    Then the write succeeds
    And the write returns the created remote link

  Scenario: Delete a remote link from an issue
    Given a Jira connector with valid credentials
    When I delete remote link "10001" from issue "PROJ-123"
    Then the write succeeds
    And the write returns remote link deletion for "PROJ-123"

  Scenario: Query project components
    Given a Jira connector with valid credentials
    When I query components for project "PROJ"
    Then the result has records
    And the result lists project components

  Scenario: Missing project filter for components raises an error
    Given a Jira connector with valid credentials
    When I query components without a project
    Then the result is an error

  Scenario: Query project versions
    Given a Jira connector with valid credentials
    When I query versions for project "PROJ"
    Then the result has records
    And the result lists project versions

  Scenario: Missing project filter for versions raises an error
    Given a Jira connector with valid credentials
    When I query versions without a project
    Then the result is an error
