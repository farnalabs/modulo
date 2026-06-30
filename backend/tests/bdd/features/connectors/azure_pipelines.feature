Feature: Azure Pipelines Connector
  As a pipeline author
  I want to interact with Azure Pipelines CI/CD via the connector
  So that I can trigger builds, check status, and view logs

  Scenario: Query projects returns results
    Given an Azure Pipelines connector with valid credentials
    When I query resource "projects"
    Then the result has records
    And the records contain project metadata

  Scenario: Query pipelines returns results
    Given an Azure Pipelines connector with valid credentials
    When I query resource "pipelines"
    Then the result has records
    And the records contain pipeline metadata

  Scenario: Query runs for a pipeline
    Given an Azure Pipelines connector with valid credentials
    When I query resource "runs" with pipeline_id "1"
    Then the result has records

  Scenario: Query releases returns results
    Given an Azure Pipelines connector with valid credentials
    When I query resource "releases"
    Then the result has records

  Scenario: Trigger a pipeline run
    Given an Azure Pipelines connector with valid credentials
    When I write resource "run" with pipeline_id "1" and branch "main"
    Then the write succeeds

  Scenario: Trigger a release
    Given an Azure Pipelines connector with valid credentials
    When I write resource "release" with definition_id "1"
    Then the write succeeds

  Scenario: Unsupported resource raises an error
    Given an Azure Pipelines connector with valid credentials
    When I query resource "invalid"
    Then the result is an error
