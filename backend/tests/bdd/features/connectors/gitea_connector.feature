Feature: Gitea Connector
  As a pipeline author
  I want to interact with Gitea via the connector
  So that I can read repos, files, PRs, issues, and write content

  Scenario: Query repositories returns results
    Given a Gitea connector with valid token
    When I query resource "repos" with limit 5
    Then the result has records
    And the records contain repository metadata

  Scenario: Query a file by repo and path
    Given a Gitea connector with valid token
    When I query resource "file" with filters repo "owner/repo" and path "README.md"
    Then the result has records
    And the record contains file content

  Scenario: Query pull requests by repo and state
    Given a Gitea connector with valid token
    When I query resource "pulls" with filters repo "owner/repo" and state "open"
    Then the result has records

  Scenario: Query issues by repo and state
    Given a Gitea connector with valid token
    When I query resource "issues" with filters repo "owner/repo" and state "open"
    Then the result has records

  Scenario: Write to a file creates content
    Given a Gitea connector with valid token
    When I write resource "file" with content "base64content" and path "docs/new.md"
    Then the write succeeds

  Scenario: Unsupported resource raises an error
    Given a Gitea connector with valid token
    When I query resource "invalid"
    Then the result is an error

  Scenario: Create a pull request
    Given a Gitea connector with valid token
    When I write Gitea resource "pull" with title "Fix bug" head "feature" and base "main"
    Then the write succeeds

  Scenario: Create an issue
    Given a Gitea connector with valid token
    When I write Gitea resource "issue" with title "Bug report"
    Then the write succeeds
