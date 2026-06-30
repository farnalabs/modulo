Feature: WebSocket Reconnection and Event Replay
  As a pipeline operator watching a run
  I want WebSocket connections to survive network drops
  So that I never miss an event during a run

  Background:
    Given run "test-run-1" has an active event broker

  Scenario: Subscribe to broker and receive published events
    Given I subscribe to run "test-run-1"
    When the broker publishes event "node_start"
    Then I receive the event on my queue
    And the event has the correct run_id

  Scenario: Events carry monotonically increasing sequence numbers
    Given I subscribe to run "test-run-1"
    When the broker publishes 3 events
    Then I receive 3 events with seq 1, 2, 3 in order

  Scenario: Reconnect with since_event_seq replays missed events
    Given I subscribe to run "test-run-1"
    And I have consumed events up to seq 3
    When the broker publishes 2 more events
    And I call replay_since(3)
    Then I receive the 2 missed events with seq 4 and 5

  Scenario: Ring buffer retains last 100 events
    Given I subscribe to run "test-run-1"
    When the broker publishes 105 events
    Then the ring buffer contains exactly 100 events
    And the oldest buffered event has seq 6

  Scenario: Evicted sequence numbers are not available for replay
    Given I subscribe to run "test-run-1"
    When the broker publishes 105 events
    And I call replay_since(1)
    Then no events are returned
    And seq 1 has been evicted from the buffer

  Scenario: Terminal run sends terminal status and closes
    Given run "test-run-1" has status "completed"
    When the run_websocket handler processes the connection
    Then it sends a JSON message with status "terminal"
    And the message includes run_status and run_id

  Scenario: Negative since_event_seq is rejected
    Given I subscribe to run "test-run-1"
    When the run_websocket handler receives since_event_seq=-1
    Then the WebSocket is closed with code 4001

  Scenario: Concurrent WebSocket connections all receive events
    Given run "test-run-concurrent" has an active event broker
    And 3 subscribers are connected to the same run
    When the broker publishes 5 events
    Then all 3 subscribers receive all 5 events
    And each subscriber receives events with correct monotonic sequence

  Scenario: Disconnecting one subscriber does not affect others
    Given run "test-run-concurrent" has an active event broker
    And 3 subscribers are connected to the same run
    When 1 subscriber disconnects
    And the broker publishes 3 events
    Then the remaining 2 subscribers receive the 3 events
    And the disconnected subscriber receives nothing
