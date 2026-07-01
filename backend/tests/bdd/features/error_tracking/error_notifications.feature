Feature: Error Notifications
  As an admin
  I want to be alerted on critical errors
  So that I can respond quickly

  Background:
    Given an organisation with a notification rule for critical errors

  Scenario: Alert fires on critical error
    When a critical error event is ingested
    Then an alert is dispatched

  Scenario: Cooldown prevents alert storm
    When the same error is ingested 3 times within cooldown
    Then only 1 alert is dispatched

  Scenario: Create a notification rule
    When I POST /api/v1/errors/notification-rules with valid config
    Then the rule is created

  Scenario: Max 10 rules per org
    Given an org has 10 notification rules
    And I create an 11th rule
    Then the response status is 422
