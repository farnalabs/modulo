Feature: Lifecycle Map Graduation
  As a user
  I want to graduate stages from manual to modulo
  So that I can automate previously manual workflow steps

  Background:
    Given I am authenticated as an admin in org "acme"

  Scenario: Lifecycle maps can store stage type information
    Given a lifecycle map named "SDLC Workflow" exists with a manual stage "stage-1"
    When I get the lifecycle map by id
    Then the response status is 200
    And the response contains a lifecycle map named "SDLC Workflow"
