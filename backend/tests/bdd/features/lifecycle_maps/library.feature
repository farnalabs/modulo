Feature: Lifecycle Map as Library Primitive
  As a user
  I want lifecycle maps to be storable as library primitives
  So that I can export, import, and share them via bundles

  Background:
    Given I am authenticated as an admin in org "acme"

  Scenario: Lifecycle maps can be created with valid content
    When I create a lifecycle map named "SDLC Workflow" with visibility "org"
    Then the response status is 201
    And the response contains a lifecycle map named "SDLC Workflow"

  Scenario: Export returns the active version as a portable envelope
    Given a lifecycle map named "SDLC Workflow" exists
    When I export the lifecycle map
    Then the response status is 200
    And the response is a lifecycle map export envelope

  Scenario: Importing an exported envelope creates a new map
    When I import a lifecycle map named "Imported SDLC"
    Then the response status is 201
    And the response contains a lifecycle map named "Imported SDLC"

  Scenario: Importing a lifecycle map with invalid content is rejected
    When I import a lifecycle map with invalid content
    Then the response status is 422
