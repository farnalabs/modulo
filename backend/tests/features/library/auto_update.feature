Feature: Auto-Update Control for Library Primitives
  As a pipeline author
  I want to control whether my adapted library primitives auto-update
  So that I can pin a primitive to a specific version

  Background:
    Given the organisation has 3 local primitives
    And 5 community primitives exist

  Scenario: Adapted primitive has auto-update enabled by default
    Given a specific primitive exists with id "comm-001"
    When the user sends POST /api/v1/libraries/comm-001/adapt
    Then the response status is 201
    And the new primitive has auto_update set to true

  Scenario: Toggle auto-update off
    Given a local primitive exists with id "local-001"
    When the user sends PATCH /api/v1/libraries/local-001 with auto_update false
    Then the response status is 200
    And the primitive has auto_update set to false

  Scenario: Toggle auto-update back on
    Given a local primitive exists with id "local-001" and auto_update is false
    When the user sends PATCH /api/v1/libraries/local-001 with auto_update true
    Then the response status is 200
    And the primitive has auto_update set to true

  Scenario: Primitives with auto-update off do not receive update notifications
    Given a published contribution with id "contrib-001" has a new version "2.0"
    And a forked copy of "contrib-001" exists with auto_update set to false
    When the system notifies importers of update for "contrib-001"
    Then the forked copy's update_available_version_id remains null

  Scenario: Primitives with auto-update on receive update notifications
    Given a published contribution with id "contrib-002" has a new version "2.0"
    And a forked copy of "contrib-002" exists with auto_update set to true
    When the system notifies importers of update for "contrib-002"
    Then the forked copy's update_available_version_id is set to the new version id
