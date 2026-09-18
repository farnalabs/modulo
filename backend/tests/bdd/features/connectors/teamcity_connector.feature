Feature: TeamCity Connector
  As a pipeline author
  I want to interact with TeamCity CI/CD via the connector
  So that I can trigger builds, check status, and view logs

  Scenario: Query projects returns results
    Given a TeamCity connector with valid token
    When I query resource "projects"
    Then the result has records
    And the records contain project metadata

  Scenario: Query build types by project
    Given a TeamCity connector with valid token
    When I query resource "buildTypes" with filters project_id "ProjectA"
    Then the result has records

  Scenario: Query agents returns results
    Given a TeamCity connector with valid token
    When I query resource "agents"
    Then the result has records
    And the records contain agent metadata

  Scenario: Trigger a build
    Given a TeamCity connector with valid token
    When I write resource "build" with buildTypeId "MyBuild"
    Then the write succeeds

  Scenario: Create a build type
    Given a TeamCity connector with valid token
    When I write resource "buildType" with buildTypeId "BT_New", projectId "ProjectA", and name "New Build Type"
    Then the write succeeds

  Scenario: Unsupported resource raises an error
    Given a TeamCity connector with valid token
    When I query resource "invalid"
    Then the result is an error
