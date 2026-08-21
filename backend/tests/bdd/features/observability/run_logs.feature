Feature: Run Logs
  As a pipeline operator
  I want to stream per-node execution logs during a run
  So that I can monitor progress and diagnose issues

  Scenario: Logs are captured during run execution
    Given a pipeline run is in progress
    When a node begins executing
    Then log entries are emitted for the node

  Scenario: Logs are grouped by node
    Given a pipeline run with multiple nodes
    When all nodes complete
    Then log entries are grouped by node id

  Scenario: Error logs are captured on node failure
    Given a pipeline run is in progress
    When a node raises an exception
    Then error log entries are captured

  Scenario: Log streaming delivers events in real time
    Given a pipeline run is in progress
    When I subscribe to the run event stream
    Then log entries are delivered in real time
