Feature: Lifecycle Map Versioning
  As a user
  I want versions to increment on content changes
  So that I can track how the map evolves

  Background:
    Given I am authenticated as an admin in org "acme"

  Scenario: Creating a new map starts at version 1
    Given a lifecycle map named "Release Workflow" exists
    When I get the lifecycle map by id
    Then the response status is 200
    And the lifecycle map has version 1

  Scenario: Explicit content save creates new version
    Given a lifecycle map named "Release Workflow" exists
    When I update the lifecycle map content to include 1 stage
    Then the response status is 200
    And the lifecycle map has version 2

  Scenario: Multiple content updates increment version
    Given a lifecycle map named "Release Workflow" exists
    When I update the lifecycle map content to include 1 stage
    When I update the lifecycle map content to include 2 stages
    Then the response status is 200
    And the lifecycle map has version 3

  Scenario: Metadata-only updates do not increment version
    Given a lifecycle map named "Release Workflow" exists with version 5
    When I update the lifecycle map description to "Updated description only"
    Then the response status is 200
    And the lifecycle map has version 5
