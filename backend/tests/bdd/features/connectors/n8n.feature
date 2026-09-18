Feature: n8n Workflow Automation Connector
  As a pipeline author
  I want to interact with n8n via the connector
  So that I can manage workflows, executions, credentials, webhooks, tags, and nodes

  Scenario: Health check returns ok
    Given an n8n connector with valid token
    When I perform a health check
    Then the health result is ok

  Scenario: Health check returns not ok for invalid token
    Given an n8n connector with invalid token
    When I perform a health check
    Then the health result is not ok

  Scenario: Query workflows returns results
    Given an n8n connector with valid token
    When I query n8n resource "workflows"
    Then the result has records

  Scenario: Query workflows with limit returns limited results
    Given an n8n connector with valid token
    When I query n8n resource "workflows" with limit 5
    Then the result has records

  Scenario: Query workflows with active filter returns active workflows
    Given an n8n connector with valid token
    When I query n8n resource "workflows" with filter "active" value "true"
    Then the result has records

  Scenario: Query single workflow by id returns a workflow
    Given an n8n connector with valid token
    When I query n8n resource "workflow" with id "W1"
    Then the result has records

  Scenario: Query workflow without id raises error
    Given an n8n connector with valid token
    When I query n8n resource "workflow" without id
    Then the result is an error

  Scenario: Query executions returns results
    Given an n8n connector with valid token
    When I query n8n resource "executions"
    Then the result has records

  Scenario: Query executions with status filter returns filtered results
    Given an n8n connector with valid token
    When I query n8n resource "executions" with filter "status" value "success"
    Then the result has records

  Scenario: Query single execution by id returns an execution
    Given an n8n connector with valid token
    When I query n8n resource "execution" with id "E1"
    Then the result has records

  Scenario: Query execution without id raises error
    Given an n8n connector with valid token
    When I query n8n resource "execution" without id
    Then the result is an error

  Scenario: Query webhooks returns results
    Given an n8n connector with valid token
    When I query n8n resource "webhooks"
    Then the result has records

  Scenario: Query credentials returns results
    Given an n8n connector with valid token
    When I query n8n resource "credentials"
    Then the result has records

  Scenario: Query single credential by id returns a credential
    Given an n8n connector with valid token
    When I query n8n resource "credential" with id "C1"
    Then the result has records

  Scenario: Query credential without id raises error
    Given an n8n connector with valid token
    When I query n8n resource "credential" without id
    Then the result is an error

  Scenario: Query tags returns results
    Given an n8n connector with valid token
    When I query n8n resource "tags"
    Then the result has records

  Scenario: Query nodes returns results
    Given an n8n connector with valid token
    When I query n8n resource "nodes"
    Then the result has records

  Scenario: Write a new workflow succeeds
    Given an n8n connector with valid token
    When I write n8n resource "workflow" with name "Test Workflow"
    Then the write succeeds

  Scenario: Write workflow without name raises error
    Given an n8n connector with valid token
    When I write n8n resource "workflow" without name
    Then the write is an error

  Scenario: Write workflow update succeeds
    Given an n8n connector with valid token
    When I write n8n resource "workflow_update" with id "W1" and name "Updated"
    Then the write succeeds

  Scenario: Write workflow update without id raises error
    Given an n8n connector with valid token
    When I write n8n resource "workflow_update" without id
    Then the write is an error

  Scenario: Write workflow activate succeeds
    Given an n8n connector with valid token
    When I write n8n resource "workflow_activate" with id "W1"
    Then the write succeeds

  Scenario: Write workflow deactivate succeeds
    Given an n8n connector with valid token
    When I write n8n resource "workflow_deactivate" with id "W1"
    Then the write succeeds

  Scenario: Write workflow delete succeeds
    Given an n8n connector with valid token
    When I write n8n resource "workflow_delete" with id "W1"
    Then the write succeeds

  Scenario: Write credential succeeds
    Given an n8n connector with valid token
    When I write n8n resource "credential" with name "MyCred" type "github"
    Then the write succeeds

  Scenario: Write credential without type raises error
    Given an n8n connector with valid token
    When I write n8n resource "credential" with name "BadCred" type ""
    Then the write is an error

  Scenario: Write execution delete succeeds
    Given an n8n connector with valid token
    When I write n8n resource "execution_delete" with id "E1"
    Then the write succeeds

  Scenario: Write execution retry succeeds
    Given an n8n connector with valid token
    When I write n8n resource "execution_retry" with id "E1"
    Then the write succeeds

  Scenario: Unsupported query resource raises error
    Given an n8n connector with valid token
    When I query n8n resource "invalid_resource"
    Then the result is an error

  Scenario: Unsupported write resource raises error
    Given an n8n connector with valid token
    When I write n8n resource "invalid_resource" with name "test"
    Then the write is an error

  Scenario: Health check with unreachable server
    Given an n8n connector with valid token
    And the n8n server is unreachable
    When I perform a health check
    Then the health result is not ok
