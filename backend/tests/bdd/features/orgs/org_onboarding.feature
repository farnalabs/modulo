Feature: Organisation Onboarding
  As a new user
  I want to complete an onboarding wizard
  So that I can quickly connect tools and configure my first pipeline

  Scenario: First run returns is_first_run true
    Given a new organisation signs up
    When I GET /api/v1/onboarding/status
    Then the response indicates it is the first run
    And the current step is step 1

  Scenario: Mark a step as completed
    Given a new organisation signs up
    When I POST /api/v1/onboarding/step with step_id "connect_tools"
    Then the step is marked completed
    And completed_steps contains "connect_tools"

  Scenario: Invalid step_id returns error
    Given a new organisation signs up
    When I POST /api/v1/onboarding/step with step_id "nonexistent_step"
    Then the response status is 422

  Scenario: All steps completed ends onboarding
    Given the welcome flow is completed
    When all onboarding steps are marked complete
    Then is_first_run becomes false

  Scenario: Get step data returns connector info
    Given a new organisation signs up
    When I GET /api/v1/onboarding/step/connect_tools
    Then the response contains connector options
