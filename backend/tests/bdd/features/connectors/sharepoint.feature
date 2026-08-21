Feature: SharePoint Connector
  As a pipeline author
  I want to interact with Microsoft SharePoint
  So that my agents can read sites, lists, list items, and files, and write list items and files

  Background:
    Given I am authenticated in org "acme"

  Scenario: Health check passes
    Given a SharePoint connector configured with valid token
    When the connector checks health
    Then the health check reports the site root name

  Scenario: List sites
    Given a SharePoint connector configured with valid token
    When the connector lists sites
    Then the result contains SharePoint sites

  Scenario: List items in a list
    Given a SharePoint connector configured with valid token
    When the connector lists items in list "list1" of site "site1"
    Then the result contains list items

  Scenario: Create a list item
    Given a SharePoint connector configured with valid token
    When the connector creates a list item in list "list1" of site "site1" with fields Title "New Task"
    Then the list item is created successfully

  Scenario: File operations
    Given a SharePoint connector configured with valid token
    When the connector reads file "/documents/report.docx" from site "site1" drive "drive1"
    Then the connector returns the file content

  Scenario: Invalid credentials are rejected
    Given a SharePoint connector configured with invalid credentials
    When the connector checks health
    Then the health check returns "unhealthy"
