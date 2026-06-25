Feature: Real-Time Run Updates
  As a user watching a pipeline run
  I want the UI to update without refreshing
  So that I see live progress from the agent

  Scenario: Status updates arrive via WebSocket
    Given I am viewing run details for run "run-abc"
    And the run is in "running" state
    When the backend emits a node_complete event
    Then the run detail page shows the updated node status

  Scenario: HITL approval notification appears
    Given I am viewing run details for run "run-xyz"
    When the run reaches an approval gate
    Then an approval banner appears without page refresh

  Scenario: WebSocket reconnects after disconnect
    Given I am viewing a running pipeline
    When the WebSocket connection drops
    Then the client reconnects automatically
    And resumes receiving updates
