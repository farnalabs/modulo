Feature: HITL Overdue Warning
  As a pipeline operator
  I want to see warnings when a HITL gate is approaching its timeout
  So that I know which gates need urgent attention

  Background:
    Given I am authenticated in org "acme"

  Scenario: Gate near timeout shows warning
    Given a run is waiting at gate "pre-deploy" with timeout 300s
    When 270 seconds have elapsed without approval
    Then the gate shows an "overdue" warning
    And the warning is visible in the stage board

  Scenario: Stage board highlights overdue gates
    Given 2 runs are waiting at gates
    And one gate is overdue
    When I GET the stage board with filter "awaiting_human"
    Then the overdue gate is highlighted

  Scenario: Notification is sent for overdue gates
    Given a run is waiting at gate "pre-deploy" with timeout 300s
    When the gate becomes overdue
    Then a notification is sent to configured approvers
    And the notification type is "hitl_overdue"

  Scenario: Overdue badge shows remaining time
    Given a run is waiting at gate "pre-deploy" with timeout 300s
    When 270 seconds have elapsed
    Then the overdue badge shows "30s remaining"
