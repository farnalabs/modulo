Feature: Dropbox Paper Connector
  As a pipeline author
  I want to interact with Dropbox Paper
  So that my agents can list docs, download doc content, list folders, and create new documents

  Background:
    Given I am authenticated in org "acme"

  Scenario: Health check passes
    Given a Dropbox Paper connector configured with valid token
    When the connector checks health
    Then the health check reports the authenticated email

  Scenario: Health check fails on bad credentials
    Given a Dropbox Paper connector configured with invalid token
    When the connector checks health
    Then the health check returns "unhealthy"

  Scenario: List Paper docs
    Given a Dropbox Paper connector configured with valid token
    When the connector lists Paper docs with filter "docs_created"
    Then the result contains doc IDs

  Scenario: Download a Paper doc as markdown
    Given a Dropbox Paper connector configured with valid token
    When the connector downloads doc "abc123"
    Then the result contains the doc content as markdown

  Scenario: List folders
    Given a Dropbox Paper connector configured with valid token
    When the connector lists folders at path "/Paper"
    Then the result contains folder entries

  Scenario: Create a new Paper doc
    Given a Dropbox Paper connector configured with valid token
    When the connector creates a Paper doc titled "Meeting Notes" with markdown content
    Then the doc is created successfully and returns metadata

  Scenario: Query unsupported resource raises error
    Given a Dropbox Paper connector configured with valid token
    When the connector queries unsupported resource "users"
    Then the connector raises an error

  Scenario: Write unsupported resource raises error
    Given a Dropbox Paper connector configured with valid token
    When the connector writes to unsupported resource "folder"
    Then the connector raises an error
