Feature: Role-Based Access Control
  As the platform
  I want to enforce role hierarchy across org and team scopes
  So that users only have the access they are entitled to

  Scenario: Effective team role is capped by org role
    Given I am an admin user with org role "runner"
    And I have team role "operator"
    When I compute the effective team role
    Then the effective role is "runner"

  Scenario: Viewer org role cannot be elevated by team role
    Given I am an admin user with org role "viewer"
    And I have team role "operator"
    When I compute the effective team role
    Then the effective role is "viewer"

  Scenario: Admin org role is not capped by team role
    Given I am an admin user with org role "admin"
    And I have team role "runner"
    When I compute the effective team role
    Then the effective role is "runner"

  Scenario: Unrecognised role falls back to viewer
    Given I am an admin user with org role "superadmin"
    And I have team role "operator"
    When I compute the effective team role
    Then the effective role is "viewer"

  Scenario: Role hierarchy levels are ordered correctly
    Given the role hierarchy for "admin" is 3
    And the role hierarchy for "operator" is 2
    And the role hierarchy for "runner" is 1
    And the role hierarchy for "viewer" is 0
    Then each level is strictly higher than the previous
