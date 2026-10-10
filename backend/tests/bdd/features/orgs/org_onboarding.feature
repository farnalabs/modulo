Feature: Organisation Onboarding
  As a new user
  I want to complete the onboarding wizard
  So that I can quickly connect tools and configure my first pipeline

  Scenario: First run returns is_first_run true
    Given a new organisation signs up
    When I GET /api/v1/onboarding/status
    Then the response indicates it is the first run
    And the onboarding status lists the login action as auto-completed

  Scenario: Mark an action as completed
    Given a new organisation signs up
    When I POST /api/v1/onboarding/actions/create_first_agent/complete with action_id "create_first_agent"
    Then the step is marked completed
    Then the response echoes action_id "create_first_agent"

  Scenario: Invalid action_id returns error
    Given a new organisation signs up
    When I POST /api/v1/onboarding/actions/nonexistent_step/complete with action_id "nonexistent_step"
    Then the response status is 422

  Scenario: All steps completed ends onboarding
    Given the welcome flow is completed
    When all onboarding steps are marked complete
    Then is_first_run becomes false

  Scenario: Onboarding status lists the recommended actions
    Given a new organisation signs up
    When I GET /api/v1/onboarding/status
    Then the response contains the "add_ai_model" onboarding action
