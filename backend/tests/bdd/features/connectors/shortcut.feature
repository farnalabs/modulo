Feature: Shortcut Connector
  As a pipeline author
  I want to interact with Shortcut stories, epics, and projects
  So that my agents can manage projects on Shortcut

  Background:
    Given a Shortcut connector with valid API token

  Scenario: Health check with valid credentials
    Given the Shortcut API returns a valid member profile
    When I perform a health check
    Then the health result is ok

  Scenario: Health check with invalid credentials
    Given the Shortcut API returns 401 Unauthorized
    When I perform a health check
    Then the health result is not ok

  Scenario: List stories
    Given the Shortcut API returns available stories
    When I query resource "stories" with limit 10
    Then the result has records
    And the records contain story metadata

  Scenario: Get single story
    Given the Shortcut API returns a single story
    When I query resource "story" with story_id "1"
    Then the result has records
    And the record contains story fields

  Scenario: List projects
    Given the Shortcut API returns available projects
    When I query resource "projects" with limit 10
    Then the result has records

  Scenario: List epics
    Given the Shortcut API returns available epics
    When I query resource "epics" with limit 10
    Then the result has records

  Scenario: Create a story
    Given the Shortcut API accepts story creation
    When I write resource "story" with name "New Story" and project_id "42"
    Then the write succeeds

  Scenario: Update a story
    Given the Shortcut API accepts story updates
    When I write resource "story_update" for story "1" with new name "Updated Name"
    Then the write succeeds

  Scenario: Add comment to story
    Given the Shortcut API accepts story comments
    When I write resource "story_comment" for story "1" with text "Nice work!"
    Then the write succeeds

  Scenario: Query unknown resource returns error
    Given the Shortcut connector is configured
    When I query resource "invalid"
    Then the result is an error
