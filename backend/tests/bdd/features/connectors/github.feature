Feature: GitHub Connector
  As a pipeline author
  I want to interact with GitHub repositories
  So that my agents can read code, review PRs, and manage issues

  Background:
    Given I am authenticated in org "acme"

  Scenario: Connector lists pull requests
    Given a GitHub connector configured with repo "my-org/my-repo"
    When the connector lists pull requests
    Then the result contains open PRs

  Scenario: Connector reads a file from repository
    Given a GitHub connector configured with repo "my-org/my-repo"
    When the connector reads "README.md" from branch "main"
    Then the connector returns the file content

  Scenario: Connector creates an issue
    Given a GitHub connector configured with repo "my-org/my-repo"
    When the connector creates an issue with title "Bug found" and body "Details"
    Then the issue is created successfully

  Scenario: Connector posts a PR comment
    Given a GitHub connector configured with repo "my-org/my-repo"
    And a pull request exists with number 42
    When the connector comments on PR 42 with "Looks good"
    Then the comment is posted successfully

  Scenario: Invalid credentials are rejected
    Given a GitHub connector configured with invalid credentials
    When the connector checks health
    Then the health check returns "unhealthy"
