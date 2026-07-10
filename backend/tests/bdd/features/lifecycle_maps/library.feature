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
