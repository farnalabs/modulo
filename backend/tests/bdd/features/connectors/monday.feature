Feature: Monday.com Connector
  As a pipeline author
  I want to interact with Monday.com boards, items, and users
  So that my agents can manage projects on Monday.com

  Background:
    Given a Monday.com connector with valid API key

  Scenario: Health check with valid credentials
    Given the Monday.com API returns a valid user profile
    When I perform a health check
    Then the health result is ok

  Scenario: Health check with invalid credentials
    Given the Monday.com API returns 401 Unauthorized
    When I perform a health check
    Then the health result is not ok

  Scenario: List boards
    Given the Monday.com API returns available boards
    When I query resource "boards" with limit 10
    Then the result has records
    And the records contain board metadata

  Scenario: Get single board
    Given the Monday.com API returns a single board
    When I query resource "board" with board_id "10"
    Then the result has records

  Scenario: List items on a board
    Given the Monday.com API returns items for a board
    When I query resource "items" with board_id "10"
    Then the result has records

  Scenario: Get single item
    Given the Monday.com API returns a single item
    When I query resource "item" with item_id "201"
    Then the result has records

  Scenario: List users
    Given the Monday.com API returns users
    When I query resource "users" with limit 10
    Then the result has records
    And the records contain user fields

  Scenario: List workspaces
    Given the Monday.com API returns workspaces
    When I query resource "workspaces" with limit 10
    Then the result has records

  Scenario: Create an item
    Given the Monday.com API accepts item creation
    When I write resource "item" with name "New Task" and board_id "10"
    Then the write succeeds

  Scenario: Update item column values
    Given the Monday.com API accepts item column updates
    When I write resource "item_update" for item "301" with column values '{"status": "Done"}'
    Then the write succeeds

  Scenario: Change single column value
    Given the Monday.com API accepts single column value changes
    When I write resource "column_value" for item "301" with column_id "status" and value '"Done"'
    Then the write succeeds

  Scenario: Add update to item
    Given the Monday.com API accepts updates
    When I write resource "update" for item "301" with body "Update body text"
    Then the write succeeds

  Scenario: Query unknown resource returns error
    Given the Monday.com connector is configured
    When I query resource "invalid"
    Then the result is an error
