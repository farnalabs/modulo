Feature: Onboarding action checklist
  As a new Modulo user with a freshly created organisation
  I want a persisted 6-action onboarding checklist driven by real org state
  So that I can complete or skip first-run setup and seed a starter example

  Background:
    Given the onboarding progress is fully empty
    And I am authenticated as an admin in org "default"

  Scenario: First-run status exposes the six-action checklist
    Given the org state auto-completes only the "login" action
    When I GET the onboarding status
    Then the response status is 200
    And the response indicates this is the first run
    And the response exposes 6 onboarding actions in order starting with "login"
    And the response reports the "login" action as completed

  Scenario: Auto-completion reflects real organisation state
    Given the stored organisation already has every onboarding primitive
    When I GET the onboarding status
    Then the response status is 200
    And the response reports all 6 onboarding actions as completed
    And the response reports 100% progress

  Scenario: Auto-completion detects a bare organisation from real state
    Given the stored organisation has no onboarding primitives
    When I GET the onboarding status
    Then the response status is 200
    And the response reports the "login" action as completed
    And the response reports 1 onboarding action as completed

  Scenario: Completing an action updates the persisted progress
    Given the org state auto-completes only the "login" action
    When I complete the onboarding action "create_first_schema"
    Then the response status is 200
    And the response reports action "create_first_schema" as completed
    And a subsequent status read reports 2 completed actions at 33% progress

  Scenario: Repeating the same completion is idempotent
    Given the onboarding progress has the "login" action completed
    When I complete the onboarding action "login"
    Then the response status is 200
    And a subsequent status read still reports exactly 1 completed action

  Scenario: Skipping an action records it without completing it
    Given the org state auto-completes only the "login" action
    When I skip the onboarding action "create_first_agent"
    Then the response status is 200
    And the response reports action "create_first_agent" as skipped
    And a subsequent status read reports "create_first_agent" as skipped

  Scenario: An unknown action id is rejected with 422
    When I complete the onboarding action "nonexistent"
    Then the response status is 422

  Scenario: Dismissing the wizard ends the first-run state
    When I dismiss the onboarding wizard
    Then the response status is 200
    And a subsequent status read no longer reports a first run

  Scenario: Seeding examples with a model backend creates the primitives
    Given the org has a model backend configured
    When I seed the onboarding examples
    Then the response status is 201
    And the seed response carries an agent, a schema, and a pipeline
    And the seed marks the schema, agent, and pipeline actions completed

  Scenario: Seeding examples without a model backend is refused before any write
    Given the org has no model backend configured
    When I seed the onboarding examples
    Then the response status is 409
    And the refusal says a model backend is required
    And no schema, agent, or pipeline was created

  Scenario: A starter pipeline is created for a fresh org
    When I create the onboarding starter pipeline
    Then the response status is 201
    And the starter pipeline response carries a pipeline id and the starter name
