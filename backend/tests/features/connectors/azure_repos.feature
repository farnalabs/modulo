Feature: Azure Repos Connector
  As a pipeline author
  I want to interact with Azure Repos (Azure DevOps) repositories
  So that my agents can read code, review PRs, and list commits

  Background:
    Given I am authenticated in org "myorg"

  Scenario: Connector lists repositories
    Given an Azure Repos connector configured with project "myproject"
    When the connector lists repositories
    Then the result contains repos

  Scenario: Connector reads a file from repository
    Given an Azure Repos connector configured with project "myproject" and repo "myrepo"
    When the connector reads "README.md" from branch "main"
    Then the connector returns the file content

  Scenario: Connector lists pull requests
    Given an Azure Repos connector configured with project "myproject" and repo "myrepo"
    When the connector lists pull requests
    Then the result contains open PRs

  Scenario: Connector lists commits
    Given an Azure Repos connector configured with project "myproject" and repo "myrepo"
    When the connector lists commits on branch "main"
    Then the result contains commits

  Scenario: Connector writes a file via push
    Given an Azure Repos connector configured with project "myproject" and repo "myrepo"
    When the connector writes "src/main.py" with content "print('hello')"
    Then the file is committed successfully

  Scenario: Connector creates a pull request
    Given an Azure Repos connector configured with project "myproject" and repo "myrepo"
    When the connector creates a pull request from "feature-branch" to "main" with title "Add feature"
    Then the pull request is created successfully

  Scenario: Invalid credentials are rejected
    Given an Azure Repos connector configured with invalid credentials
    When the connector checks health
    Then the health check returns "unhealthy"
