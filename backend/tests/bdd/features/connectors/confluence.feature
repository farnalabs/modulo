Feature: Confluence Connector
  As a pipeline author
  I want to interact with Confluence via the connector
  So that I can read, search, create, and update pages

  Scenario: Query pages in a space
    Given a Confluence connector with valid credentials
    When I query resource "pages" with space_id "s1"
    Then the result has records
    And the records contain page metadata

  Scenario: Query a single page by ID
    Given a Confluence connector with valid credentials
    When I query resource "page" with page_id "p1"
    Then the result has records
    And the record contains page fields

  Scenario: Query spaces
    Given a Confluence connector with valid credentials
    When I query resource "spaces" with type "global"
    Then the result has records
    And the records contain space metadata

  Scenario: Search content by CQL
    Given a Confluence connector with valid credentials
    When I query resource "content" with cql "text~bug"
    Then the result has records
    And the records contain page metadata

  Scenario: Query child pages
    Given a Confluence connector with valid credentials
    When I query resource "children" with page_id "p1"
    Then the result has records
    And the records contain page metadata

  Scenario: Query page labels
    Given a Confluence connector with valid credentials
    When I query resource "labels" with page_id "p1"
    Then the result has records
    And the records contain label metadata

  Scenario: Create a page
    Given a Confluence connector with valid credentials
    When I write resource "page" in space "s1" with title "New Page"
    Then the write succeeds

  Scenario: Add a label to a page
    Given a Confluence connector with valid credentials
    When I write resource "label" on page "p1" with name "how-to"
    Then the write succeeds

  Scenario: Health check returns valid response
    Given a Confluence connector with valid credentials
    When I perform a health check
    Then the health result is ok
