Feature: Asana Connector
  As a pipeline author
  I want to interact with Asana workspaces, projects, tasks, and sections
  So that my agents can manage projects on Asana

  Background:
    Given an Asana connector with valid Personal Access Token

  Scenario: Health check with valid credentials
    Given the Asana API returns a valid user profile
    When I perform a health check
    Then the health result is ok

  Scenario: Health check with invalid credentials
    Given the Asana API returns 401 Unauthorized
    When I perform a health check
    Then the health result is not ok

  Scenario: List workspaces
    Given the Asana API returns available workspaces
    When I query resource "workspaces" with limit 10
    Then the result has records
    And the records contain workspace metadata

  Scenario: List projects in a workspace
    Given the Asana API returns projects for a workspace
    When I query resource "projects" with workspace "w1"
    Then the result has records
    And the records contain project metadata

  Scenario: Get single project
    Given the Asana API returns a single project
    When I query resource "project" with project_id "p1"
    Then the result has records
    And the record contains project fields

  Scenario: List tasks in a project
    Given the Asana API returns tasks for a project
    When I query resource "tasks" with project_id "p1"
    Then the result has records

  Scenario: List sections in a project
    Given the Asana API returns sections for a project
    When I query resource "sections" with project_id "p1"
    Then the result has records
    And the records contain section metadata

  Scenario: Create a task
    Given the Asana API accepts task creation
    When I write resource "task" with name "New Task" and project "p1"
    Then the write succeeds

  Scenario: Add comment to task
    Given the Asana API accepts comments
    When I write resource "comment" for task "t1" with text "Nice work!"
    Then the write succeeds

  Scenario: Query unknown resource returns error
    Given the Asana connector is configured
    When I query resource "invalid"
    Then the result is an error
