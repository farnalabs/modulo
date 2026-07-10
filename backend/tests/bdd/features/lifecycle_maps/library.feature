Feature: Lifecycle Map as Library Primitive
  As a user
  I want lifecycle maps to be storable as library primitives
  So that I can export, import, and share them via bundles

  Background:
    Given I am authenticated as an admin in org "acme"

  Scenario: Library supports lifecycle_map primitive type
    When I query the library for primitive type "lifecycle_map"
    Then the response status is 200
    And the response contains primitives filtered by type "lifecycle_map"

  Scenario: Lifecycle map can be saved to library
    Given a lifecycle map named "SDLC Workflow" exists
    When I save the lifecycle map to the library as "sdlc-workflow"
    Then the response status is 201
    And the library contains a primitive of type "lifecycle_map" with slug "sdlc-workflow"

  Scenario: Lifecycle map bundle export
    Given a lifecycle map named "SDLC Workflow" exists with content_json:
      | stages: [{id: "stage-1", name: "Dev"}] |
    When I export the lifecycle map as a bundle
    Then the response status is 200
    And the bundle contains lifecycle_map content

  Scenario: Lifecycle map bundle import
    Given I have a lifecycle map bundle
    When I import the bundle
    Then the response status is 201
    And a lifecycle map named "Imported SDLC Workflow" exists
