Feature: Theme Switching
  As a user
  I want to switch between standard and agent themes
  So that I can use my preferred visual style

  Scenario: Default theme is standard
    Given I open the Modulo app
    Then the data-theme attribute is "standard"

  Scenario: Switch to agent theme
    Given I open the Modulo app
    When I click the theme toggle
    Then the data-theme attribute is "agent"
    And the background uses the agent colour palette

  Scenario: Theme preference persists across reload
    Given I have selected the agent theme
    When I reload the page
    Then the data-theme attribute is still "agent"
