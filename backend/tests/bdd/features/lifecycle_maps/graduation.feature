Feature: Lifecycle Map Graduation
  As a user
  I want to graduate stages from manual to modulo
  So that I can automate previously manual workflow steps

  Background:
    Given I am authenticated as an admin in org "acme"

  Scenario: Graduate a manual stage to modulo
    Given a lifecycle map named "SDLC Workflow" exists with content_json:
      | stages: [{id: "stage-1", name: "Code Review", type: "manual"}] |
    When I graduate stage "stage-1" to modulo with pipeline_name "Code Review Pipeline"
    Then the response status is 200
    And the stage type is "modulo"
    And the stage has a pipeline link

  Scenario: Graduation creates a new version
    Given a lifecycle map named "SDLC Workflow" exists with version 3
    And the map has a manual stage "stage-1"
    When I graduate stage "stage-1" to modulo with pipeline_name "Review Pipeline"
    Then the response status is 200
    And the lifecycle map has version 4

  Scenario: Cannot graduate an already modulo stage
    Given a lifecycle map named "SDLC Workflow" exists with content_json:
      | stages: [{id: "stage-1", name: "Build", type: "modulo"}] |
    When I graduate stage "stage-1" to modulo with pipeline_name "Build Pipeline"
    Then the response status is 409
