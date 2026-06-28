Feature: GitHub Connector
  As a pipeline author
  I want to interact with GitHub via the connector
  So that I can read repos, files, PRs and write files

  Scenario: Query repositories returns results
    Given a GitHub connector with valid token
    When I query resource "repos" with limit 5
    Then the result has records
    And the records contain repository metadata

  Scenario: Query a file by repo and path
    Given a GitHub connector with valid token
    When I query resource "file" with filters repo "owner/repo" and path "README.md"
    Then the result has records
    And the record contains file content

  Scenario: Query pull requests by repo and state
    Given a GitHub connector with valid token
    When I query resource "pulls" with filters repo "owner/repo" and state "open"
    Then the result has records

  Scenario: Write to a file creates content
    Given a GitHub connector with valid token
    When I write resource "file" with content "base64content" and path "docs/new.md"
    Then the write succeeds

  Scenario: Unsupported resource raises an error
    Given a GitHub connector with valid token
    When I query resource "invalid"
    Then the result is an error
