Feature: Auto-Update Control for Library Primitives
  Users can control whether adapted library primitives auto-update when
  the upstream contribution publishes a new version.

  Background:
    Given the organisation exists
    And a community primitive "PRD Input Schema" exists

  Scenario: Adapted primitive has auto-update enabled by default
    When the user sends POST /api/v1/libraries/{community_primitive_id}/adapt
    Then the response status is 201
    And the new primitive has auto_update set to true

  Scenario: Toggle auto-update off on an adapted primitive
    Given the user has adapted the community primitive
    When the user sends PATCH /api/v1/libraries/{adapted_primitive_id} with {"auto_update": false}
    Then the response status is 200
    And the primitive has auto_update set to false

  Scenario: Toggle auto-update back on
    Given the user has adapted the community primitive
    And auto-update is disabled on the adapted primitive
    When the user sends PATCH /api/v1/libraries/{adapted_primitive_id} with {"auto_update": true}
    Then the response status is 200
    And the primitive has auto_update set to true
