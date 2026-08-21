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
