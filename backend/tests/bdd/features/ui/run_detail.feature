Feature: Run Detail View
  As a pipeline operator
  I want to inspect individual run details, node outputs, and logs
  So that I can debug and audit pipeline executions

  Scenario: View run status and details
    Given I am on the run detail page for a completed run
    Then I see the run status
    And the run has 3 nodes with their outputs

  Scenario: Expand a node to view its output
    Given I am on the run detail page for a completed run
    When I click on a node to expand its output
    Then I see the node input and output payload

  Scenario: View run logs
    Given I am on the run detail page for a completed run
    When I click the log viewer tab
    Then I see per-node log entries

  Scenario: Sensitive values are masked in run output
    Given I am on the run detail page for a completed run
    Then sensitive values are masked with ●●●●●

  Scenario: Live status updates via WebSocket
    Given I am on the run detail page for a running run
    When the backend emits a node_complete event
    Then the run detail page shows the updated node statuses
