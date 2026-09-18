Feature: System Admin — Configuration Management
  As a system admin
  I want to manage deployment-wide settings
  So that I can configure system behaviour per-deployment

  Scenario: System admin sets a config value
    Given I am authenticated as a system admin
    When I set system config "default_plan" to "team"
    Then the config value is saved

  Scenario: System admin reads config values
    Given I am authenticated as a system admin
    When I list all system config
    Then I see all configured keys and values

  Scenario: Regular admin cannot access config
    Given I am authenticated as an org admin
    When I attempt to list system config
    Then I receive a 403 Forbidden error

  Scenario: System admin deletes a config value
    Given I am authenticated as a system admin
    When I delete system config "default_plan"
    Then the config entry is deleted

  Scenario: Deleting an unknown config key returns not found
    Given I am authenticated as a system admin
    When I delete a missing config key "does_not_exist"
    Then I receive a 404 Not Found error

  Scenario: Regular admin cannot delete a config value
    Given I am authenticated as an org admin
    When I attempt to delete system config "default_plan"
    Then I receive a 403 Forbidden error
