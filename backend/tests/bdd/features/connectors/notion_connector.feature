Feature: Notion Connector
  As a pipeline author
  I want to interact with Notion via the connector
  So that I can search databases, query pages, read blocks, and manage content

  Scenario: Health check returns valid response
    Given a Notion connector with valid token
    When I perform a health check
    Then the health result is ok

  Scenario: Health check returns error on failure
    Given a Notion connector with valid token
    And the Notion API returns 401 Unauthorized
    When I perform a health check
    Then the health result is not ok

  Scenario: Search databases returns results
    Given a Notion connector with valid token
    When I query resource "databases" with limit 10
    Then the result has records
    And the records contain database metadata

  Scenario: Query a database by ID returns record
    Given a Notion connector with valid token
    When I query resource "database" with database_id "db123"
    Then the result has records
    And the record contains database fields

  Scenario: Query pages in a database returns results
    Given a Notion connector with valid token
    When I query resource "pages" with database_id "db123"
    Then the result has records

  Scenario: Query a page by ID returns record
    Given a Notion connector with valid token
    When I query resource "page" with page_id "p123"
    Then the result has records
    And the record contains Notion page fields

  Scenario: Query users returns results
    Given a Notion connector with valid token
    When I query resource "users" with limit 10
    Then the result has records

  Scenario: Write a new page succeeds
    Given a Notion connector with valid token
    When I write Notion resource "page" with database_id "db123" and title "New Task"
    Then the write succeeds

  Scenario: Missing database_id raises an error
    Given a Notion connector with valid token
    When I query resource "pages" without database_id filter
    Then the result is an error
